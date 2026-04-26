from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from html.parser import HTMLParser
import json
import re
import sqlite3
import ssl
import sys
from urllib.error import HTTPError
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.db_utils import connect_db as _connect_db


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
MIRRORS_URL = "https://www.gutenberg.org/MIRRORS.ALL"
GUTENBERG_FILES_URL = "https://www.gutenberg.org/files"
LIBRIVOX_API_URL = "https://librivox.org/api/feed/audiobooks"
README_URL_TEMPLATE = "https://www.gutenberg.org/files/{gid}/{gid}-readme.txt"
CATALOG_UA = "AudioBooksCatalog/1.0 (Project Gutenberg audio index)"

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

AUDIO_PROBE_TIMEOUT_SECONDS = 30

LIVE_AUDIO_DOWNLOAD_TYPE_BY_SUFFIX = [
    ((".mp3",), "audio/mpeg"),
    ((".m4a", ".mp4"), "audio/mp4"),
    ((".ogg",), "audio/ogg"),
    ((".wav",), "audio/x-wav"),
    ((".wma",), "audio/x-ms-wma"),
    ((".mid", ".midi"), "audio/midi"),
    (("-mp3.zip",), "application/octet-stream"),
    (("-m4b.zip",), "application/octet-stream"),
    (("-ogg.zip",), "application/octet-stream"),
    (("-mid.zip",), "application/octet-stream"),
    (("-midi.zip",), "application/octet-stream"),
    (("-wav.zip",), "application/octet-stream"),
    (("-wma.zip",), "application/octet-stream"),
]

