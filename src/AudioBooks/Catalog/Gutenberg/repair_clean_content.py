"""
Re-apply _normalize_clean_text to books listed in books_needing_clean_fix.csv.

Fixes books whose clean_content still contains Gutenberg license text or CSS
artifacts because improved trailer/HTML stripping was added after the initial
backfill. Re-cleans the existing raw_content in place — no re-downloading.

Usage:
  python AudioBooks/Catalog/Gutenberg/repair_clean_content.py
  python AudioBooks/Catalog/Gutenberg/repair_clean_content.py --dry-run
  python AudioBooks/Catalog/Gutenberg/repair_clean_content.py --limit 200
  python AudioBooks/Catalog/Gutenberg/repair_clean_content.py --batch-size 1000
  python AudioBooks/Catalog/Gutenberg/repair_clean_content.py --csv-path /path/to/other.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.backfill_missing_book_contents import _normalize_clean_text
from AudioBooks.Catalog.Gutenberg.db_utils import connect_db, with_sqlite_retry

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
CSV_PATH = BASE_DIR / "books_needing_clean_fix.csv"
DEFAULT_BATCH_SIZE = 500
GUTENBERG_PREAMBLE_START_RE = re.compile(
    r"^\s*\*{3,}\s*start of (?:(?:this|the)\s+)?project gutenberg",
    re.IGNORECASE,
)
GUTENBERG_PREAMBLE_HEADER_RE = re.compile(
    r"^\s*the project gutenberg ebook[, ]",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-apply clean text normalization for books with Gutenberg license contamination.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to gutenbergindex.db")
    parser.add_argument("--csv-path", default=str(CSV_PATH), help="Path to books_needing_clean_fix.csv")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Books per DB batch (default: 500)")
    parser.add_argument("--limit", type=int, default=None, help="Stop after processing N books")
    parser.add_argument("--dry-run", action="store_true", help="Re-clean in memory without writing to SQLite")
    parser.add_argument(
        "--patterns",
        default=None,
        help="Comma-separated pattern values from CSV (e.g. section1,end_marker). If omitted, all patterns are used.",
    )
    parser.add_argument(
        "--only-preamble",
        action="store_true",
        help="Only re-clean rows whose current clean_content still contains a Gutenberg preamble header/start marker.",
    )
    return parser.parse_args()


def _has_gutenberg_preamble(text: str) -> bool:
    if not text:
        return False
    lines = text.splitlines()
    max_scan = min(len(lines), 800)
    for i in range(max_scan):
        stripped = lines[i].strip()
        if GUTENBERG_PREAMBLE_START_RE.match(stripped) or GUTENBERG_PREAMBLE_HEADER_RE.match(stripped):
            return True
    return False


def _read_book_ids(csv_path: str, limit: int | None, allowed_patterns: set[str] | None) -> list[int]:
    book_ids: list[int] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pattern = (row.get("pattern") or "").strip().lower()
            if allowed_patterns is not None and pattern not in allowed_patterns:
                continue
            val = (row.get("book_id") or "").strip()
            if val.isdigit():
                book_ids.append(int(val))
    seen: set[int] = set()
    unique = [bid for bid in book_ids if not (bid in seen or seen.add(bid))]
    return unique[:limit] if limit is not None else unique


def _process_batch(db_path: str, book_ids: list[int], *, dry_run: bool, only_preamble: bool) -> tuple[int, int, int, int]:
    """Re-clean one batch.

    Returns: (updated, unchanged, skipped_no_raw_content, skipped_no_preamble)
    """
    placeholders = ",".join("?" * len(book_ids))

    def fetch():
        conn = connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            f"SELECT bookid, raw_content, clean_content FROM book_contents WHERE bookid IN ({placeholders})",
            book_ids,
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    rows = with_sqlite_retry(fetch)

    updates: list[tuple[str, int]] = []
    skipped_no_raw = 0
    skipped_no_preamble = 0
    unchanged = 0
    for book_id, raw_content, clean_content in rows:
        current_clean = clean_content or ""
        if only_preamble and not _has_gutenberg_preamble(current_clean):
            skipped_no_preamble += 1
            continue
        if not raw_content:
            skipped_no_raw += 1
            continue
        new_clean = _normalize_clean_text(raw_content)
        if new_clean == current_clean:
            unchanged += 1
            continue
        updates.append((new_clean, int(book_id)))

    if dry_run or not updates:
        return len(updates), unchanged, skipped_no_raw, skipped_no_preamble

    def write():
        conn = connect_db(db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executemany(
            "UPDATE book_contents SET clean_content = ? WHERE bookid = ?",
            updates,
        )
        conn.commit()
        conn.close()

    with_sqlite_retry(write)
    return len(updates), unchanged, skipped_no_raw, skipped_no_preamble


def main() -> None:
    args = parse_args()
    allowed_patterns = None
    if args.patterns:
        allowed_patterns = {p.strip().lower() for p in args.patterns.split(",") if p.strip()}
        if not allowed_patterns:
            allowed_patterns = None

    print(f"stage: reading book ids from {args.csv_path}", flush=True)
    book_ids = _read_book_ids(args.csv_path, args.limit, allowed_patterns)
    print(f"summary: books_to_repair={len(book_ids)}", flush=True)
    if not book_ids:
        print("done: nothing to do", flush=True)
        return

    if args.dry_run:
        print("stage: dry-run enabled; no writes will be committed", flush=True)
    if allowed_patterns is not None:
        print(f"summary: csv_patterns={','.join(sorted(allowed_patterns))}", flush=True)
    if args.only_preamble:
        print("summary: mode=only-preamble", flush=True)

    batches = [book_ids[i : i + args.batch_size] for i in range(0, len(book_ids), args.batch_size)]
    total_updated = 0
    total_unchanged = 0
    total_skipped_no_raw = 0
    total_skipped_no_preamble = 0

    for batch_num, batch in enumerate(batches, 1):
        updated, unchanged, skipped_no_raw, skipped_no_preamble = _process_batch(
            args.db_path,
            batch,
            dry_run=args.dry_run,
            only_preamble=args.only_preamble,
        )
        total_updated += updated
        total_unchanged += unchanged
        total_skipped_no_raw += skipped_no_raw
        total_skipped_no_preamble += skipped_no_preamble
        print(
            f"stage: batch {batch_num}/{len(batches)}"
            f"  updated={total_updated}  unchanged={total_unchanged}"
            f"  skipped_no_raw={total_skipped_no_raw}"
            f"  skipped_no_preamble={total_skipped_no_preamble}",
            flush=True,
        )

    print(
        f"done: total_updated={total_updated}"
        f"  total_unchanged={total_unchanged}"
        f"  total_skipped_no_raw={total_skipped_no_raw}"
        f"  total_skipped_no_preamble={total_skipped_no_preamble}",
        flush=True,
    )


if __name__ == "__main__":
    main()
