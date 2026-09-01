from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from AudioBooks.MediaPlayer.Repository.MediaHistoryRepository import MediaHistoryRepository


media_history_bp = Blueprint("media_history", __name__)


class MediaHistoryService:
    def __init__(self, repository: MediaHistoryRepository | None = None):
        self.media_history_repository = repository or MediaHistoryRepository()

    def get_recently_played(self, user_id: int, limit: int = 10):
        return self.media_history_repository.get_history(user_id, limit=limit)

    def get_last_played(self, user_id: int):
        return self.media_history_repository.get_last_played(user_id)

    def get_media_position(self, user_id: int, book_id: int):
        return self.media_history_repository.get_media_position(user_id, book_id)

    def save_recently_played(self, user_id: int, book_id: int, media_type: str, position):
        return self.media_history_repository.save_history(user_id, book_id, media_type, position)

    def clear_recently_played(self, user_id: int):
        self.media_history_repository.clear_history(user_id)


media_history_service = MediaHistoryService()


def _current_user_id():
    user_id = session.get("user_id")
    return int(user_id) if user_id is not None else None


@media_history_bp.route("/api/media-history/recent", methods=["GET"])
def get_recently_played():
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401
    limit = request.args.get("limit", default=10, type=int)
    history = media_history_service.get_recently_played(user_id, limit=limit)
    return jsonify({"items": [item.to_dict() for item in history]})


@media_history_bp.route("/api/media-history/last", methods=["GET"])
def get_last_played():
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401
    last_item = media_history_service.get_last_played(user_id)
    if last_item is None:
        return jsonify({"item": None})
    return jsonify({"item": last_item.to_dict()})


@media_history_bp.route("/api/media-history/books/<int:book_id>", methods=["GET"])
def get_media_position(book_id: int):
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401
    item = media_history_service.get_media_position(user_id, book_id)
    if item is None:
        return jsonify({"item": None})
    return jsonify({"item": item.to_dict()})


@media_history_bp.route("/api/media-history/books/<int:book_id>", methods=["POST"])
def save_media_position(book_id: int):
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    media_type = str(data.get("media_type") or "audio").strip().lower()
    position = data.get("position") or {}
    item = media_history_service.save_recently_played(user_id, book_id, media_type, position)
    return jsonify({"item": item.to_dict()})


@media_history_bp.route("/api/media-history/recent", methods=["DELETE"])
def clear_recently_played():
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401
    media_history_service.clear_recently_played(user_id)
    return jsonify({"message": "Recently played history cleared"})
