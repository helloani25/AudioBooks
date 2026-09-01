from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from typing import Any

from AudioBooks.MediaPlayer.Model.RecentlyPlayed import RecentlyPlayed


class MediaHistoryRepository:
    """Persist and query each user's recently played media state."""

    def __init__(self, db_path: str | None = None):
        current_dir = Path(__file__).resolve().parent
        default_path = current_dir.parent / "DB" / "media_history.db"
        self.db_path = Path(db_path) if db_path else default_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recently_played_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    book_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type IN ('audio', 'text')),
                    position TEXT NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, book_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recently_played_user_updated
                ON recently_played_media(user_id, updated_at DESC, id DESC)
                """
            )

    def _serialize_position(self, position: dict[str, Any] | str | None) -> str:
        if position is None:
            return "{}"
        if isinstance(position, str):
            text = position.strip()
            if not text:
                return "{}"
            try:
                json.loads(text)
                return text
            except json.JSONDecodeError:
                return json.dumps({"raw": text}, separators=(",", ":"), sort_keys=True)
        return json.dumps(position, separators=(",", ":"), sort_keys=True)

    def save_history(self, user_id: int, book_id: int, media_type: str, position: dict[str, Any] | str | None) -> RecentlyPlayed:
        record = RecentlyPlayed(
            user_id=user_id,
            book_id=book_id,
            media_type=media_type,
            position=json.loads(self._serialize_position(position)),
        )
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO recently_played_media (user_id, book_id, media_type, position, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, book_id) DO UPDATE SET
                    media_type = excluded.media_type,
                    position = excluded.position,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    record.user_id,
                    record.book_id,
                    record.normalized_media_type(),
                    record.position_json(),
                ),
            )
            cur.execute(
                """
                DELETE FROM recently_played_media
                WHERE id IN (
                    SELECT id
                    FROM recently_played_media
                    WHERE user_id = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT -1 OFFSET 10
                )
                """,
                (user_id,),
            )
            cur.execute(
                """
                SELECT id, user_id, book_id, media_type, position, updated_at
                FROM recently_played_media
                WHERE user_id = ? AND book_id = ?
                """,
                (user_id, book_id),
            )
            row = cur.fetchone()
        return RecentlyPlayed.from_row(row) if row else record

    def get_history(self, user_id: int, limit: int = 10) -> list[RecentlyPlayed]:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, book_id, media_type, position, updated_at
                FROM recently_played_media
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            return [RecentlyPlayed.from_row(row) for row in cur.fetchall()]

    def get_last_played(self, user_id: int) -> RecentlyPlayed | None:
        history = self.get_history(user_id, limit=1)
        return history[0] if history else None

    def get_media_position(self, user_id: int, book_id: int) -> RecentlyPlayed | None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, book_id, media_type, position, updated_at
                FROM recently_played_media
                WHERE user_id = ? AND book_id = ?
                """,
                (user_id, book_id),
            )
            row = cur.fetchone()
            return RecentlyPlayed.from_row(row) if row else None

    def clear_history(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM recently_played_media WHERE user_id = ?", (user_id,))
