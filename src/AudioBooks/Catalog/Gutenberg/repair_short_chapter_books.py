from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
import multiprocessing as mp
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"

CHAPTER_HEADING_RE = re.compile(
    r"^\s*(chapter|book|part|section)\s+"
    r"(?:the\s+)?"
    r"(?:"
    r"[ivxlcdm]+|\d+|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|"
    r"seventeenth|eighteenth|nineteenth|twentieth"
    r")\b(?:\s*[:.\-–—]\s*.*)?\s*$",
    re.IGNORECASE,
)
CHAPTER_KEYWORD_RE = re.compile(r"\b(chapter|book|part|section)\b", re.IGNORECASE)
CHAPTER_SQL_KEYWORDS: tuple[str, ...] = ("chapter", "book", "part", "section")

DEFAULT_MIN_LINES = 10

DELETE_TABLES: tuple[tuple[str, str], ...] = (
    ("book_audio_chapters", "book_id"),
    ("book_audio", "book_id"),
    ("book_desc", "bookid"),
    ("book_contents", "bookid"),
    ("book_cover_art", "bookid"),
    ("book_content_backfill_queue", "bookid"),
    ("downloadlinks", "bookid"),
    ("book_subjects", "bookid"),
    ("book_authors", "bookid"),
    ("titles", "bookid"),
    ("books", "id"),
)


@dataclass(frozen=True)
class ChapterBlock:
    title: str
    text: str


@dataclass(frozen=True)
class ScanOutcome:
    book_id: int
    gutenberg_id: int | None
    title: str
    skipped_empty: bool
    skipped_audio: bool
    is_bad: bool
    line_counts: list[int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete books whose interior chapters are shorter than the configured minimum line count.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to the SQLite catalog database.")
    parser.add_argument("--dry-run", action="store_true", help="Preview deletions without writing to SQLite.")
    parser.add_argument(
        "--min-lines",
        type=int,
        default=DEFAULT_MIN_LINES,
        help="Minimum non-empty lines required for interior chapters.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after deleting this many books.",
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=None,
        help="Stop after scanning this many candidate books, regardless of matches.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print one line per scanned book instead of only matched deletions.",
    )
    parser.add_argument(
        "--book-id",
        action="append",
        type=int,
        dest="book_ids",
        help="Scan only the given internal book id(s). Can be passed multiple times.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel worker processes for chapter scanning.",
    )
    parser.add_argument(
        "--worker-chunk-size",
        type=int,
        default=8,
        help="Chunk size passed to process workers when --workers > 1.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=100,
        help="Commit SQLite deletes every N matched books (ignored in dry-run mode).",
    )
    parser.add_argument(
        "--db-fetch-size",
        type=int,
        default=500,
        help="Number of rows fetched per SQLite batch before dispatching to workers.",
    )
    parser.add_argument(
        "--delete-books-with-audio",
        action="store_true",
        help="Allow deleting books that already have audio rows (default keeps them).",
    )
    return parser.parse_args()


def _connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _has_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped and CHAPTER_HEADING_RE.match(stripped))


def split_into_chapters(text: str) -> list[ChapterBlock]:
    lines = text.splitlines()
    if not any(_has_heading(line) for line in lines):
        return [ChapterBlock(title="Full Text", text=text.strip())]

    blocks: list[ChapterBlock] = []
    current_title = "Front Matter"
    current_lines: list[str] = []
    saw_heading = False

    for line in lines:
        if _has_heading(line):
            saw_heading = True
            if current_lines:
                blocks.append(ChapterBlock(title=current_title, text="\n".join(current_lines).strip()))
            current_title = line.strip()
            current_lines = [line]
            continue

        current_lines.append(line)

    if current_lines:
        blocks.append(ChapterBlock(title=current_title, text="\n".join(current_lines).strip()))

    cleaned = [block for block in blocks if block.text]
    if not saw_heading or not cleaned:
        return [ChapterBlock(title="Full Text", text=text.strip())]
    return cleaned


