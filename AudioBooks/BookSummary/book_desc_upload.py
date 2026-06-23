"""
Upload book_desc and the gutenberg-id → book_id map to GCS.

GCS layout:
  gs://<bucket>/book-desc/<bookid>.json         per-book title/author/summary
  gs://<bucket>/book-desc/gutenberg-id-map.json  {str(gutenberg_id): book_id, ...}

Run book_contents_upload.py first to upload content, then this script to upload
descriptions. After both uploads, summarizer.py needs no SQLite.

Usage:
  # Check prior run state
  python AudioBooks/BookSummary/book_desc_upload.py --status

  # Dry-run to preview scope
  python AudioBooks/BookSummary/book_desc_upload.py --dry-run --limit 20

  # Full upload
  python AudioBooks/BookSummary/book_desc_upload.py --workers 8 --chunk-size 200

  # Resume after interruption
  python AudioBooks/BookSummary/book_desc_upload.py --workers 8 --max-attempts 5

  # Force re-upload specific books
  python AudioBooks/BookSummary/book_desc_upload.py --book-ids 48907,12345 --force

  # Full re-run from scratch
  python AudioBooks/BookSummary/book_desc_upload.py --force --reset-queue --workers 8
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from google.cloud import storage

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "AudioBooks" / "Catalog" / "DB" / "gutenbergindex.db"
GCS_BUCKET = os.environ.get("GCS_BUCKET")
GCS_DESC_PREFIX = "book-desc"
ID_MAP_BLOB = f"{GCS_DESC_PREFIX}/gutenberg-id-map.json"
CREDENTIALS_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
QUEUE_KEY = "desc:v1"


# ---------------------------------------------------------------------------
# GCS client
# ---------------------------------------------------------------------------

def _make_gcs_client():
    if CREDENTIALS_PATH:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(CREDENTIALS_PATH)
        return storage.Client(credentials=creds)
    return storage.Client()


# ---------------------------------------------------------------------------
# DB / queue helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_queue_table(db_path: str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS book_desc_upload_queue (
                queue_key TEXT NOT NULL,
                bookid    INTEGER NOT NULL,
                status    TEXT NOT NULL DEFAULT 'pending',
                attempts  INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (queue_key, bookid)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bduq_status"
            " ON book_desc_upload_queue(queue_key, status)"
        )
        conn.commit()
    finally:
        conn.close()


def _queue_status(db_path: str) -> dict[str, int]:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT status, COUNT(*) FROM book_desc_upload_queue"
            " WHERE queue_key = ? GROUP BY status",
            (QUEUE_KEY,),
        )
        return {row[0]: row[1] for row in cur}
    finally:
        conn.close()


def _reset_queue(db_path: str) -> int:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM book_desc_upload_queue WHERE queue_key = ?",
            (QUEUE_KEY,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _load_target_rows(
    db_path: str,
    book_ids: list[int] | None,
    limit: int | None,
    force: bool,
) -> list[dict]:
    conn = _connect(db_path)
    try:
        conditions: list[str] = []
        params: list = []

        if not force:
            conditions.append(
                "NOT EXISTS ("
                "  SELECT 1 FROM book_desc_upload_queue q"
                "  WHERE q.queue_key = ? AND q.bookid = bd.bookid AND q.status = 'done'"
                ")"
            )
            params.append(QUEUE_KEY)

        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            conditions.append(f"bd.bookid IN ({placeholders})")
            params.extend(book_ids)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = f"LIMIT {limit}" if limit else ""
        cur = conn.execute(
            f"SELECT bd.bookid, bd.source_title, bd.source_author, bd.summary"
            f" FROM book_desc bd {where} ORDER BY bd.bookid {limit_clause}",
            params,
        )
        return [dict(row) for row in cur]
    finally:
        conn.close()


def _load_gutenberg_id_map(db_path: str) -> dict[str, int]:
    """Return {str(gutenbergbookid): internal_book_id} for all books."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT id, gutenbergbookid FROM books WHERE gutenbergbookid IS NOT NULL"
        )
        return {str(int(row["gutenbergbookid"])): int(row["id"]) for row in cur}
    finally:
        conn.close()


