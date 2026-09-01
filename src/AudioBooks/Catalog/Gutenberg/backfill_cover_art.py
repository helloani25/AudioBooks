from __future__ import annotations

import argparse
import re
import sqlite3
import ssl
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.db_utils import (
    connect_db as _connect_db,
    ensure_book_cover_art_table as _ensure_book_cover_art_table,
    with_sqlite_retry as _with_sqlite_retry,
)


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
LOCAL_RDF_DIR = BASE_DIR.parent / "DB" / "cache" / "epub"
GUTENBERG_RDF_URL = "https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.rdf"

RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DCTERMS_NS = "http://purl.org/dc/terms/"
PGTERMS_NS = "http://www.gutenberg.org/2009/pgterms/"

KNOWN_COVER_SIZE_ORDER = {
    "small": 0,
    "medium": 1,
    "large": 2,
    "xlarge": 3,
    "full": 4,
    "original": 5,
    "cover": 6,
}

IMAGE_MIME_PREFIX = "image/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill Gutenberg cover art metadata with every available cover size.",
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
    parser.add_argument(
        "--refresh-live",
        action="store_true",
        help="Fetch the live Gutenberg RDF and merge it with the local cache when available.",
    )
    parser.add_argument("--ca-bundle", dest="cafile", help="Path to a PEM CA bundle file.")
    parser.add_argument("--ca-dir", dest="capath", help="Path to a directory of CA certificates.")
    parser.add_argument(
        "--no-verify",
        action="store_false",
        dest="verify",
        default=True,
        help="Disable SSL certificate verification.",
    )
    return parser.parse_args()


def _resolve_ca_paths(cli_cafile: str | None, cli_capath: str | None, verify: bool = True):
    if not verify:
        return None, None

    cafile = cli_cafile
    if not cafile:
        try:
            import certifi

            cafile = certifi.where()
        except ImportError:
            cafile = None

    capath = cli_capath

    if cafile and not Path(cafile).is_file():
        raise FileNotFoundError(f"CA bundle not found: {cafile}")
    if capath and not Path(capath).is_dir():
        raise FileNotFoundError(f"CA directory not found: {capath}")
    return cafile, capath


def _install_https_opener(cafile, capath, verify: bool = True) -> None:
    if not verify:
        context = ssl._create_unverified_context()
    else:
        context = ssl.create_default_context()
        if cafile or capath:
            context.load_verify_locations(cafile=cafile, capath=capath)

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    urllib.request.install_opener(opener)
    ssl._create_default_https_context = lambda: context


