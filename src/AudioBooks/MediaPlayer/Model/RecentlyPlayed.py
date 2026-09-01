from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import json


@dataclass(slots=True)
class RecentlyPlayed:
    """Represents a single user's last played or read book state."""

    user_id: int
    book_id: int
    media_type: str
    position: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None
    id: int | None = None

    def normalized_media_type(self) -> str:
        media_type = str(self.media_type or "").strip().lower()
        return media_type if media_type in {"audio", "text"} else "audio"

    def position_json(self) -> str:
        return json.dumps(self.position or {}, separators=(",", ":"), sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "book_id": self.book_id,
            "media_type": self.normalized_media_type(),
            "position": self.position or {},
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row) -> "RecentlyPlayed":
        raw_position = row["position"] if "position" in row.keys() else "{}"
        try:
            position = json.loads(raw_position) if raw_position else {}
        except json.JSONDecodeError:
            position = {}
        return cls(
            id=row["id"] if "id" in row.keys() else None,
            user_id=int(row["user_id"]),
            book_id=int(row["book_id"]),
            media_type=str(row["media_type"] or "audio"),
            position=position if isinstance(position, dict) else {},
            updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
        )