def _seed_queue(db_path: str, rows: list[dict]) -> None:
    conn = _connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO book_desc_upload_queue (queue_key, bookid, status, attempts)
            VALUES (?, ?, 'pending', 0)
            ON CONFLICT(queue_key, bookid) DO UPDATE SET
                status = CASE WHEN status IN ('done', 'skipped') THEN status ELSE 'pending' END,
                updated_at = CURRENT_TIMESTAMP
            """,
            [(QUEUE_KEY, row["bookid"]) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _load_pending(
    db_path: str,
    max_attempts: int,
    only_book_ids: set[int] | None = None,
) -> list[int]:
    conn = _connect(db_path)
    try:
        query = (
            "SELECT bookid FROM book_desc_upload_queue"
            " WHERE queue_key = ? AND status IN ('pending', 'failed') AND attempts < ?"
        )
        params: list = [QUEUE_KEY, max_attempts]
        if only_book_ids:
            placeholders = ",".join("?" * len(only_book_ids))
            query += f" AND bookid IN ({placeholders})"
            params.extend(only_book_ids)
        query += " ORDER BY bookid"
        return [int(row[0]) for row in conn.execute(query, params)]
    finally:
        conn.close()


def _update_queue_status(
    db_path: str,
    bookid: int,
    *,
    status: str,
    attempt_delta: int = 0,
    last_error: str | None = None,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE book_desc_upload_queue
            SET status = ?,
                attempts = attempts + ?,
                last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE queue_key = ? AND bookid = ?
            """,
            (status, attempt_delta, last_error, QUEUE_KEY, bookid),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-book upload
# ---------------------------------------------------------------------------