def _count_non_empty_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def _iter_book_payload_batches(
    conn: sqlite3.Connection,
    book_ids: set[int] | None,
    fetch_size: int,
):
    params: list[object] = []
    conditions: list[str] = []
    content_expr = "COALESCE(c.clean_content, c.raw_content, '')"
    if book_ids:
        placeholders = ",".join(["?"] * len(book_ids))
        conditions.append(f"b.id IN ({placeholders})")
        params.extend(sorted(book_ids))
    else:
        # Full scans can skip books that do not have any stored content.
        conditions.append("c.bookid IS NOT NULL")
        # Prefilter rows so Python scans only books that mention chapter-like keywords.
        keyword_conditions = " OR ".join(f"{content_expr} LIKE ?" for _ in CHAPTER_SQL_KEYWORDS)
        conditions.append(f"({keyword_conditions})")
        params.extend([f"%{keyword}%" for keyword in CHAPTER_SQL_KEYWORDS])
    clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            b.id AS book_id,
            b.gutenbergbookid,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title,
            EXISTS(SELECT 1 FROM book_audio ba WHERE ba.book_id = b.id)
                OR EXISTS(SELECT 1 FROM book_audio_chapters bac WHERE bac.book_id = b.id) AS has_audio,
            COALESCE(c.clean_content, c.raw_content, '') AS text
        FROM books b
        LEFT JOIN book_contents c ON c.bookid = b.id
        {clause}
        ORDER BY b.id
        """,
        params,
    )
    batch_size = max(1, fetch_size)
    try:
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield [_row_to_payload(row) for row in rows]
    finally:
        try:
            cur.close()
        except sqlite3.ProgrammingError:
            # Cursor/connection might already be closed when scan exits early.
            pass


def _is_short_chapter_book(
    text: str,
    min_lines: int,
    *,
    collect_full_counts: bool,
) -> tuple[bool, list[int]]:
    # Fast reject for books that do not even mention chapter/book/part/section.
    if not CHAPTER_KEYWORD_RE.search(text):
        return False, []

    counts: list[int] = []
    current_count = 0
    saw_heading = False
    has_front_matter = False

    for line in text.splitlines():
        stripped = line.strip()
        is_heading = bool(stripped and CHAPTER_HEADING_RE.match(stripped))
        if is_heading:
            if saw_heading:
                # Finish the previous detected chapter block.
                counts.append(current_count)
                if current_count >= min_lines and not collect_full_counts:
                    return False, []
            elif current_count > 0:
                # Keep pre-heading content separately as front matter.
                counts.append(current_count)
                has_front_matter = True
            saw_heading = True
            current_count = 1  # heading line itself
            continue

        if stripped:
            current_count += 1

    if not saw_heading:
        return False, []

    if current_count <= 0:
        return False, counts if collect_full_counts else []

    # Final chapter block at EOF.
    counts.append(current_count)
    if current_count >= min_lines and not collect_full_counts:
        return False, []

    chapter_counts = counts[1:] if has_front_matter else counts
    if len(chapter_counts) < 2:
        return False, counts if collect_full_counts else []

    is_bad = all(count < min_lines for count in chapter_counts)
    return is_bad, counts if collect_full_counts or is_bad else []


def _scan_row_payload(
    payload: tuple[int, int | None, str, str, bool],
    min_lines: int,
    collect_full_counts: bool,
    delete_books_with_audio: bool,
) -> ScanOutcome:
    book_id, gutenberg_id, title, text, has_audio = payload
    if has_audio and not delete_books_with_audio:
        return ScanOutcome(
            book_id=book_id,
            gutenberg_id=gutenberg_id,
            title=title,
            skipped_empty=False,
            skipped_audio=True,
            is_bad=False,
            line_counts=[],
        )
    raw_text = str(text or "")
    if not raw_text or raw_text.isspace():
        return ScanOutcome(
            book_id=book_id,
            gutenberg_id=gutenberg_id,
            title=title,
            skipped_empty=True,
            skipped_audio=False,
            is_bad=False,
            line_counts=[],
        )
    is_bad, counts = _is_short_chapter_book(
        raw_text,
        min_lines,
        collect_full_counts=collect_full_counts,
    )
    return ScanOutcome(
        book_id=book_id,
        gutenberg_id=gutenberg_id,
        title=title,
        skipped_empty=False,
        skipped_audio=False,
        is_bad=is_bad,
        line_counts=counts,
    )


def _scan_row_task(task: tuple[tuple[int, int | None, str, str, bool], int, bool, bool]) -> ScanOutcome:
    payload, min_lines, collect_full_counts, delete_books_with_audio = task
    return _scan_row_payload(payload, min_lines, collect_full_counts, delete_books_with_audio)


def _row_to_payload(row: sqlite3.Row) -> tuple[int, int | None, str, str, bool]:
    return (
        int(row["book_id"]),
        int(row["gutenbergbookid"]) if row["gutenbergbookid"] is not None else None,
        str(row["title"] or "Untitled"),
        str(row["text"] or ""),
        bool(row["has_audio"]),
    )


def _iter_scan_outcomes(
    payload_batches,
    min_lines: int,
    workers: int,
    worker_chunk_size: int,
    collect_full_counts: bool,
    delete_books_with_audio: bool,
):
    if workers <= 1:
        for payload_batch in payload_batches:
            for payload in payload_batch:
                yield _scan_row_payload(payload, min_lines, collect_full_counts, delete_books_with_audio)
        return

    chunk_size = max(1, worker_chunk_size)
    try:
        try:
            ctx = mp.get_context("fork")
        except ValueError:
            ctx = mp.get_context()

        with ctx.Pool(processes=workers) as pool:
            for payload_batch in payload_batches:
                if not payload_batch:
                    continue
                tasks = (
                    (payload, min_lines, collect_full_counts, delete_books_with_audio)
                    for payload in payload_batch
                )
                yield from pool.imap(_scan_row_task, tasks, chunksize=chunk_size)
        return
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            f"warning: multiprocessing workers unavailable ({exc}); "
            f"falling back to thread workers={workers}"
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for payload_batch in payload_batches:
            if not payload_batch:
                continue
            yield from executor.map(
                _scan_row_payload,
                payload_batch,
                repeat(min_lines),
                repeat(collect_full_counts),
                repeat(delete_books_with_audio),
            )


def _delete_book(conn: sqlite3.Connection, book_id: int) -> dict[str, int]:
    cur = conn.cursor()
    deleted: dict[str, int] = {}
    for table, column in DELETE_TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (book_id,))
        count = int(cur.fetchone()[0] or 0)
        if count:
            cur.execute(f"DELETE FROM {table} WHERE {column} = ?", (book_id,))
        deleted[table] = count
    return deleted


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    if args.worker_chunk_size < 1:
        raise SystemExit("--worker-chunk-size must be >= 1")
    if args.commit_every < 1:
        raise SystemExit("--commit-every must be >= 1")
    if args.db_fetch_size < 1:
        raise SystemExit("--db-fetch-size must be >= 1")
    if args.scan_limit is not None and args.scan_limit < 1:
        raise SystemExit("--scan-limit must be >= 1")

    conn = _connect_db(args.db_path)
    payload_batches = None
    try:
        payload_batches = _iter_book_payload_batches(
            conn,
            set(args.book_ids) if args.book_ids else None,
            args.db_fetch_size,
        )
        matched = 0
        scanned = 0
        deleted = 0
        skipped_audio = 0
        pending_commits = 0

        if args.workers > 1:
            print(f"scan mode: parallel workers={args.workers} chunk_size={args.worker_chunk_size}")

        for outcome in _iter_scan_outcomes(
            payload_batches,
            args.min_lines,
            args.workers,
            args.worker_chunk_size,
            args.verbose,
            args.delete_books_with_audio,
        ):
            if args.scan_limit is not None and scanned >= args.scan_limit:
                break
            scanned += 1
            if outcome.skipped_audio:
                skipped_audio += 1
                if args.verbose:
                    print(f"scan book_id={outcome.book_id} skipped has-audio")
                continue
            if outcome.skipped_empty:
                if args.verbose:
                    print(f"scan book_id={outcome.book_id} skipped empty text")
                continue

            if not outcome.is_bad:
                if args.verbose:
                    print(
                        f"scan book_id={outcome.book_id} ok "
                        f"chapters={len(outcome.line_counts)} counts={outcome.line_counts}"
                    )
                continue

            matched += 1
            print(
                f"{'would delete' if args.dry_run else 'deleting'} "
                f"book_id={outcome.book_id} gutenberg_id={outcome.gutenberg_id} title={outcome.title!r} "
                f"chapter_lines={outcome.line_counts}"
            )

            if not args.dry_run:
                _delete_book(conn, outcome.book_id)
                deleted += 1
                pending_commits += 1
                if pending_commits >= args.commit_every:
                    conn.commit()
                    pending_commits = 0

            if args.limit is not None and matched >= args.limit:
                break

        if not args.dry_run and pending_commits:
            conn.commit()

        print(
            f"completed scanned={scanned} skipped_audio={skipped_audio} matched={matched} "
            f"{'deleted=' + str(deleted) if not args.dry_run else 'deleted=0 (dry-run)'}"
        )
        return 0
    finally:
        if payload_batches is not None and hasattr(payload_batches, "close"):
            try:
                payload_batches.close()
            except Exception:
                pass
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
