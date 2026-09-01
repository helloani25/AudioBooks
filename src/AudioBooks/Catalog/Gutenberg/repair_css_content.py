"""
Re-download HTML books listed in books_needing_css_clean.csv to fix CSS
contamination stored in raw_content.

The _html_bytes_to_text fix (drop <head>/<style> nodes) only applies to
newly downloaded content.  Books already stored with CSS text embedded in
raw_content must be fully re-downloaded so the improved parser can run.

re_clean_content.py is NOT sufficient for these books — the damage is upstream
of clean_content in raw_content itself.

CSV format (pipe-delimited, no header): book_id|gutenberg_id|title

Usage:
  python AudioBooks/Catalog/Gutenberg/repair_css_content.py
  python AudioBooks/Catalog/Gutenberg/repair_css_content.py --dry-run
  python AudioBooks/Catalog/Gutenberg/repair_css_content.py --limit 200
  python AudioBooks/Catalog/Gutenberg/repair_css_content.py --workers 4 --mirror-tries 3
  python AudioBooks/Catalog/Gutenberg/repair_css_content.py --reset-queue
  python AudioBooks/Catalog/Gutenberg/repair_css_content.py --no-verify
  python AudioBooks/Catalog/Gutenberg/repair_css_content.py --ca-bundle /path/to/cacert.pem
"""

from __future__ import annotations

import argparse
import csv
import ssl
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Generator

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.backfill_missing_book_contents import (
    _get_mirrors,
    _install_https_opener,
    _load_queue_books,
    _process_backfill_queue_item,
    _queue_seed_books,
    _resolve_ca_paths,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
CSV_PATH = BASE_DIR / "books_needing_css_clean.csv"
QUEUE_KEY = "css-repair:v1"
DEFAULT_WORKERS = 4
DEFAULT_MIRROR_TRIES = 3
DEFAULT_CHUNK_SIZE = 100
DEFAULT_MAX_ATTEMPTS = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-download HTML books with CSS contamination in raw_content.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to gutenbergindex.db")
    parser.add_argument("--csv-path", default=str(CSV_PATH), help="Path to books_needing_css_clean.csv")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Parallel download workers (default: 4)")
    parser.add_argument("--mirror-tries", type=int, default=DEFAULT_MIRROR_TRIES, help="Mirrors to try per book (default: 3)")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Books dispatched per executor batch (default: 100)")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS, help="Max download attempts before giving up (default: 3)")
    parser.add_argument("--limit", type=int, default=None, help="Stop after processing N books")
    parser.add_argument("--dry-run", action="store_true", help="Discover and parse without writing to SQLite")
    parser.add_argument("--reset-queue", action="store_true", help="Discard saved queue state and restart from scratch")
    parser.add_argument("--ca-bundle", dest="cafile", default=None, help="Path to a PEM CA bundle file")
    parser.add_argument("--ca-dir", dest="capath", default=None, help="Path to a directory of CA certificates")
    parser.add_argument("--no-verify", action="store_false", dest="verify", default=True, help="Disable SSL certificate verification")
    return parser.parse_args()


def _read_books(csv_path: str, limit: int | None) -> list[tuple[int, int | None, str]]:
    books: list[tuple[int, int | None, str]] = []
    seen: set[int] = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="|"):
            if len(row) < 2:
                continue
            book_id_str = row[0].strip()
            gutenberg_id_str = row[1].strip()
            title = row[2].strip() if len(row) > 2 else ""
            if not book_id_str.isdigit():
                continue
            book_id = int(book_id_str)
            if book_id in seen:
                continue
            seen.add(book_id)
            gutenberg_id = int(gutenberg_id_str) if gutenberg_id_str.isdigit() else None
            books.append((book_id, gutenberg_id, title))
    return books[:limit] if limit is not None else books


def _batched(items: list, size: int) -> Generator[list, None, None]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main() -> None:
    args = parse_args()

    cafile, capath = _resolve_ca_paths(args.cafile, args.capath, args.verify)
    _install_https_opener(cafile, capath, args.verify)

    print(f"stage: reading book ids from {args.csv_path}", flush=True)
    books = _read_books(args.csv_path, args.limit)
    print(f"summary: books_to_repair={len(books)}", flush=True)
    if not books:
        print("done: nothing to do", flush=True)
        return

    print("stage: loading Project Gutenberg mirrors", flush=True)
    mirrors = _get_mirrors()
    print(f"summary: mirrors_loaded={len(mirrors)}", flush=True)

    print(f"stage: seeding queue namespace {QUEUE_KEY!r}", flush=True)
    _queue_seed_books(args.db_path, QUEUE_KEY, books, reset=args.reset_queue)

    queue_rows = _load_queue_books(args.db_path, QUEUE_KEY, max_attempts=args.max_attempts)
    print(f"summary: queued_books={len(queue_rows)}", flush=True)
    if not queue_rows:
        print("done: nothing pending (use --reset-queue to retry completed books)", flush=True)
        return

    if args.dry_run:
        print("stage: dry-run enabled; no writes will be committed", flush=True)

    print(
        f"stage: downloading with workers={args.workers} mirror_tries={args.mirror_tries} chunk_size={args.chunk_size}",
        flush=True,
    )

    processed = matched = written = previewed = skipped = failed = 0
    batch_size = max(1, args.chunk_size)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        for queue_batch in _batched(queue_rows, batch_size):
            futures = {
                executor.submit(
                    _process_backfill_queue_item,
                    args.db_path,
                    QUEUE_KEY,
                    book_id,
                    gutenberg_id,
                    title,
                    mirrors=mirrors,
                    repair_all=True,
                    preflight=False,
                    refresh_cache=False,
                    dry_run=args.dry_run,
                    mirror_tries=args.mirror_tries,
                ): (book_id, title)
                for book_id, gutenberg_id, title, _priority, _status, _attempts in queue_batch
            }

            for future in as_completed(futures):
                processed += 1
                book_id, title = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    failed += 1
                    print(f"fail {book_id}: {exc} ({title})", flush=True)
                    continue

                status = result["status"]
                if status in {"no_candidate", "skip", "audio-only"}:
                    skipped += 1
                    print(f"skip {book_id}: {result.get('reason') or 'no candidate'} ({title})", flush=True)
                    continue
                if status == "failed":
                    failed += 1
                    print(f"fail {book_id}: {result.get('error') or 'download failed'} ({title})", flush=True)
                    continue

                matched += 1
                source_type = result["source_type"]
                source_url = result.get("source_url", "")
                if args.dry_run:
                    previewed += 1
                    print(f"dry-run {book_id} [{source_type}] {source_url} ({title})", flush=True)
                else:
                    written += 1
                    print(f"saved {book_id} [{source_type}] {source_url} ({title})", flush=True)

    print(
        f"done: processed={processed}, matched={matched}, written={written}, "
        f"previewed={previewed}, skipped={skipped}, failed={failed}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except (ssl.SSLCertVerificationError, urllib.error.URLError) as exc:
        if isinstance(exc, ssl.SSLCertVerificationError) or (
            isinstance(exc, urllib.error.URLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc)
        ):
            raise RuntimeError(
                "TLS verification failed. Provide a trusted CA bundle via "
                "GUTENBERG_CA_BUNDLE, SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE, "
                "or pass --ca-bundle/--ca-dir. Alternatively, use --no-verify."
            ) from exc
        raise
