from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def with_sqlite_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 8,
    initial_delay: float = 0.5,
) -> T:
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
                    download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(query)
