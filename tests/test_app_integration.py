from __future__ import annotations

import base64
from pathlib import Path

import pytest


CATALOG_DB = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "AudioBooks"
    / "Catalog"
    / "DB"
    / "gutenbergindex.db"
)

pytestmark = pytest.mark.skipif(
    not CATALOG_DB.is_file(),
    reason="The local Gutenberg catalog database has not been restored.",
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")

    import AudioBooks.app as app_module
    import AudioBooks.MediaPlayer.Service.MediaHistoryService as media_module
    from AudioBooks.Authentication.Repository.UserRepository import UserRepository
    from AudioBooks.MediaPlayer.Repository.MediaHistoryRepository import MediaHistoryRepository
    from AudioBooks.MediaPlayer.Service.MediaHistoryService import MediaHistoryService

    app_module.user_repo = UserRepository(str(tmp_path / "users.db"))
    media_module.media_history_service = MediaHistoryService(
        MediaHistoryRepository(str(tmp_path / "media_history.db")),
    )
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _csrf_token(client) -> str:
    response = client.get("/api/csrf-token")
    assert response.status_code == 200
    return response.get_json()["csrf_token"]


def test_catalog_endpoints_present_downloaded_books(client):
    response = client.get("/api/books?limit=2&offset=0")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] > 0
    assert len(payload["books"]) == 2

    book_id = payload["books"][0]["id"]
    description = client.get(f"/api/books/{book_id}/description")
    assert description.status_code == 200
    assert description.get_json()["bookid"] == book_id

    cover_art = client.get(f"/api/books/{book_id}/cover-art")
    assert cover_art.status_code == 200
    assert "covers" in cover_art.get_json()

    subjects = client.get("/api/subjects?limit=3")
    assert subjects.status_code == 200
    assert len(subjects.get_json()) == 3


def test_signup_login_session_and_media_history(client):
    token = _csrf_token(client)
    signup = client.post(
        "/api/signup",
        json={
            "email": "reader@example.com",
            "password": "correct-horse-battery-staple",
            "confirm_password": "correct-horse-battery-staple",
            "full_name": "Test Reader",
        },
        headers={"X-CSRFToken": token},
    )
    assert signup.status_code == 201

    basic = base64.b64encode(
        b"reader@example.com:correct-horse-battery-staple",
    ).decode("ascii")
    login = client.post(
        "/api/login",
        headers={"Authorization": f"Basic {basic}", "X-CSRFToken": token},
    )
    assert login.status_code == 200
    assert client.get("/api/me").status_code == 200
    assert client.get("/api/media-history/last").get_json() == {"item": None}
    assert client.get("/api/media-history/books/1").get_json() == {"item": None}

    token = _csrf_token(client)
    saved = client.post(
        "/api/media-history/books/1",
        json={"media_type": "text", "position": {"page": 12}},
        headers={"X-CSRFToken": token},
    )
    assert saved.status_code == 200

    recent = client.get("/api/media-history/recent")
    assert recent.status_code == 200
    assert recent.get_json()["items"][0]["position"]["page"] == 12

    logout = client.post("/api/logout", headers={"X-CSRFToken": token})
    assert logout.status_code == 200
    assert client.get("/api/me").status_code == 401