def _upload_desc(bucket, row: dict) -> tuple[int, str, str]:
    """Upload one book_desc row as JSON to GCS. Returns (bookid, status, msg)."""
    bookid = int(row["bookid"])
    payload = json.dumps(
        {
            "bookid": bookid,
            "source_title": row["source_title"] or "Untitled",
            "source_author": row["source_author"] or "",
            "summary": row["summary"] or "",
        },
        ensure_ascii=False,
    )
    blob_path = f"{GCS_DESC_PREFIX}/{bookid}.json"
    try:
        bucket.blob(blob_path).upload_from_string(payload, content_type="application/json")
        return bookid, "done", blob_path
    except Exception as exc:
        return bookid, "error", str(exc)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def upload_book_descs(
    db_path: str,
    bucket_name: str,
    *,
    book_ids: list[int] | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    reset_queue: bool = False,
    status_only: bool = False,
    workers: int = 4,
    chunk_size: int = 100,
    max_attempts: int = 3,
    progress_every: int = 50,
) -> None:
    print(f"stage: initialising db={db_path}", flush=True)
    _ensure_queue_table(db_path)

    prior = _queue_status(db_path)
    if prior:
        counts = "  ".join(f"{s}={n}" for s, n in sorted(prior.items()))
        print(f"summary: prior_queue  {counts}", flush=True)

    if status_only:
        if not prior:
            print("summary: queue is empty (no prior run)", flush=True)
        return

    if reset_queue:
        n = _reset_queue(db_path)
        print(f"stage: queue reset — deleted {n} rows", flush=True)

    print("stage: discovering rows in book_desc", flush=True)
    rows = _load_target_rows(db_path, book_ids, limit, force)
    print(f"summary: target_books={len(rows)}", flush=True)

    if not rows:
        print("done: nothing to do", flush=True)
        return

    _seed_queue(db_path, rows)

    restrict = bool(book_ids or limit)
    only_ids = {row["bookid"] for row in rows} if restrict else None
    pending_ids = set(_load_pending(db_path, max_attempts, only_ids))
    pending_rows = [r for r in rows if r["bookid"] in pending_ids]
    already_done = len(rows) - len(pending_rows)
    print(
        f"summary: queued={len(pending_rows)}  already_done={already_done}  max_attempts={max_attempts}",
        flush=True,
    )

    if not pending_rows:
        print("done: queue is empty — all books already uploaded", flush=True)
        return

    if dry_run:
        print("stage: dry-run enabled; no writes will be committed", flush=True)
        for row in pending_rows[:5]:
            print(f"  gs://{bucket_name}/{GCS_DESC_PREFIX}/{row['bookid']}.json")
        if len(pending_rows) > 5:
            print(f"  ... and {len(pending_rows) - 5} more")
        print(f"  gs://{bucket_name}/{ID_MAP_BLOB}")
        return

    print(f"stage: connecting to GCS bucket '{bucket_name}'", flush=True)
    try:
        client = _make_gcs_client()
        bucket = client.bucket(bucket_name)
    except Exception as exc:
        print(f"ERROR: could not create GCS client: {exc}", flush=True)
        return

    print(
        f"stage: uploading book-desc with workers={workers} chunk_size={chunk_size}",
        flush=True,
    )

    uploaded = failed = 0
    total = len(pending_rows)
    t0 = time.monotonic()

    def _worker(row: dict) -> tuple[int, str, str]:
        return _upload_desc(bucket, row)

    for chunk_start in range(0, total, chunk_size):
        chunk = pending_rows[chunk_start : chunk_start + chunk_size]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker, row): row for row in chunk}
            for i, future in enumerate(as_completed(futures), 1):
                bookid, status, msg = future.result()
                if status == "done":
                    _update_queue_status(db_path, bookid, status="done")
                    uploaded += 1
                else:
                    _update_queue_status(
                        db_path, bookid, status="failed",
                        attempt_delta=1, last_error=msg[:500],
                    )
                    failed += 1
                    print(f"  ERROR book_id={bookid}: {msg}", flush=True)

                processed = chunk_start + i
                if processed % progress_every == 0 or processed == total:
                    elapsed = time.monotonic() - t0
                    rate = processed / elapsed if elapsed else 0
                    print(
                        f"stage: progress processed={processed}/{total}"
                        f" uploaded={uploaded} failed={failed}"
                        f" rate={rate:.1f}/s"
                        f" last={bookid} {status}: {msg[:80]}",
                        flush=True,
                    )

    # Upload the gutenberg-id map after all book-desc blobs are done
    print("stage: uploading gutenberg-id-map", flush=True)
    try:
        id_map = _load_gutenberg_id_map(db_path)
        bucket.blob(ID_MAP_BLOB).upload_from_string(
            json.dumps(id_map, ensure_ascii=False),
            content_type="application/json",
        )
        print(f"stage: uploaded {ID_MAP_BLOB} ({len(id_map):,} entries)", flush=True)
    except Exception as exc:
        print(f"ERROR: could not upload gutenberg-id-map: {exc}", flush=True)

    elapsed = time.monotonic() - t0
    print(
        f"done: total={total} uploaded={uploaded} failed={failed}"
        f" already_done={already_done} elapsed={elapsed:.1f}s",
        flush=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload book_desc rows and gutenberg-id map from gutenbergindex.db to GCS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=str(DB_PATH), help="Path to gutenbergindex.db")
    parser.add_argument("--bucket", default=GCS_BUCKET, help="GCS bucket name (default: GCS_BUCKET from .env)")
    parser.add_argument("--book-ids", default=None, metavar="IDS", help="Comma-separated internal book IDs")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="Stop after discovering N books")
    parser.add_argument("--force", action="store_true", help="Re-upload books already marked done in the queue")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading or writing queue rows")
    parser.add_argument("--reset-queue", action="store_true", help="Delete all queue rows before running")
    parser.add_argument("--status", action="store_true", help="Print queue state and exit without uploading")
    parser.add_argument("--workers", type=int, default=4, help="Parallel upload workers (default: 4)")
    parser.add_argument("--chunk-size", type=int, default=100, help="Books per executor batch (default: 100)")
    parser.add_argument("--max-attempts", type=int, default=3, help="Skip books with this many failures (default: 3)")
    parser.add_argument("--progress-every", type=int, default=50, help="Print progress every N books (default: 50)")
    args = parser.parse_args()

    if not args.status and not args.bucket:
        raise ValueError("GCS_BUCKET is not set. Add it to .env or pass --bucket.")

    book_ids = [int(x) for x in args.book_ids.split(",")] if args.book_ids else None

    upload_book_descs(
        args.db,
        args.bucket or "",
        book_ids=book_ids,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        reset_queue=args.reset_queue,
        status_only=args.status,
        workers=args.workers,
        chunk_size=args.chunk_size,
        max_attempts=args.max_attempts,
        progress_every=args.progress_every,
    )


if __name__ == "__main__":
    main()