def _load_books(conn: sqlite3.Connection, gutenberg_ids: list[int] | None) -> list[dict]:
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
        WHERE b.gutenbergbookid IS NOT NULL
          {id_clause}
        ORDER BY b.gutenbergbookid
        """,
        params,
    )
    rows = cur.fetchall()
    return [{"book_id": int(book_id), "title": title} for book_id, title in rows]


def _local_rdf_path(book_id: int) -> Path:
    return LOCAL_RDF_DIR / str(book_id) / f"pg{book_id}.rdf"


def _load_rdf_bytes(book_id: int, refresh_live: bool) -> tuple[bytes, str]:
    rdf_url = GUTENBERG_RDF_URL.format(book_id=book_id)
    local_path = _local_rdf_path(book_id)
    local_payload: bytes | None = None

    if local_path.is_file():
        local_payload = local_path.read_bytes()
        if not refresh_live:
            return local_payload, rdf_url

    try:
        response = urllib.request.urlopen(rdf_url, timeout=60)
        try:
            live_payload = response.read()
        finally:
            response.close()
    except Exception:
        if local_payload is not None:
            return local_payload, rdf_url
        raise

    if local_payload and refresh_live and len(live_payload) > 0:
        return live_payload, rdf_url
    if local_payload:
        return local_payload, rdf_url
    return live_payload, rdf_url


def _cover_size_sort_order(size_label: str) -> int:
    return KNOWN_COVER_SIZE_ORDER.get(size_label.lower(), 100)


def _extract_mime_type(file_el: ET.Element) -> str:
    value_el = file_el.find(f".//{{{RDF_NS}}}value")
    if value_el is not None and value_el.text:
        return value_el.text.strip()
    return ""


def _extract_byte_size(file_el: ET.Element) -> int | None:
    extent_el = file_el.find(f"{{{DCTERMS_NS}}}extent")
    if extent_el is None or not extent_el.text:
        return None
    value = extent_el.text.strip()
    return int(value) if value.isdigit() else None


def _extract_cover_entries(rdf_bytes: bytes, book_id: int, rdf_url: str) -> list[dict]:
    root = ET.fromstring(rdf_bytes)
    entries_by_url: dict[str, dict] = {}

    for has_format in root.findall(f".//{{{DCTERMS_NS}}}hasFormat"):
        file_el = has_format.find(f"{{{PGTERMS_NS}}}file")
        if file_el is None:
            continue

        image_url = file_el.attrib.get(f"{{{RDF_NS}}}about")
        if not image_url:
            continue

        filename = Path(urlsplit(image_url).path).name.lower()
        match = re.match(
            rf"^pg{book_id}\.cover(?:\.([^.]+))?\.(jpg|jpeg|png|gif|webp)$",
            filename,
        )
        if not match:
            continue

        mime_type = _extract_mime_type(file_el)
        if not mime_type.startswith(IMAGE_MIME_PREFIX):
            continue

        size_label = match.group(1) or "cover"
        entry = {
            "bookid": book_id,
            "size_label": size_label,
            "sort_order": _cover_size_sort_order(size_label),
            "image_url": image_url,
            "mime_type": mime_type,
            "byte_size": _extract_byte_size(file_el),
            "rdf_url": rdf_url,
            "source": "gutenberg-rdf",
        }

        existing = entries_by_url.get(image_url)
        if existing is None or entry["sort_order"] < existing["sort_order"]:
            entries_by_url[image_url] = entry

    return sorted(
        entries_by_url.values(),
        key=lambda item: (item["sort_order"], item["byte_size"] or 0, item["size_label"], item["image_url"]),
    )


def _save_cover_art(db_path: str, book_id: int, entries: list[dict]) -> None:
    def write() -> None:
        conn = _connect_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM book_cover_art WHERE bookid = ?", (book_id,))
            for entry in entries:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO book_cover_art (
                        bookid,
                        size_label,
                        sort_order,
                        image_url,
                        mime_type,
                        byte_size,
                        rdf_url,
                        source,
                        download_date
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        entry["bookid"],
                        entry["size_label"],
                        entry["sort_order"],
                        entry["image_url"],
                        entry["mime_type"],
                        entry["byte_size"],
                        entry["rdf_url"],
                        entry["source"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    _with_sqlite_retry(write)


def backfill(
    db_path: str,
    limit: int | None,
    gutenberg_ids: list[int] | None,
    dry_run: bool,
    refresh_live: bool,
) -> None:
    conn = _connect_db(db_path)
    try:
        _ensure_book_cover_art_table(db_path)
        books = _load_books(conn, gutenberg_ids)
    finally:
        conn.close()
    if limit is not None:
        books = books[:limit]

    print("stage: scanning catalog for cover art", flush=True)
    print(f"summary: books={len(books)}", flush=True)

    written = 0
    missing = 0
    for book in books:
        try:
            rdf_bytes, rdf_url = _load_rdf_bytes(book["book_id"], refresh_live=refresh_live)
            entries = _extract_cover_entries(rdf_bytes, book["book_id"], rdf_url)
        except Exception as exc:
            missing += 1
            print(f"cover art {book['book_id']}: failed to load RDF ({exc})", flush=True)
            continue

        if not entries:
            missing += 1
            print(f"cover art {book['book_id']}: no cover images found ({book['title']})", flush=True)
            if not dry_run:
                _save_cover_art(db_path, book["book_id"], [])
            continue

        print(
            f"cover art {book['book_id']}: sizes={', '.join(entry['size_label'] for entry in entries)} "
            f"count={len(entries)} ({book['title']})",
            flush=True,
        )
        for entry in entries:
            print(f"  {entry['size_label']}: {entry['image_url']}", flush=True)

        if not dry_run:
            _save_cover_art(db_path, book["book_id"], entries)
            written += 1

    if dry_run:
        print(f"done: previewed={len(books)} missing={missing}", flush=True)
    else:
        print(f"done: written={written} missing={missing}", flush=True)


def main() -> None:
    args = parse_args()
    cafile, capath = _resolve_ca_paths(args.cafile, args.capath, verify=args.verify)
    _install_https_opener(cafile, capath, verify=args.verify)
    backfill(args.db_path, args.limit, args.gutenberg_ids, args.dry_run, args.refresh_live)


if __name__ == "__main__":
    main()
