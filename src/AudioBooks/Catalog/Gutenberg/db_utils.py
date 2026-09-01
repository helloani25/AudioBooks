from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


def connect_db(db_path: str) -> sqlite3.Connection:
    """Open SQLite with a long timeout so catalog backfills can coexist safely."""
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def with_sqlite_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 8,
    initial_delay: float = 0.5,
) -> T:
    """Retry SQLite writes when the database is temporarily locked or busy."""
    delay = initial_delay
    last_error: sqlite3.OperationalError | None = None
    for _ in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            last_error = exc
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
    if last_error is not None:
        raise last_error
    raise sqlite3.OperationalError("database is locked")


def ensure_book_contents_table(db_path: str) -> None:
    """Create the canonical text-content table used by all content backfills."""
    def query() -> None:
        conn = connect_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS book_contents (
                    bookid INTEGER PRIMARY KEY,
                    raw_content TEXT NOT NULL,
                    clean_content TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'text',
                    download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(query)


def migrate_book_contents_content_type(db_path: str) -> None:
    """Add content_type column to book_contents if it doesn't exist yet."""
    def query() -> None:
        conn = connect_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(book_contents)")
            cols = {row[1] for row in cur.fetchall()}
            if "content_type" not in cols:
                cur.execute(
                    "ALTER TABLE book_contents ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'"
                )
                conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(query)


def migrate_book_contents_html_content(db_path: str) -> None:
    """Add html_content and has_images columns to book_contents if missing."""
    def query() -> None:
        conn = connect_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(book_contents)")
            cols = {row[1] for row in cur.fetchall()}
            if "html_content" not in cols:
                cur.execute("ALTER TABLE book_contents ADD COLUMN html_content TEXT")
            if "has_images" not in cols:
                cur.execute(
                    "ALTER TABLE book_contents ADD COLUMN has_images INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(query)


def ensure_book_cover_art_table(db_path: str) -> None:
    """Create the cover-art table used by the Gutenberg art backfill."""
    def query() -> None:
        conn = connect_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS book_cover_art (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bookid INTEGER NOT NULL,
                    size_label TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    image_url TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    byte_size INTEGER,
                    rdf_url TEXT,
                    source TEXT NOT NULL DEFAULT 'gutenberg-rdf',
                    download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(bookid, size_label, image_url)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_book_cover_art_bookid ON book_cover_art(bookid)")
            conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(query)


def ensure_book_content_backfill_tables(db_path: str) -> None:
    """Create queue and discovery-cache tables for resumable text backfills."""
    def query() -> None:
        conn = connect_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS book_content_backfill_queue (
                    queue_key TEXT NOT NULL,
                    bookid INTEGER NOT NULL,
                    gutenbergbookid INTEGER,
                    title TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    source_bookid INTEGER,
                    source_url TEXT,
                    source_type TEXT,
                    download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (queue_key, bookid)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gutenberg_discovery_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_backfill_queue_status ON book_content_backfill_queue(queue_key, status, priority)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_backfill_queue_gutenbergbookid ON book_content_backfill_queue(gutenbergbookid)"
            )
            conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(query)
