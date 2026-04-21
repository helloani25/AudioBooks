from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"

AUDIO_FORMAT_PRIORITY = [
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "audio/x-wav",
    "audio/x-ms-wma",
    "audio/midi",
]

AUDIO_PACKAGE_SUFFIXES = (
    "-mp3.zip",
    "-m4b.zip",
    "-ogg.zip",
    "-mid.zip",
    "-midi.zip",
    "-wav.zip",
    "-wma.zip",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill book_audio and book_audio_chapters from Gutenberg download links.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to the SQLite database.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after processing this many books.")
    parser.add_argument(
        "--gutenberg-id",
        action="append",
        type=int,
        dest="gutenberg_ids",
        help="Only process the specified Gutenberg ids. Can be repeated.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written without writing.")
    return parser.parse_args()


def _connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _chunked(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _ensure_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("CREATE INDEX IF NOT EXISTS books_gutenbergbookid_idx ON books(gutenbergbookid)")
    cur.execute("CREATE INDEX IF NOT EXISTS downloadlinks_bookid_idx ON downloadlinks(bookid)")
    cur.execute("CREATE INDEX IF NOT EXISTS downloadlinks_downloadtypeid_idx ON downloadlinks(downloadtypeid)")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS book_audio (
            book_id INTEGER PRIMARY KEY,
            package_url TEXT NOT NULL,
            audio_format TEXT NOT NULL,
            track_count INTEGER NOT NULL DEFAULT 0,
            is_chaptered INTEGER NOT NULL DEFAULT 0,
            download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS book_audio_chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            track_order INTEGER NOT NULL,
            chapter_title TEXT,
            track_url TEXT NOT NULL,
            audio_format TEXT NOT NULL,
            download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(book_id, track_order, track_url)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_book_audio_chapters_book_id ON book_audio_chapters(book_id)")
    conn.commit()


def _track_order_from_url(url: str, fallback: int) -> int:
    filename = Path(urlsplit(url).path).name
    match = re.search(r"(\d+)(?=\.[^.]+$)", filename)
    if match:
        return int(match.group(1))
    match = re.search(r"-(\d+)(?:\.[^.]+)?$", filename)
    if match:
        return int(match.group(1))
    return fallback


def _is_audio_package(url: str, download_type: str) -> bool:
    name = Path(urlsplit(url).path).name.lower()
    return download_type == "application/octet-stream" and any(name.endswith(suffix) for suffix in AUDIO_PACKAGE_SUFFIXES)


def _track_name(url: str) -> str:
    return Path(urlsplit(url).path).name


def _preview_tracks(tracks: list[dict], limit: int = 3) -> str:
    if not tracks:
        return "[]"
    names = [_track_name(track["track_url"]) for track in tracks[:limit]]
    remaining = len(tracks) - len(names)
    preview = ", ".join(names)
    if remaining > 0:
        preview += f", ... (+{remaining} more)"
    return f"[{preview}]"


def _load_audio_books(conn: sqlite3.Connection, gutenberg_ids: list[int] | None) -> list[dict]:
    cur = conn.cursor()
    params: list[object] = []
    id_clause = ""
    if gutenberg_ids:
        placeholders = ",".join(["?"] * len(gutenberg_ids))
        id_clause = f"AND b.gutenbergbookid IN ({placeholders})"
        params.extend(gutenberg_ids)

    cur.execute(
        f"""
        SELECT DISTINCT
            b.gutenbergbookid,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
        FROM books b
        JOIN downloadlinks d ON d.bookid = b.id
        JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
        WHERE b.gutenbergbookid IS NOT NULL
          AND (
              dt.name LIKE 'audio/%'
              OR (
                  dt.name = 'application/octet-stream'
                  AND (
                      lower(d.name) LIKE '%-mp3.zip'
                      OR lower(d.name) LIKE '%-m4b.zip'
                      OR lower(d.name) LIKE '%-ogg.zip'
                      OR lower(d.name) LIKE '%-mid.zip'
                      OR lower(d.name) LIKE '%-midi.zip'
                      OR lower(d.name) LIKE '%-wav.zip'
                      OR lower(d.name) LIKE '%-wma.zip'
                  )
              )
          )
          {id_clause}
        ORDER BY b.gutenbergbookid
        """,
        params,
    )
    book_rows = cur.fetchall()
    book_ids = [int(row[0]) for row in book_rows]
    titles_by_book = {int(book_id): title for book_id, title in book_rows}

    grouped: dict[int, dict] = {
        book_id: {"book_id": book_id, "title": titles_by_book[book_id], "packages": [], "formats": {}}
        for book_id in book_ids
    }

    def query_chunk(chunk: list[int]) -> list[tuple[int, str, str]]:
        if not chunk:
            return []
        chunk_placeholders = ",".join(["?"] * len(chunk))
        cur.execute(
            f"""
            SELECT
                b.gutenbergbookid,
                d.name AS url,
                dt.name AS download_type
            FROM books b
            JOIN downloadlinks d ON d.bookid = b.id
            JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
            WHERE b.gutenbergbookid IN ({chunk_placeholders})
              AND (
                  dt.name LIKE 'audio/%'
                  OR (
                      dt.name = 'application/octet-stream'
                      AND (
                          lower(d.name) LIKE '%-mp3.zip'
                          OR lower(d.name) LIKE '%-m4b.zip'
                          OR lower(d.name) LIKE '%-ogg.zip'
                          OR lower(d.name) LIKE '%-mid.zip'
                          OR lower(d.name) LIKE '%-midi.zip'
                          OR lower(d.name) LIKE '%-wav.zip'
                          OR lower(d.name) LIKE '%-wma.zip'
                      )
                  )
              )
            ORDER BY b.gutenbergbookid, d.name
            """,
            chunk,
        )
        return [(int(book_id), url, download_type) for book_id, url, download_type in cur.fetchall()]

    for chunk in _chunked(book_ids, 200):
        for book_id, url, download_type in query_chunk(chunk):
            entry = grouped[book_id]
            if _is_audio_package(url, download_type):
                entry["packages"].append((url, download_type))
            elif download_type.startswith("audio/"):
                entry["formats"].setdefault(download_type, []).append(url)

    results: list[dict] = []
    for entry in grouped.values():
        primary_format = None
        primary_tracks: list[str] = []
        for audio_format in AUDIO_FORMAT_PRIORITY:
            tracks = entry["formats"].get(audio_format, [])
            if tracks:
                primary_format = audio_format
                primary_tracks = tracks
                break
        if primary_format is None:
            # Keep anything audio-like if the catalog uses an unexpected MIME type.
            for audio_format, tracks in entry["formats"].items():
                primary_format = audio_format
                primary_tracks = tracks
                break

        if not primary_format:
            continue

        package_url = entry["packages"][0][0] if entry["packages"] else primary_tracks[0]
        tracks = []
        for fallback_index, url in enumerate(primary_tracks, start=1):
            tracks.append(
                {
                    "track_order": _track_order_from_url(url, fallback_index),
                    "chapter_title": None,
                    "track_url": url,
                    "audio_format": primary_format,
                }
            )
        tracks.sort(key=lambda item: (item["track_order"], item["track_url"]))

        results.append(
            {
                "book_id": entry["book_id"],
                "title": entry["title"],
                "package_url": package_url,
                "audio_format": primary_format,
                "track_count": len(tracks),
                "is_chaptered": len(tracks) > 1,
                "tracks": tracks,
            }
        )

    results.sort(key=lambda item: item["book_id"])
    return results


def _save_audio(conn: sqlite3.Connection, item: dict) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO book_audio (
            book_id,
            package_url,
            audio_format,
            track_count,
            is_chaptered,
            download_date
        )
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            item["book_id"],
            item["package_url"],
            item["audio_format"],
            item["track_count"],
            1 if item["is_chaptered"] else 0,
        ),
    )
    cur.execute("DELETE FROM book_audio_chapters WHERE book_id = ?", (item["book_id"],))
    for track in item["tracks"]:
        cur.execute(
            """
            INSERT OR REPLACE INTO book_audio_chapters (
                book_id,
                track_order,
                chapter_title,
                track_url,
                audio_format,
                download_date
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                item["book_id"],
                track["track_order"],
                track["chapter_title"],
                track["track_url"],
                track["audio_format"],
            ),
        )
    conn.commit()


def backfill(db_path: str, limit: int | None, gutenberg_ids: list[int] | None, dry_run: bool) -> None:
    conn = _connect_db(db_path)
    _ensure_tables(conn)

    print("stage: scanning catalog for audio books", flush=True)
    items = _load_audio_books(conn, gutenberg_ids)
    if limit is not None:
        items = items[:limit]

    print("stage: resolving audio packages and tracks", flush=True)
    print(f"summary: audio_books={len(items)}", flush=True)
    print("stage: persisting audio metadata", flush=True)
    written = 0
    for item in items:
        print(
            f"audio {item['book_id']}: package={item['package_url']} "
            f"tracks={item['track_count']} chaptered={item['is_chaptered']} "
            f"format={item['audio_format']} ({item['title']})",
            flush=True,
        )
        if item["track_count"]:
            print(f"  track preview: {_preview_tracks(item['tracks'])}", flush=True)
        if not dry_run:
            _save_audio(conn, item)
            written += 1
            chapter_table = "book_audio_chapters" if item["is_chaptered"] else "book_audio"
            extra = f" and {chapter_table}({item['track_count']} rows)" if item["is_chaptered"] else ""
            print(f"saved book_audio(book_id={item['book_id']}){extra}", flush=True)
        else:
            chapter_table = "book_audio_chapters" if item["is_chaptered"] else "book_audio"
            extra = f" and {chapter_table}({item['track_count']} rows)" if item["is_chaptered"] else ""
            print(f"dry-run would save book_audio(book_id={item['book_id']}){extra}", flush=True)

    if dry_run:
        print(f"done: previewed={len(items)}", flush=True)
    else:
        print(f"done: written={written}", flush=True)
    conn.close()


def main() -> None:
    args = parse_args()
    backfill(args.db_path, args.limit, args.gutenberg_ids, args.dry_run)


if __name__ == "__main__":
    main()