# Machine-read audio from Gutenberg (MIT TTS) uses paths like /files/1234/1234-m/
_SYNTHESIZED_PATH_RE = re.compile(r"/\d+-m/", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill book_audio and book_audio_chapters from Gutenberg download links.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to the SQLite database.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after processing this many books.")
    parser.add_argument("--chunk-size", type=int, default=64, help="How many books to resolve and write per batch.")
    parser.add_argument("--workers", type=int, default=4, help="How many books to resolve in parallel.")
    parser.add_argument(
        "--repair-all",
        action="store_true",
        help="Prefer live Gutenberg audio indexes for every target and rewrite existing rows.",
    )
    parser.add_argument(
        "--gutenberg-id",
        action="append",
        type=int,
        dest="gutenberg_ids",
        help="Only process the specified Gutenberg ids. Can be repeated.",
    )
    parser.add_argument("--mirror-tries", type=int, default=3, help="How many mirrors to try per file.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written without writing.")
    parser.add_argument(
        "--fill-librivox",
        action="store_true",
        help=(
            "After the normal backfill, search LibriVox for books with no Gutenberg audio "
            "or with synthesized (machine-read) audio and import their human narrations."
        ),
    )
    parser.add_argument(
        "--skip-readme",
        action="store_true",
        help="Skip fetching readme.txt files for narrator and chapter metadata.",
    )
    parser.add_argument(
        "--enrich-readme",
        action="store_true",
        help=(
            "Only post-process existing book_audio rows: fetch readme.txt for each non-synthesized book "
            "that is missing narrator info, then update narrator/narrator_source and "
            "chapter_title/duration columns. Skips URL resolution entirely."
        ),
    )
    return parser.parse_args()


def _chunked(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _rotate_list(values: list[str], offset: int) -> list[str]:
    if not values:
        return values
    offset %= len(values)
    return values[offset:] + values[:offset]


def _audio_downloadlink_sql() -> str:
    """Return the SQL predicate that identifies audio download links."""
    return """
        (
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
    """


class _HrefCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name and name.lower() == "href" and value:
                self.hrefs.append(value)


def _infer_live_audio_download_type(link_name: str) -> str | None:
    lower_name = link_name.lower()
    for suffixes, download_type in LIVE_AUDIO_DOWNLOAD_TYPE_BY_SUFFIX:
        if lower_name.endswith(suffixes):
            return download_type
    return None


def _is_live_audio_url(url: str) -> bool:
    path_name = Path(urlsplit(url).path).name.lower()
    if not path_name or path_name.endswith("/") or path_name.endswith(".rdf") or path_name.endswith(".nfo"):
        return False
    return _infer_live_audio_download_type(path_name) is not None or any(
        path_name.endswith(suffix) for suffix in AUDIO_PACKAGE_SUFFIXES
    )


def _url_points_to_gutenberg_id(url: str, gutenberg_id: int) -> bool:
    """Return True when a Gutenberg audio URL appears to belong to the given Gutenberg id."""
    path = urlsplit(url).path
    needle = str(gutenberg_id)
    segments = [segment for segment in path.split("/") if segment]
    for segment in segments:
        if segment == needle:
            return True
        if segment.startswith(f"{needle}."):
            return True
        if segment.startswith(f"{needle}-"):
            return True
        if segment.startswith(f"{needle}_"):
            return True
        if segment.startswith(f"pg{needle}."):
            return True
        if segment.startswith(f"pg{needle}-"):
            return True
    return False


def _is_synthesized_url(url: str) -> bool:
    """Detect machine-generated (MIT TTS) Gutenberg audio by path pattern like /1234-m/."""
    return bool(_SYNTHESIZED_PATH_RE.search(urlsplit(url).path))


def _fetch_readme(gutenberg_id: int) -> str | None:
    """Fetch the readme.txt for a Gutenberg audio book; returns None if unavailable."""
    url = README_URL_TEMPLATE.format(gid=gutenberg_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": CATALOG_UA})
        resp = urllib.request.urlopen(req, timeout=15)
        try:
            return resp.read().decode("utf-8", errors="replace")
        finally:
            resp.close()
    except Exception:
        return None


def _parse_readme(text: str) -> dict:
    """Extract narrator name and chapter list (title + duration) from a Gutenberg readme.txt."""
    narrator: str | None = None
    chapters: list[dict] = []

    # "is read by\n\nNarrator Name" (LibriVox format)
    m = re.search(r"is read by\s*\n+\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if m:
        narrator = m.group(1).strip()

    # Fallback: "read by Narrator" on a single line, skip generic phrases
    if not narrator:
        m = re.search(r"\bread by\s+([^\n.]+)", text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            if not re.search(r"\b(your|the|a)\b", candidate, re.IGNORECASE):
                narrator = candidate

    # "# Chapter Title - HH:MM:SS"
    for m in re.finditer(r"^#\s*(.+?)\s*-\s*(\d+:\d+:\d+)\s*$", text, re.MULTILINE):
        chapters.append({"title": m.group(1).strip(), "duration": m.group(2).strip()})

    return {"narrator": narrator, "chapters": chapters}


def _fetch_librivox(gutenberg_id: int | None, title: str | None) -> dict | None:
    """
    Look up a book on the LibriVox API.
    Tries gutenberg_id first, then title search.
    Returns the first matching LibriVox book dict or None.
    """
    headers = {"User-Agent": CATALOG_UA}
    fields = "id,title,sections,readers,url_zip_file"

    if gutenberg_id is not None:
        try:
            url = f"{LIBRIVOX_API_URL}?gutenberg_id={gutenberg_id}&fields={fields}&format=json"
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            try:
                data = json.loads(resp.read())
            finally:
                resp.close()
            books = data.get("books") or []
            if books:
                return books[0]
        except Exception:
            pass

    if title:
        try:
            safe_title = urllib.parse.quote(title[:60])
            url = f"{LIBRIVOX_API_URL}?title={safe_title}&fields={fields}&format=json"
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            try:
                data = json.loads(resp.read())
            finally:
                resp.close()
            books = data.get("books") or []
            if books:
                return books[0]
        except Exception:
            pass

    return None


def _build_librivox_audio_item(book_id: int, title: str, lv_book: dict) -> dict | None:
    """Build a normalized audio item dict from a LibriVox API book response."""
    sections = lv_book.get("sections") or []
    if not sections:
        return None

    readers = lv_book.get("readers") or []
    narrator = (
        ", ".join(r.get("display_name", "").strip() for r in readers if r.get("display_name", "").strip())
        or None
    )

    tracks: list[dict] = []
    for sec in sections:
        listen_url = (sec.get("listen_url") or "").strip()
        if not listen_url:
            continue
        try:
            track_num = int(sec.get("section_number") or 0)
        except (ValueError, TypeError):
            track_num = len(tracks) + 1
        chapter_title = (sec.get("title") or "").strip() or None
        tracks.append(
            {
                "track_order": track_num,
                "chapter_title": chapter_title,
                "track_url": listen_url,
                "audio_format": "audio/mpeg",
                "duration": (sec.get("duration") or "").strip() or None,
            }
        )

    if not tracks:
        return None

    tracks.sort(key=lambda t: (t["track_order"], t["track_url"]))
    package_url = (lv_book.get("url_zip_file") or "").strip() or tracks[0]["track_url"]

    return {
        "book_id": book_id,
        "title": title,
        "package_url": package_url,
        "audio_format": "audio/mpeg",
        "track_count": len(tracks),
        "is_chaptered": len(tracks) > 1,
        "narrator": narrator,
        "narrator_source": "librivox",
        "is_synthesized": 0,
        "tracks": tracks,
        "gutenbergbookid": None,
        "audio_action": "librivox",
        "source": "librivox api",
    }


def _fetch_live_file_index_links(book_id: int) -> list[tuple[str, str]]:
    """Fetch the live Gutenberg file index for one Gutenberg id and return audio links."""
    index_url = f"{GUTENBERG_FILES_URL}/{book_id}/"
    response = urllib.request.urlopen(index_url, timeout=60)
    try:
        payload = response.read().decode("utf-8", errors="ignore")
    finally:
        response.close()

    parser = _HrefCollector()
    parser.feed(payload)

    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for href in parser.hrefs:
        if not href or href.startswith("?"):
            continue
        absolute_url = urljoin(index_url, href)
        if absolute_url in seen:
            continue
        if not _is_live_audio_url(absolute_url):
            continue
        download_type = _infer_live_audio_download_type(Path(urlsplit(absolute_url).path).name)
        if not download_type:
            if any(absolute_url.lower().endswith(suffix) for suffix in AUDIO_PACKAGE_SUFFIXES):
                download_type = "application/octet-stream"
            else:
                continue
        seen.add(absolute_url)
        links.append((absolute_url, download_type))

    links.sort(key=lambda item: item[0])
    return links


@lru_cache(maxsize=1)
def _get_mirrors() -> list[str]:
    response = urllib.request.urlopen(MIRRORS_URL, timeout=60)
    try:
        payload = response.read().decode("utf-8", errors="ignore")
    finally:
        response.close()

    mirrors = re.findall(r"http://[^ \r\n]+", payload)
    mirrors = [mirror.rstrip("/") for mirror in mirrors if not mirror.rstrip("/").endswith("/dirs")]
    if not mirrors:
        raise RuntimeError("Could not load any Project Gutenberg mirrors")
    return mirrors


def _mirror_url(url: str, mirror: str) -> str:
    source = urlsplit(url)
    target = urlsplit(mirror)
    return urlunsplit((target.scheme, target.netloc, source.path, source.query, source.fragment))


def _download_audio_blob(url: str) -> int:
    head_request = urllib.request.Request(url, method="HEAD")
    try:
        response = urllib.request.urlopen(head_request, timeout=AUDIO_PROBE_TIMEOUT_SECONDS)
    except HTTPError as exc:
        if exc.code not in {405, 501}:
            raise
    else:
        try:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    return int(content_length)
                except ValueError:
                    pass
        finally:
            response.close()

    get_request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    response = urllib.request.urlopen(get_request, timeout=AUDIO_PROBE_TIMEOUT_SECONDS)
    try:
        chunk = response.read(1)
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                return int(content_length)
            except ValueError:
                pass
        return len(chunk)
    finally:
        response.close()


def _install_https_opener() -> None:
    context = ssl._create_unverified_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    urllib.request.install_opener(opener)
    ssl._create_default_https_context = lambda: context


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create audio tables and add any missing columns from schema upgrades."""
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
            narrator TEXT,
            narrator_source TEXT,
            is_synthesized INTEGER NOT NULL DEFAULT 0,
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
            duration TEXT,
            download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(book_id, track_order, track_url)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_book_audio_chapters_book_id ON book_audio_chapters(book_id)")

    # Migrate existing tables: add columns introduced after initial schema
    existing_audio = {r[1] for r in cur.execute("PRAGMA table_info(book_audio)")}
    for col, defn in [
        ("narrator", "TEXT"),
        ("narrator_source", "TEXT"),
        ("is_synthesized", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in existing_audio:
            cur.execute(f"ALTER TABLE book_audio ADD COLUMN {col} {defn}")

    existing_chapters = {r[1] for r in cur.execute("PRAGMA table_info(book_audio_chapters)")}
    if "duration" not in existing_chapters:
        cur.execute("ALTER TABLE book_audio_chapters ADD COLUMN duration TEXT")

    conn.commit()


def _track_order_from_url(url: str, fallback: int) -> int:
    """Derive a stable track order from the filename when Gutenberg already encodes one."""
    filename = Path(urlsplit(url).path).name
    match = re.search(r"(\d+)(?=\.[^.]+$)", filename)
    if match:
        return int(match.group(1))
    match = re.search(r"-(\d+)(?:\.[^.]+)?$", filename)
    if match:
        return int(match.group(1))
    return fallback


def _is_audio_package(url: str, download_type: str) -> bool:
    """Return True for package archives such as mp3, m4b, ogg, midi, wav, or wma bundles."""
    name = Path(urlsplit(url).path).name.lower()
    return download_type == "application/octet-stream" and any(name.endswith(suffix) for suffix in AUDIO_PACKAGE_SUFFIXES)


def _track_name(url: str) -> str:
    """Return the filename portion of a track URL."""
    return Path(urlsplit(url).path).name


def _preview_tracks(tracks: list[dict], limit: int = 3) -> str:
    """Render a short human-readable preview for logging."""
    if not tracks:
        return "[]"
    names = [_track_name(track["track_url"]) for track in tracks[:limit]]
    remaining = len(tracks) - len(names)
    preview = ", ".join(names)
    if remaining > 0:
        preview += f", ... (+{remaining} more)"
    return f"[{preview}]"


def _get_local_audio_candidates_for_books(
    conn: sqlite3.Connection,
    book_ids: list[int] | None,
) -> dict[int, list[tuple[str, str]]]:
    """Load local audio candidates keyed by internal catalog book id."""
    cur = conn.cursor()
    params: list[object] = []
    id_clause = ""
    if book_ids:
        placeholders = ",".join(["?"] * len(book_ids))
        id_clause = f"AND b.id IN ({placeholders})"
        params.extend(book_ids)

    cur.execute(
        f"""
        SELECT DISTINCT
            b.id,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
        FROM books b
        JOIN downloadlinks d ON d.bookid = b.id
        JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
        WHERE b.gutenbergbookid IS NOT NULL
          AND {_audio_downloadlink_sql()}
          {id_clause}
        ORDER BY b.id
        """,
        params,
    )
    book_rows = cur.fetchall()
    book_ids = [int(row[0]) for row in book_rows]

    candidates_by_book: dict[int, list[tuple[str, str]]] = {book_id: [] for book_id in book_ids}

    def query_chunk(chunk: list[int]) -> list[tuple[int, str, str]]:
        if not chunk:
            return []
        chunk_placeholders = ",".join(["?"] * len(chunk))
        cur.execute(
            f"""
            SELECT
                b.id,
                d.name AS url,
                dt.name AS download_type
            FROM books b
            JOIN downloadlinks d ON d.bookid = b.id
            JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
            WHERE b.id IN ({chunk_placeholders})
              AND {_audio_downloadlink_sql()}
            ORDER BY b.id, d.name
            """,
            chunk,
        )
        return [(int(book_id), url, download_type) for book_id, url, download_type in cur.fetchall()]

    for chunk in _chunked(book_ids, 200):
        for book_id, url, download_type in query_chunk(chunk):
            candidates_by_book.setdefault(book_id, []).append((url, download_type))

    return candidates_by_book


def _build_audio_item(book_id: int, title: str, candidates: list[tuple[str, str]]) -> dict | None:
    """Convert a candidate URL list into the normalized audio summary structure."""
    grouped: dict[str, list[str]] = {}
    packages: list[tuple[str, str]] = []

    for url, download_type in candidates:
        if _is_audio_package(url, download_type):
            packages.append((url, download_type))
        elif download_type.startswith("audio/"):
            grouped.setdefault(download_type, []).append(url)

    primary_format = None
    primary_tracks: list[str] = []
    for audio_format in AUDIO_FORMAT_PRIORITY:
        tracks = grouped.get(audio_format, [])
        if tracks:
            primary_format = audio_format
            primary_tracks = tracks
            break

    if primary_format is None:
        for audio_format, tracks in grouped.items():
            primary_format = audio_format
            primary_tracks = tracks
            break

    if not primary_format:
        return None

    is_synthesized = bool(primary_tracks) and all(_is_synthesized_url(url) for url in primary_tracks)

    package_url = packages[0][0] if packages else primary_tracks[0]
    tracks = []
    for fallback_index, url in enumerate(primary_tracks, start=1):
        tracks.append(
            {
                "track_order": _track_order_from_url(url, fallback_index),
                "chapter_title": None,
                "track_url": url,
                "audio_format": primary_format,
                "duration": None,
            }
        )
    tracks.sort(key=lambda item: (item["track_order"], item["track_url"]))

    return {
        "book_id": book_id,
        "title": title,
        "package_url": package_url,
        "audio_format": primary_format,
        "track_count": len(tracks),
        "is_chaptered": len(tracks) > 1,
        "narrator": None,
        "narrator_source": "synthesized" if is_synthesized else None,
        "is_synthesized": 1 if is_synthesized else 0,
        "tracks": tracks,
    }


def _select_audio_candidates(
    gutenbergbookid: int | None,
    local_candidates: list[tuple[str, str]],
    repair_all: bool,
) -> tuple[str, list[tuple[str, str]]]:
    """Pick the best candidate set for one book and label it as repair or refresh."""
    live_candidates = _fetch_live_file_index_links(gutenbergbookid) if gutenbergbookid is not None else []

    if repair_all:
        if live_candidates:
            return "repair", live_candidates
        if local_candidates:
            return "repair", local_candidates
        return "skip", []

    if not local_candidates:
        if live_candidates:
            return "refresh", live_candidates
        return "skip", []

    if any(not _url_points_to_gutenberg_id(url, gutenbergbookid or 0) for url, _ in local_candidates):
        if live_candidates:
            return "repair", live_candidates
        return "repair", local_candidates

    local_names = {Path(urlsplit(url).path).name.lower() for url, _ in local_candidates}
    if live_candidates:
        live_names = {Path(urlsplit(url).path).name.lower() for url, _ in live_candidates}
        if local_names != live_names:
            return "refresh", live_candidates

    return "local", local_candidates


def _download_audio_url(url: str, book_id: int, prefer_mirrors: bool, mirror_tries: int) -> tuple[str, int]:
    """Download-probe one URL, optionally trying mirrors before the source URL."""
    mirrors = _get_mirrors()
    mirror_order = _rotate_list(mirrors, book_id)
    if mirror_tries > 0:
        mirror_order = mirror_order[:mirror_tries]

    attempts: list[str] = []
    if prefer_mirrors:
        attempts.extend(_mirror_url(url, mirror) for mirror in mirror_order)
        attempts.append(url)
    else:
        attempts.append(url)
        attempts.extend(_mirror_url(url, mirror) for mirror in mirror_order)

    last_error: Exception | None = None
    for candidate_url in attempts:
        try:
            return candidate_url, _download_audio_blob(candidate_url)
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Could not download audio URL: {url}")


def _materialize_audio_item(item: dict, prefer_mirrors: bool, mirror_tries: int) -> dict:
    """Resolve all package and track URLs through live mirror probing."""
    resolved_urls: dict[str, str] = {}
    ordered_urls: list[str] = [item["package_url"], *(track["track_url"] for track in item["tracks"])]
    for url in ordered_urls:
        if url in resolved_urls:
            continue
        resolved_url, _ = _download_audio_url(url, item["book_id"], prefer_mirrors, mirror_tries)
        resolved_urls[url] = resolved_url

    item = dict(item)
    item["package_url"] = resolved_urls[item["package_url"]]
    item["tracks"] = [
        {
            **track,
            "track_url": resolved_urls[track["track_url"]],
        }
        for track in item["tracks"]
    ]
    return item


def _load_audio_targets(conn: sqlite3.Connection, gutenberg_ids: list[int] | None) -> list[dict]:
    """Load candidate books for audio backfill keyed by internal catalog book id."""
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
            b.id AS book_id,
            b.gutenbergbookid,
            EXISTS (SELECT 1 FROM book_audio ba WHERE ba.book_id = b.id) AS has_audio_row,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
        FROM books b
        WHERE b.gutenbergbookid IS NOT NULL
          AND (
              EXISTS (SELECT 1 FROM book_audio ba WHERE ba.book_id = b.id)
              OR EXISTS (
                  SELECT 1
                  FROM downloadlinks d
                  JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
                  WHERE d.bookid = b.id
                    AND {_audio_downloadlink_sql()}
              )
          )
          {id_clause}
        ORDER BY b.gutenbergbookid, b.id
        """,
        params,
    )
    book_rows = cur.fetchall()
    return [
        {
            "book_id": int(book_id),
            "gutenbergbookid": int(gutenbergbookid) if gutenbergbookid is not None else None,
            "has_audio_row": bool(has_audio_row),
            "title": title,
        }
        for book_id, gutenbergbookid, has_audio_row, title in book_rows
    ]


def _load_gap_books(conn: sqlite3.Connection) -> list[dict]:
    """Load books that have no audio row at all — candidates for LibriVox gap-filling."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT
            b.id AS book_id,
            b.gutenbergbookid,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
        FROM books b
        WHERE b.gutenbergbookid IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM book_audio ba WHERE ba.book_id = b.id)
        ORDER BY b.id
        LIMIT 5000
        """
    )
    return [{"book_id": int(r[0]), "gutenbergbookid": int(r[1]), "title": r[2]} for r in cur.fetchall()]


def _load_synthesized_books(conn: sqlite3.Connection) -> list[dict]:
    """Load books whose current audio is machine-generated — candidates for LibriVox replacement."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT
            b.id AS book_id,
            b.gutenbergbookid,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
        FROM books b
        JOIN book_audio ba ON ba.book_id = b.id
        WHERE ba.is_synthesized = 1
        ORDER BY b.id
        """
    )
    return [{"book_id": int(r[0]), "gutenbergbookid": int(r[1]), "title": r[2]} for r in cur.fetchall()]


def _resolve_audio_item(
    target: dict,
    local_candidates: list[tuple[str, str]],
    repair_all: bool,
    mirror_tries: int,
) -> dict | None:
    """Resolve one audio target into the normalized row payload for persistence."""
    action, selected_candidates = _select_audio_candidates(
        target["gutenbergbookid"],
        local_candidates,
        repair_all,
    )
    if not selected_candidates:
        if repair_all and target.get("has_audio_row"):
            return {
                "book_id": target["book_id"],
                "gutenbergbookid": target["gutenbergbookid"],
                "title": target["title"],
                "audio_action": "delete",
                "source": "stale audio row",
                "delete_audio": True,
            }
        return None

    item = _build_audio_item(target["book_id"], target["title"], selected_candidates)
    if item is None:
        return None

    item["gutenbergbookid"] = target["gutenbergbookid"]
    item["audio_action"] = action
    item["source"] = "live file index" if action in {"repair", "refresh"} else "catalog downloadlinks"
    item = _materialize_audio_item(item, prefer_mirrors=action in {"repair", "refresh"}, mirror_tries=mirror_tries)
    return item


def _enrich_item_from_readme(item: dict) -> None:
    """
    Fetch and parse the Gutenberg readme.txt for narrator and per-chapter info.
    Mutates item in-place; silently skips if readme is unavailable.
    """
    gutenberg_id = item.get("gutenbergbookid")
    if gutenberg_id is None:
        return

    readme_text = _fetch_readme(gutenberg_id)
    if not readme_text:
        return

    readme = _parse_readme(readme_text)

    if readme["narrator"] and not item.get("is_synthesized"):
        item["narrator"] = readme["narrator"]
        item["narrator_source"] = "readme"

    readme_chapters = readme["chapters"]
    if readme_chapters:
        for i, track in enumerate(item.get("tracks", [])):
            if i >= len(readme_chapters):
                break
            rc = readme_chapters[i]
            if not track.get("chapter_title"):
                track["chapter_title"] = rc["title"]
            track["duration"] = rc["duration"]


def _delete_audio(conn: sqlite3.Connection, book_id: int) -> None:
    """Delete all stored audio rows for one book."""
    cur = conn.cursor()
    cur.execute("DELETE FROM book_audio_chapters WHERE book_id = ?", (book_id,))
    cur.execute("DELETE FROM book_audio WHERE book_id = ?", (book_id,))


def _save_audio(conn: sqlite3.Connection, item: dict) -> None:
    """Persist the normalized audio summary and chapter rows for one book."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO book_audio (
            book_id,
            package_url,
            audio_format,
            track_count,
            is_chaptered,
            narrator,
            narrator_source,
            is_synthesized,
            download_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            item["book_id"],
            item["package_url"],
            item["audio_format"],
            item["track_count"],
            1 if item["is_chaptered"] else 0,
            item.get("narrator"),
            item.get("narrator_source"),
            item.get("is_synthesized", 0),
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
                duration,
                download_date
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                item["book_id"],
                track["track_order"],
                track.get("chapter_title"),
                track["track_url"],
                track["audio_format"],
                track.get("duration"),
            ),
        )
    conn.commit()


def _run_librivox_fill(
    conn: sqlite3.Connection,
    books: list[dict],
    label: str,
    dry_run: bool,
) -> tuple[int, int]:
    """
    Try to find each book on LibriVox and save the result.
    Returns (written, failed) counts.
    """
    written = 0
    failed = 0
    for book in books:
        lv_book = _fetch_librivox(book["gutenbergbookid"], book["title"])
        if lv_book is None:
            failed += 1
            print(f"librivox {label} {book['book_id']}: not found on LibriVox ({book['title']})", flush=True)
            continue

        item = _build_librivox_audio_item(book["book_id"], book["title"], lv_book)
        if item is None:
            failed += 1
            print(
                f"librivox {label} {book['book_id']}: found on LibriVox but no sections available ({book['title']})",
                flush=True,
            )
            continue

        narrator_label = item.get("narrator") or "unknown narrator"
        print(
            f"librivox {label} {book['book_id']}: narrator={narrator_label!r} "
            f"tracks={item['track_count']} ({book['title']})",
            flush=True,
        )
        if not dry_run:
            _save_audio(conn, item)
            written += 1

    return written, failed


def _run_readme_enrichment(
    conn: sqlite3.Connection,
    dry_run: bool,
    limit: int | None,
) -> tuple[int, int]:
    """
    Post-process existing book_audio rows to fill narrator and per-chapter metadata from readme.txt.
    Only targets non-synthesized books where narrator is currently NULL.
    Returns (enriched, skipped) counts.
    """
    cur = conn.cursor()
    query = """
        SELECT ba.book_id, b.gutenbergbookid,
               COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
        FROM book_audio ba
        JOIN books b ON b.id = ba.book_id
        WHERE ba.is_synthesized = 0
          AND ba.narrator IS NULL
          AND b.gutenbergbookid IS NOT NULL
        ORDER BY ba.book_id
    """
    if limit is not None:
        query += f" LIMIT {limit}"
    cur.execute(query)
    rows = cur.fetchall()

    print(f"summary: readme_enrich_candidates={len(rows)}", flush=True)

    enriched = 0
    skipped = 0
    for book_id, gutenberg_id, title in rows:
        readme_text = _fetch_readme(gutenberg_id)
        if not readme_text:
            skipped += 1
            print(f"readme enrich {book_id}: no readme available ({title})", flush=True)
            continue

        readme = _parse_readme(readme_text)
        narrator = readme["narrator"]
        readme_chapters = readme["chapters"]

        if not narrator and not readme_chapters:
            skipped += 1
            print(f"readme enrich {book_id}: nothing found in readme ({title})", flush=True)
            continue

        print(
            f"readme enrich {book_id}: narrator={narrator!r} chapters={len(readme_chapters)} ({title})",
            flush=True,
        )

        if not dry_run:
            if narrator:
                conn.execute(
                    "UPDATE book_audio SET narrator = ?, narrator_source = 'readme' WHERE book_id = ? AND narrator IS NULL",
                    (narrator, book_id),
                )
            if readme_chapters:
                chapter_rows = conn.execute(
                    "SELECT id FROM book_audio_chapters WHERE book_id = ? ORDER BY track_order, id",
                    (book_id,),
                ).fetchall()
                for i, (chapter_id,) in enumerate(chapter_rows):
                    if i >= len(readme_chapters):
                        break
                    rc = readme_chapters[i]
                    conn.execute(
                        """
                        UPDATE book_audio_chapters
                        SET chapter_title = COALESCE(chapter_title, ?), duration = ?
                        WHERE id = ?
                        """,
                        (rc["title"], rc["duration"], chapter_id),
                    )
            conn.commit()
            enriched += 1

    return enriched, skipped


def backfill(
    db_path: str,
    limit: int | None,
    gutenberg_ids: list[int] | None,
    dry_run: bool,
    mirror_tries: int,
    chunk_size: int,
    workers: int,
    repair_all: bool,
    fill_librivox: bool,
    skip_readme: bool,
    enrich_readme: bool,
) -> None:
    """Run the audio backfill, resolving books in parallel but writing sequentially."""
    _install_https_opener()
    conn = _connect_db(db_path)
    _ensure_tables(conn)

    if enrich_readme:
        print("stage: readme enrichment only (skipping URL resolution)", flush=True)
        enriched, skipped = _run_readme_enrichment(conn, dry_run, limit)
        conn.close()
        if dry_run:
            print(f"done: would_enrich={enriched + skipped} skipped={skipped}", flush=True)
        else:
            print(f"done: enriched={enriched} skipped={skipped}", flush=True)
        return

    print("stage: scanning catalog for audio books", flush=True)
    targets = _load_audio_targets(conn, gutenberg_ids)
    if limit is not None:
        targets = targets[:limit]
    local_candidates_by_book = _get_local_audio_candidates_for_books(conn, [target["book_id"] for target in targets])
    conn.close()

    print("stage: resolving audio packages and tracks", flush=True)
    print(
        f"summary: audio_books={len(targets)} chunk_size={max(1, chunk_size)} "
        f"workers={max(1, workers)} repair_all={repair_all} fill_librivox={fill_librivox}",
        flush=True,
    )
    print("stage: persisting audio metadata", flush=True)
    written = 0
    deleted = 0
    failed = 0
    if not targets:
        print("done: written=0 failed=0", flush=True)
        return

    conn = _connect_db(db_path)
    try:
        for chunk in _chunked(targets, max(1, chunk_size)):
            resolved_items: list[dict] = []
            with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                futures = {
                    executor.submit(
                        _resolve_audio_item,
                        target,
                        local_candidates_by_book.get(target["book_id"], []),
                        repair_all,
                        mirror_tries,
                    ): target
                    for target in chunk
                }
                for future in as_completed(futures):
                    target = futures[future]
                    try:
                        item = future.result()
                    except Exception as exc:
                        failed += 1
                        print(f"audio {target['book_id']}: failed to resolve download URLs ({exc})", flush=True)
                        continue
                    if item is None:
                        failed += 1
                        print(f"audio {target['book_id']}: no usable audio candidates", flush=True)
                        continue
                    resolved_items.append(item)

            resolved_items.sort(key=lambda item: item["book_id"])

            # Enrich non-delete items with readme metadata (narrator, chapter title, duration)
            if not skip_readme:
                for item in resolved_items:
                    if not item.get("delete_audio"):
                        _enrich_item_from_readme(item)

            previewed = len(resolved_items)
            if not dry_run:
                conn.execute("BEGIN")
            try:
                for item in resolved_items:
                    if item.get("delete_audio"):
                        if dry_run:
                            print(
                                f"audio {item['book_id']}: action=delete source={item.get('source', 'stale audio row')} "
                                f"would remove stale audio rows ({item['title']})",
                                flush=True,
                            )
                        else:
                            _delete_audio(conn, item["book_id"])
                            deleted += 1
                            print(
                                f"deleted stale audio rows for book_id={item['book_id']} "
                                f"({item['title']})",
                                flush=True,
                            )
                        continue

                    narrator_label = item.get("narrator") or ("synthesized" if item.get("is_synthesized") else "unknown")
                    print(
                        f"audio {item['book_id']}: action={item.get('audio_action', 'local')} "
                        f"source={item.get('source', 'catalog downloadlinks')} "
                        f"narrator={narrator_label!r} synthesized={bool(item.get('is_synthesized'))} "
                        f"package={item['package_url']} tracks={item['track_count']} "
                        f"chaptered={item['is_chaptered']} format={item['audio_format']} ({item['title']})",
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
                if not dry_run:
                    conn.commit()
            except Exception:
                if not dry_run:
                    conn.rollback()
                raise

        # LibriVox gap-filling: books with no audio at all
        if fill_librivox:
            print("stage: librivox gap-filling (no audio)", flush=True)
            gap_books = _load_gap_books(conn)
            print(f"summary: gap_books={len(gap_books)}", flush=True)
            lv_written, lv_failed = _run_librivox_fill(conn, gap_books, "gap", dry_run)
            written += lv_written
            failed += lv_failed

            # LibriVox replacement: books with synthesized audio
            print("stage: librivox gap-filling (synthesized audio)", flush=True)
            synth_books = _load_synthesized_books(conn)
            print(f"summary: synthesized_books={len(synth_books)}", flush=True)
            lv_written, lv_failed = _run_librivox_fill(conn, synth_books, "replace-synth", dry_run)
            written += lv_written
            failed += lv_failed

    finally:
        conn.close()

    if dry_run:
        print(f"done: previewed={previewed} failed={failed}", flush=True)
    else:
        print(f"done: written={written} deleted={deleted} failed={failed}", flush=True)


def main() -> None:
    args = parse_args()
    backfill(
        args.db_path,
        args.limit,
        args.gutenberg_ids,
        args.dry_run,
        args.mirror_tries,
        args.chunk_size,
        args.workers,
        args.repair_all,
        args.fill_librivox,
        args.skip_readme,
        args.enrich_readme,
    )


if __name__ == "__main__":
    main()
