"""Backfill Gutenberg HTML+images into book_contents (content_type='html').

Downloads image-capable Gutenberg variants, uploads images to GCS, rewrites
image paths in the HTML to route through the Flask image endpoint, and stores
the result in book_contents.

Image source
------------
The images are the original illustrations from each book's first publication,
digitised by Project Gutenberg volunteers and released into the public domain
along with the text. They include:

  - Engravings and woodcuts from 19th-century novels
  - Photographs from travel memoirs, natural history, and biographies
  - Maps and diagrams from geography and science titles
  - Decorative chapter headings and frontispieces

Primary source is the cleaned -h.zip archive (downloadlinks type 8). If that
does not produce inline images, the script falls back to `.html.images`,
`.epub3.images`, and `.epub.images` variants. During backfill we:

  1. Upload each image to gs://gutenberg-books/book-html/{gutenberg_id}/images/
  2. Rewrite the HTML src attributes to /api/books/{book_id}/images/{filename}
  3. The Flask route signs a GCS URL on demand and returns a 302 redirect

Usage examples:
  # Dry-run on all books that have an image-capable source and no HTML yet
  python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --dry-run

  # Run with 8 workers, GCS credentials from env (GOOGLE_APPLICATION_CREDENTIALS)
  python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --workers 8

  # Process a single book by internal ID
  python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --book-ids 48907

  # Resume a previous run (skips already-done books automatically)
  python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --workers 4

  # Check queue state without processing
  python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --status

  # Re-process already-done books
  python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --force --reset-queue
"""

from __future__ import annotations

import argparse
import html as html_module
import io
import os
import posixpath
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

try:
    from dotenv import load_dotenv
except ImportError:
    # Optional dependency: script can run without loading .env files.
    def load_dotenv(*_args, **_kwargs):  # type: ignore[override]
        return False

load_dotenv()

try:
    from google.cloud import storage as gcs_storage
    HAS_GCS = True
except ImportError:
    HAS_GCS = False

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import lxml.html as _lxml_html
    _HAS_LXML = True
except ImportError:
    _lxml_html = None  # type: ignore[assignment]
    _HAS_LXML = False

from AudioBooks.Catalog.Gutenberg.db_utils import (
    connect_db as _connect_db,
    with_sqlite_retry,
    ensure_book_contents_table,
    ensure_book_content_backfill_tables,
    migrate_book_contents_content_type,
    migrate_book_contents_html_content,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
QUEUE_KEY = "html:v1"
HTTP_TIMEOUT = 60
DEFAULT_GCS_BUCKET = os.environ.get("GCS_BUCKET")
GCS_PREFIX = "book-html"

# Gutenberg's downloadlinks table uses integer type IDs. Type 8 is the -h.zip
# archive which bundles cleaned HTML together with inline images.
HTML_ZIP_DOWNLOAD_TYPE = 8

SOURCE_HZIP = "h-zip"
SOURCE_HTML_IMAGES = "html.images"
SOURCE_EPUB3_IMAGES = "epub3.images"
SOURCE_EPUB_IMAGES = "epub.images"

_HZIP_URL_RE = re.compile(r"-h\.zip(?:[?#].*)?$", re.IGNORECASE)
_HTML_IMAGES_URL_RE = re.compile(r"\.(?:html|htm)\.images(?:[?#].*)?$", re.IGNORECASE)
_EPUB3_IMAGES_URL_RE = re.compile(r"\.epub3\.images(?:[?#].*)?$", re.IGNORECASE)
_EPUB_IMAGES_URL_RE = re.compile(r"\.epub\.images(?:[?#].*)?$", re.IGNORECASE)


@dataclass
class HtmlBookRow:
    book_id: int
    gutenberg_id: int
    title: str
    source_url: str
    source_type: str


def _is_hzip_url(url: str) -> bool:
    return bool(_HZIP_URL_RE.search(url))


def _is_html_images_url(url: str) -> bool:
    return bool(_HTML_IMAGES_URL_RE.search(url))


def _is_epub3_images_url(url: str) -> bool:
    return bool(_EPUB3_IMAGES_URL_RE.search(url))


def _is_epub_images_url(url: str) -> bool:
    lower = url.lower()
    return ".epub.noimages" not in lower and bool(_EPUB_IMAGES_URL_RE.search(url))


def _classify_source_url(url: str) -> str | None:
    if _is_hzip_url(url):
        return SOURCE_HZIP
    if _is_html_images_url(url):
        return SOURCE_HTML_IMAGES
    if _is_epub3_images_url(url):
        return SOURCE_EPUB3_IMAGES
    if _is_epub_images_url(url):
        return SOURCE_EPUB_IMAGES
    return None


def _source_sort_key(url: str) -> tuple[int, int, int, str]:
    source_type = _classify_source_url(url)
    kind_rank = {
        SOURCE_HZIP: 0,
        SOURCE_HTML_IMAGES: 1,
        SOURCE_EPUB3_IMAGES: 2,
        SOURCE_EPUB_IMAGES: 3,
    }.get(source_type, 9)

    if url.startswith("https://www.gutenberg.org/cache/epub/"):
        host_rank = 0
    elif url.startswith("https://www.gutenberg.org/files/"):
        host_rank = 1
    elif url.startswith("https://www.gutenberg.org/ebooks/"):
        host_rank = 2
    else:
        host_rank = 3
    return (kind_rank, host_rank, len(url), url)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _ensure_tables(db_path: str) -> None:
    ensure_book_contents_table(db_path)
    ensure_book_content_backfill_tables(db_path)
    migrate_book_contents_content_type(db_path)
    migrate_book_contents_html_content(db_path)


def _load_target_books(
    db_path: str,
    *,
    book_ids: list[int] | None = None,
    gutenberg_ids: list[int] | None = None,
    force: bool = False,
    limit: int | None = None,
) -> list[HtmlBookRow]:
    conn = _connect_db(db_path)
    try:
        cur = conn.cursor()
        conditions = ["1=1"]
        params: list = []

        if not force:
            conditions.append(
                "NOT EXISTS ("
                "  SELECT 1 FROM book_contents bc"
                "  WHERE bc.bookid = b.id AND bc.content_type = 'html'"
                ")"
            )

        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            conditions.append(f"b.id IN ({placeholders})")
            params.extend(book_ids)

        if gutenberg_ids:
            placeholders = ",".join("?" * len(gutenberg_ids))
            conditions.append(f"b.gutenbergbookid IN ({placeholders})")
            params.extend(gutenberg_ids)

        where = " AND ".join(conditions)
        limit_clause = f"LIMIT {limit}" if limit else ""
        cur.execute(
            f"""
            WITH candidate_links AS (
                SELECT
                    dl.bookid,
                    dl.name,
                    CASE
                        WHEN LOWER(dl.name) LIKE '%-h.zip'
                          OR LOWER(dl.name) LIKE '%-h.zip?%'
                          OR LOWER(dl.name) LIKE '%-h.zip#%' THEN '{SOURCE_HZIP}'
                        WHEN LOWER(dl.name) LIKE '%.html.images'
                          OR LOWER(dl.name) LIKE '%.html.images?%'
                          OR LOWER(dl.name) LIKE '%.html.images#%'
                          OR LOWER(dl.name) LIKE '%.htm.images'
                          OR LOWER(dl.name) LIKE '%.htm.images?%'
                          OR LOWER(dl.name) LIKE '%.htm.images#%' THEN '{SOURCE_HTML_IMAGES}'
                        WHEN LOWER(dl.name) LIKE '%.epub3.images'
                          OR LOWER(dl.name) LIKE '%.epub3.images?%'
                          OR LOWER(dl.name) LIKE '%.epub3.images#%' THEN '{SOURCE_EPUB3_IMAGES}'
                        WHEN (
                            LOWER(dl.name) LIKE '%.epub.images'
                            OR LOWER(dl.name) LIKE '%.epub.images?%'
                            OR LOWER(dl.name) LIKE '%.epub.images#%'
                        ) AND LOWER(dl.name) NOT LIKE '%.epub.noimages%' THEN '{SOURCE_EPUB_IMAGES}'
                        ELSE NULL
                    END AS source_type
                FROM downloadlinks dl
                WHERE (
                    (
                        dl.downloadtypeid = {HTML_ZIP_DOWNLOAD_TYPE}
                        AND (
                            LOWER(dl.name) LIKE '%-h.zip'
                            OR LOWER(dl.name) LIKE '%-h.zip?%'
                            OR LOWER(dl.name) LIKE '%-h.zip#%'
                        )
                    )
                    OR LOWER(dl.name) LIKE '%.html.images'
                    OR LOWER(dl.name) LIKE '%.html.images?%'
                    OR LOWER(dl.name) LIKE '%.html.images#%'
                    OR LOWER(dl.name) LIKE '%.htm.images'
                    OR LOWER(dl.name) LIKE '%.htm.images?%'
                    OR LOWER(dl.name) LIKE '%.htm.images#%'
                    OR LOWER(dl.name) LIKE '%.epub3.images'
                    OR LOWER(dl.name) LIKE '%.epub3.images?%'
                    OR LOWER(dl.name) LIKE '%.epub3.images#%'
                    OR (
                        (
                            LOWER(dl.name) LIKE '%.epub.images'
                            OR LOWER(dl.name) LIKE '%.epub.images?%'
                            OR LOWER(dl.name) LIKE '%.epub.images#%'
                        )
                        AND LOWER(dl.name) NOT LIKE '%.epub.noimages%'
                    )
                )
            ),
            ranked_links AS (
                SELECT
                    cl.bookid,
                    cl.name,
                    cl.source_type,
                    ROW_NUMBER() OVER (
                        PARTITION BY cl.bookid
                        ORDER BY
                            CASE cl.source_type
                                WHEN '{SOURCE_HZIP}' THEN 0
                                WHEN '{SOURCE_HTML_IMAGES}' THEN 1
                                WHEN '{SOURCE_EPUB3_IMAGES}' THEN 2
                                WHEN '{SOURCE_EPUB_IMAGES}' THEN 3
                                ELSE 9
                            END,
                            CASE
                                WHEN cl.name LIKE 'https://www.gutenberg.org/cache/epub/%' THEN 0
                                WHEN cl.name LIKE 'https://www.gutenberg.org/files/%' THEN 1
                                WHEN cl.name LIKE 'https://www.gutenberg.org/ebooks/%' THEN 2
                                ELSE 3
                            END,
                            LENGTH(cl.name),
                            cl.name
                    ) AS rn
                FROM candidate_links cl
                WHERE cl.source_type IS NOT NULL
            )
            SELECT
                b.id AS book_id,
                b.gutenbergbookid AS gutenberg_id,
                COALESCE((SELECT name FROM titles WHERE bookid = b.id LIMIT 1), 'Untitled') AS title,
                rl.name AS source_url,
                rl.source_type
            FROM books b
            JOIN ranked_links rl
              ON rl.bookid = b.id
             AND rl.rn = 1
            WHERE {where}
            ORDER BY b.numdownloads DESC
            {limit_clause}
            """,
            params,
        )
        return [
            HtmlBookRow(
                book_id=int(row[0]),
                gutenberg_id=int(row[1]),
                title=row[2] or "Untitled",
                source_url=row[3],
                source_type=row[4] or SOURCE_HZIP,
            )
            for row in cur
        ]
    finally:
        conn.close()


def _load_source_candidates_for_book(
    db_path: str,
    *,
    book_id: int,
    primary_url: str | None,
) -> list[tuple[str, str]]:
    conn = _connect_db(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM downloadlinks WHERE bookid = ?", (book_id,))
        all_urls = [row[0] for row in cur if row and row[0]]
    finally:
        conn.close()

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        if not url or url in seen:
            return
        source_type = _classify_source_url(url)
        if source_type is None:
            return
        seen.add(url)
        candidates.append((url, source_type))

    if primary_url:
        _add(primary_url)
    for url in all_urls:
        _add(url)

    candidates.sort(key=lambda item: _source_sort_key(item[0]))

    if primary_url and primary_url in seen:
        primary = next((item for item in candidates if item[0] == primary_url), None)
        if primary is not None:
            candidates = [primary] + [item for item in candidates if item[0] != primary_url]

    return candidates


def _seed_queue(db_path: str, books: list[HtmlBookRow]) -> None:
    def _query() -> None:
        conn = _connect_db(db_path)
        try:
            conn.executemany(
                """
                INSERT INTO book_content_backfill_queue
                    (queue_key, bookid, gutenbergbookid, title, source_url, source_type, status, attempts)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0)
                ON CONFLICT(queue_key, bookid) DO UPDATE SET
                    source_url = excluded.source_url,
                    source_type = excluded.source_type,
                    status = CASE WHEN status IN ('done', 'skipped') THEN status ELSE 'pending' END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        QUEUE_KEY,
                        b.book_id,
                        b.gutenberg_id,
                        b.title,
                        b.source_url,
                        b.source_type,
                    )
                    for b in books
                ],
            )
            conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(_query)


def _queue_status(db_path: str) -> dict[str, int]:
    """Return per-status row counts for QUEUE_KEY — used to show resumable run state."""
    conn = _connect_db(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, COUNT(*) FROM book_content_backfill_queue"
            " WHERE queue_key = ? GROUP BY status",
            (QUEUE_KEY,),
        )
        return {row[0]: row[1] for row in cur}
    finally:
        conn.close()


def _reset_queue(db_path: str) -> int:
    def _query() -> int:
        conn = _connect_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM book_content_backfill_queue WHERE queue_key=?",
                (QUEUE_KEY,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    return with_sqlite_retry(_query)


def _load_pending(
    db_path: str,
    max_attempts: int,
    *,
    only_book_ids: set[int] | None = None,
) -> list[tuple[int, int, str, str]]:
    conn = _connect_db(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT bookid, gutenbergbookid, title, source_url
            FROM book_content_backfill_queue
            WHERE queue_key = ? AND status IN ('pending', 'failed') AND attempts < ?
            ORDER BY priority DESC, bookid
            """,
            (QUEUE_KEY, max_attempts),
        )
        rows = [(int(r[0]), int(r[1]), r[2], r[3]) for r in cur]
        if only_book_ids is not None:
            rows = [row for row in rows if row[0] in only_book_ids]
        return rows
    finally:
        conn.close()


def _update_queue_status(
    db_path: str,
    book_id: int,
    *,
    status: str,
    attempt_delta: int = 0,
    last_error: str | None = None,
) -> None:
    """Persist the latest queue status so interrupted runs can resume cleanly."""
    def _query() -> None:
        conn = _connect_db(db_path)
        try:
            conn.execute(
                """
                UPDATE book_content_backfill_queue
                SET status = ?,
                    attempts = attempts + ?,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE queue_key = ? AND bookid = ?
                """,
                (status, attempt_delta, last_error, QUEUE_KEY, book_id),
            )
            conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(_query)


def _save_html_content(db_path: str, book_id: int, html_content: str) -> None:
    """Persist cleaned HTML and set has_images=1 without touching raw_content/clean_content."""
    def _query() -> None:
        conn = _connect_db(db_path)
        try:
            conn.execute(
                """
                INSERT INTO book_contents
                    (bookid, raw_content, clean_content, html_content, has_images, download_date)
                VALUES (?, '', '', ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(bookid) DO UPDATE SET
                    html_content = excluded.html_content,
                    has_images   = 1
                """,
                (book_id, html_content),
            )
            conn.commit()
        finally:
            conn.close()

    with_sqlite_retry(_query)


# ---------------------------------------------------------------------------
# HTML / zip processing
# ---------------------------------------------------------------------------

# Patterns that identify Gutenberg license/administrative text at the front and
# back of a book's HTML.  Only applied to top-level body children so mid-book
# mentions of "Project Gutenberg" (e.g. in a history book) are never stripped.
_GUTENBERG_PREAMBLE_RE = re.compile(
    r"project\s+gutenberg|gutenberg-tm|www\.gutenberg\.org"
    r"|this\s+ebook\s+is\s+for\s+the\s+use\s+of\s+anyone"
    r"|\*{3,}\s*start\s+of",
    re.IGNORECASE,
)
_GUTENBERG_TRAILER_RE = re.compile(
    r"\*{3,}\s*end\s+of"
    r"|end\s+of\s+(?:this\s+)?(?:the\s+)?project\s+gutenberg"
    r"|updated\s+editions\s+will\s+replace"
    r"|this\s+file\s+should\s+be\s+named"
    r"|project\s+gutenberg-tm\s+is\s+a\s+registered\s+trademark",
    re.IGNORECASE,
)

_IMAGE_EXTS = frozenset([".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"])

# Matches src= and href= attributes that point to image files.
# Handles both single-quoted and double-quoted attribute values.
_IMG_ATTR_RE = re.compile(
    r'((?:src|href)\s*=\s*)(["\'])([^"\']+\.(?:png|jpe?g|gif|svg|webp)(?:[?#][^"\']*)?)(\2)',
    re.IGNORECASE,
)


def _strip_gutenberg_html_nodes(body) -> None:
    """Remove Gutenberg preamble/trailer nodes from the start and end of <body>."""
    children = list(body)

    drop_front: list = []
    for child in children:
        text = "".join(child.itertext()).strip()
        if not text or _GUTENBERG_PREAMBLE_RE.search(text):
            drop_front.append(child)
        else:
            break

    remaining = [c for c in children if c not in drop_front]
    drop_back: list = []
    for child in reversed(remaining):
        text = "".join(child.itertext()).strip()
        if not text or _GUTENBERG_TRAILER_RE.search(text):
            drop_back.append(child)
        else:
            break

    for el in drop_front + drop_back:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _clean_gutenberg_html(raw_html: str, book_id: int, uploaded_filenames: set[str]) -> str:
    """
    Strip Gutenberg boilerplate and CSS from HTML, rewrite image paths to the
    Flask /api/books/{id}/images/ route, and return a browser-ready HTML string.
    Falls back to plain path-rewrite if lxml is unavailable.
    """
    if _HAS_LXML:
        try:
            doc = _lxml_html.document_fromstring(raw_html)
            for el in doc.xpath("//style|//script|//link"):
                el.drop_tree()
            body_list = doc.xpath("//body")
            if body_list:
                _strip_gutenberg_html_nodes(body_list[0])
            raw_html = _lxml_html.tostring(doc, encoding="unicode", method="html")
        except Exception:
            pass

    return _rewrite_image_paths(raw_html, book_id, uploaded_filenames)


def _find_main_html(zf: zipfile.ZipFile, gutenberg_id: int) -> str | None:
    names = zf.namelist()
    preferred = [
        f"pg{gutenberg_id}-h.htm",
        f"{gutenberg_id}-h.htm",
        f"pg{gutenberg_id}-h.html",
        f"{gutenberg_id}-h.html",
    ]
    for suffix in preferred:
        for name in names:
            if name.endswith(suffix):
                return name
    for name in names:
        if name.lower().endswith((".htm", ".html")):
            return name
    return None


def _rewrite_image_paths(html: str, book_id: int, uploaded_filenames: set[str]) -> str:
    def _replace(m: re.Match) -> str:
        attr_open = m.group(1)
        quote = m.group(2)
        path = m.group(3)
        attr_close = m.group(4)
        path_without_query = path.split("?", 1)[0].split("#", 1)[0]
        filename = Path(path_without_query).name
        if filename in uploaded_filenames:
            return f'{attr_open}{quote}/api/books/{book_id}/images/{filename}{attr_close}'
        return m.group(0)

    return _IMG_ATTR_RE.sub(_replace, html)


def _referenced_image_filenames(html: str) -> set[str]:
    filenames: set[str] = set()
    for match in _IMG_ATTR_RE.finditer(html):
        path = match.group(3)
        path_without_query = path.split("?", 1)[0].split("#", 1)[0]
        name = Path(path_without_query).name
        if name:
            filenames.add(name)
    return filenames


def _referenced_image_paths(html: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _IMG_ATTR_RE.finditer(html):
        value = match.group(3)
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _download_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def _extract_html_body(html_text: str) -> str:
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if body_match:
        return body_match.group(1)

    # XHTML and older Gutenberg files occasionally omit a strict body wrapper.
    text = re.sub(r"<\?xml[^>]*\?>", "", html_text, flags=re.IGNORECASE)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.IGNORECASE)
    return text


def _epub_spine_documents(zf: zipfile.ZipFile) -> list[str]:
    names = set(zf.namelist())
    if "META-INF/container.xml" not in names:
        return []

    try:
        container_root = ET.fromstring(zf.read("META-INF/container.xml"))
    except Exception:
        return []

    container_ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = container_root.find(".//c:rootfile", container_ns)
    if rootfile is None:
        rootfile = container_root.find(".//rootfile")
    if rootfile is None:
        return []

    opf_rel = rootfile.attrib.get("full-path", "").strip()
    if not opf_rel or opf_rel not in names:
        return []

    try:
        opf_root = ET.fromstring(zf.read(opf_rel))
    except Exception:
        return []

    if opf_root.tag.startswith("{"):
        opf_ns_uri = opf_root.tag[1:].split("}", 1)[0]
        ns = {"opf": opf_ns_uri}
        manifest_items = opf_root.findall(".//opf:manifest/opf:item", ns)
        spine_items = opf_root.findall(".//opf:spine/opf:itemref", ns)
    else:
        manifest_items = opf_root.findall(".//manifest/item")
        spine_items = opf_root.findall(".//spine/itemref")

    manifest: dict[str, tuple[str, str]] = {}
    for item in manifest_items:
        item_id = item.attrib.get("id", "").strip()
        href = item.attrib.get("href", "").strip()
        media_type = item.attrib.get("media-type", "").strip().lower()
        if not item_id or not href:
            continue
        manifest[item_id] = (href, media_type)

    opf_dir = posixpath.dirname(opf_rel)
    ordered: list[str] = []
    seen: set[str] = set()
    for item in spine_items:
        idref = item.attrib.get("idref", "").strip()
        if not idref or idref not in manifest:
            continue
        href, media_type = manifest[idref]
        if media_type and "html" not in media_type and "xhtml" not in media_type:
            continue
        normalized = posixpath.normpath(posixpath.join(opf_dir, href)).lstrip("/")
        if normalized in names and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _build_epub_html(zf: zipfile.ZipFile) -> str | None:
    names = zf.namelist()
    doc_paths = _epub_spine_documents(zf)
    if not doc_paths:
        doc_paths = sorted(
            n
            for n in names
            if n.lower().endswith((".xhtml", ".html", ".htm"))
        )

    sections: list[str] = []
    for idx, path in enumerate(doc_paths, 1):
        try:
            doc = zf.read(path).decode("utf-8", errors="replace")
        except Exception:
            continue
        body = _extract_html_body(doc).strip()
        if not body:
            continue
        escaped_path = html_module.escape(path, quote=True)
        sections.append(
            f'<section id="epub-chapter-{idx}" data-epub-src="{escaped_path}">\n{body}\n</section>'
        )

    if not sections:
        return None

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>\n"
        + "\n".join(sections)
        + "\n</body></html>"
    )


def _collect_linked_image_payloads_from_zip(
    zf: zipfile.ZipFile,
    raw_html: str,
) -> dict[str, bytes]:
    all_image_zip_paths = [n for n in zf.namelist() if Path(n).suffix.lower() in _IMAGE_EXTS]
    all_image_filenames = {Path(p).name for p in all_image_zip_paths}
    referenced_images = _referenced_image_filenames(raw_html)
    linked_image_filenames = all_image_filenames.intersection(referenced_images)

    payloads: dict[str, bytes] = {}
    for zip_path in all_image_zip_paths:
        filename = Path(zip_path).name
        if filename not in linked_image_filenames or filename in payloads:
            continue
        payloads[filename] = zf.read(zip_path)
    return payloads


_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "webp": "image/webp",
}


def _upload_image_payloads(
    image_payloads: dict[str, bytes],
    gutenberg_id: int,
    gcs_bucket: str,
    gcs_client,
    *,
    force_images: bool,
) -> set[str]:
    bucket = gcs_client.bucket(gcs_bucket)
    uploaded: set[str] = set()

    for filename, data in image_payloads.items():
        blob_path = f"{GCS_PREFIX}/{gutenberg_id}/images/{filename}"
        blob = bucket.blob(blob_path)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content_type = _MIME.get(ext, "application/octet-stream")

        # Default mode: create-if-missing only. Existing objects are preserved.
        # Force mode: overwrite existing object bytes.
        if force_images:
            blob.upload_from_string(data, content_type=content_type)
            uploaded.add(filename)
            continue

        # Avoid requiring GET/list permission: create-if-not-exists upload first.
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except TypeError as exc:
            # Older clients that do not support if_generation_match cannot safely
            # guarantee non-overwrite semantics; fail closed in non-force mode.
            raise RuntimeError(
                "GCS client does not support create-if-missing uploads; "
                "upgrade google-cloud-storage or run with --force-images."
            ) from exc
        except Exception as exc:
            message = str(exc).lower()
            if "412" not in message and "conditionnotmet" not in message and "precondition" not in message:
                raise
        uploaded.add(filename)

    return uploaded


def _persist_html_with_images(
    db_path: str,
    *,
    book_id: int,
    gutenberg_id: int,
    raw_html: str,
    image_payloads: dict[str, bytes],
    gcs_bucket: str,
    gcs_client,
    dry_run: bool,
    force_images: bool,
) -> tuple[str, str]:
    if not image_payloads:
        return "skip", "no inline images referenced in html; keeping text content"

    if dry_run:
        return "dry_run", f"{len(image_payloads)} images, {len(raw_html):,} chars HTML"

    if gcs_client is not None:
        try:
            uploaded = _upload_image_payloads(
                image_payloads,
                gutenberg_id,
                gcs_bucket,
                gcs_client,
                force_images=force_images,
            )
        except Exception as exc:
            return "error", f"GCS upload failed: {exc}"
    else:
        uploaded = set(image_payloads.keys())

    html_content = _clean_gutenberg_html(raw_html, book_id, uploaded)
    try:
        _save_html_content(db_path, book_id, html_content)
    except Exception as exc:
        return "error", f"DB save failed: {exc}"
    return "done", f"{len(uploaded)} images"


def _process_hzip_source(
    db_path: str,
    *,
    book_id: int,
    gutenberg_id: int,
    source_url: str,
    gcs_bucket: str,
    gcs_client,
    dry_run: bool,
    force_images: bool,
) -> tuple[str, str]:
    try:
        payload = _download_bytes(source_url)
    except Exception as exc:
        return "error", f"download failed: {exc}"

    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        return "skip", f"bad zip: {exc}"

    main_html_path = _find_main_html(zf, gutenberg_id)
    if not main_html_path:
        return "skip", "no HTML file found in zip"

    try:
        raw_html = zf.read(main_html_path).decode("utf-8", errors="replace")
    except Exception as exc:
        return "error", f"HTML read failed: {exc}"

    image_payloads = _collect_linked_image_payloads_from_zip(zf, raw_html)
    return _persist_html_with_images(
        db_path,
        book_id=book_id,
        gutenberg_id=gutenberg_id,
        raw_html=raw_html,
        image_payloads=image_payloads,
        gcs_bucket=gcs_bucket,
        gcs_client=gcs_client,
        dry_run=dry_run,
        force_images=force_images,
    )


def _process_html_images_source(
    db_path: str,
    *,
    book_id: int,
    gutenberg_id: int,
    source_url: str,
    gcs_bucket: str,
    gcs_client,
    dry_run: bool,
    force_images: bool,
) -> tuple[str, str]:
    try:
        html_bytes = _download_bytes(source_url)
    except Exception as exc:
        return "error", f"download failed: {exc}"

    raw_html = html_bytes.decode("utf-8", errors="replace")
    ref_paths = _referenced_image_paths(raw_html)
    if not ref_paths:
        return "skip", "html.images has no inline image references"

    image_payloads: dict[str, bytes] = {}
    for ref in ref_paths:
        stripped = ref.split("?", 1)[0].split("#", 1)[0]
        filename = Path(stripped).name
        if not filename or filename in image_payloads:
            continue
        image_url = urljoin(source_url, ref)
        parsed = urlsplit(image_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        try:
            image_payloads[filename] = _download_bytes(image_url)
        except Exception:
            continue

    if not image_payloads:
        return "skip", "html.images referenced images but none were downloadable"

    return _persist_html_with_images(
        db_path,
        book_id=book_id,
        gutenberg_id=gutenberg_id,
        raw_html=raw_html,
        image_payloads=image_payloads,
        gcs_bucket=gcs_bucket,
        gcs_client=gcs_client,
        dry_run=dry_run,
        force_images=force_images,
    )


def _process_epub_images_source(
    db_path: str,
    *,
    book_id: int,
    gutenberg_id: int,
    source_url: str,
    gcs_bucket: str,
    gcs_client,
    dry_run: bool,
    force_images: bool,
) -> tuple[str, str]:
    try:
        payload = _download_bytes(source_url)
    except Exception as exc:
        return "error", f"download failed: {exc}"

    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        return "skip", f"bad epub zip: {exc}"

    raw_html = _build_epub_html(zf)
    if not raw_html:
        return "skip", "epub had no readable html/xhtml content"

    image_payloads = _collect_linked_image_payloads_from_zip(zf, raw_html)
    return _persist_html_with_images(
        db_path,
        book_id=book_id,
        gutenberg_id=gutenberg_id,
        raw_html=raw_html,
        image_payloads=image_payloads,
        gcs_bucket=gcs_bucket,
        gcs_client=gcs_client,
        dry_run=dry_run,
        force_images=force_images,
    )


# ---------------------------------------------------------------------------
# Per-book worker
# ---------------------------------------------------------------------------

def _process_book(
    db_path: str,
    book_id: int,
    gutenberg_id: int,
    source_url: str,
    gcs_bucket: str,
    gcs_client,
    dry_run: bool,
    force_images: bool,
) -> tuple[str, str]:
    """Try image-capable variants in deterministic order for a single book."""
    candidates = _load_source_candidates_for_book(
        db_path,
        book_id=book_id,
        primary_url=source_url,
    )
    if not candidates:
        return "skip", "no image-capable download variants found"

    attempts: list[str] = []
    saw_error = False
    for candidate_url, source_type in candidates:
        if source_type == SOURCE_HZIP:
            status, msg = _process_hzip_source(
                db_path,
                book_id=book_id,
                gutenberg_id=gutenberg_id,
                source_url=candidate_url,
                gcs_bucket=gcs_bucket,
                gcs_client=gcs_client,
                dry_run=dry_run,
                force_images=force_images,
            )
        elif source_type == SOURCE_HTML_IMAGES:
            status, msg = _process_html_images_source(
                db_path,
                book_id=book_id,
                gutenberg_id=gutenberg_id,
                source_url=candidate_url,
                gcs_bucket=gcs_bucket,
                gcs_client=gcs_client,
                dry_run=dry_run,
                force_images=force_images,
            )
        elif source_type in {SOURCE_EPUB3_IMAGES, SOURCE_EPUB_IMAGES}:
            status, msg = _process_epub_images_source(
                db_path,
                book_id=book_id,
                gutenberg_id=gutenberg_id,
                source_url=candidate_url,
                gcs_bucket=gcs_bucket,
                gcs_client=gcs_client,
                dry_run=dry_run,
                force_images=force_images,
            )
        else:
            status, msg = "skip", f"unsupported source type: {source_type}"

        attempts.append(f"{source_type} {status}: {msg}")
        if status in {"done", "dry_run"}:
            return status, f"{source_type}: {msg}"
        if status == "error":
            saw_error = True

    joined = " | ".join(attempts)[:500]
    if saw_error:
        return "error", joined
    return "skip", joined


# ---------------------------------------------------------------------------
# GCS client factory
# ---------------------------------------------------------------------------

def _make_gcs_client(credentials_path: str | None):
    # Signed URLs require a service account key for signing. If no key file is
    # provided explicitly, the client falls back to GOOGLE_APPLICATION_CREDENTIALS
    # or Application Default Credentials (ADC) from the metadata server on GCE.
    # Note: ADC via the metadata server cannot sign blobs directly — if that case
    # is hit, the Flask image route will fail at signing time, not here.
    if not HAS_GCS:
        raise RuntimeError(
            "google-cloud-storage is required for HTML backfill image uploads. "
            "Install with: pip install google-cloud-storage"
        )
    try:
        if credentials_path:
            from google.oauth2 import service_account as sa
            creds = sa.Credentials.from_service_account_file(credentials_path)
            return gcs_storage.Client(credentials=creds)
        return gcs_storage.Client()
    except Exception as exc:
        raise RuntimeError(f"Could not create GCS client: {exc}") from exc


# ---------------------------------------------------------------------------
# SSL helpers — match pattern used by backfill_missing_book_contents.py
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def backfill(
    db_path: str,
    *,
    gcs_bucket: str | None = DEFAULT_GCS_BUCKET,
    gcs_credentials: str | None = None,
    book_ids: list[int] | None = None,
    gutenberg_ids: list[int] | None = None,
    force: bool = False,
    dry_run: bool = False,
    reset_queue: bool = False,
    status_only: bool = False,
    workers: int = 4,
    chunk_size: int = 100,
    max_attempts: int = 3,
    limit: int | None = None,
    progress_every: int = 50,
    force_images: bool = False,
) -> None:
    print(f"stage: initialising db={db_path}", flush=True)
    _ensure_tables(db_path)

    # Show previous run state before any modifications
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

    print("stage: discovering books with image-capable downloads", flush=True)
    books = _load_target_books(
        db_path,
        book_ids=book_ids,
        gutenberg_ids=gutenberg_ids,
        force=force,
        limit=limit,
    )
    print(f"summary: target_books={len(books)}", flush=True)

    if not books:
        print("done: nothing to do", flush=True)
        return

    _seed_queue(db_path, books)

    restrict_to_targets = bool(book_ids or gutenberg_ids or limit)
    target_book_ids = {b.book_id for b in books} if restrict_to_targets else None
    pending = _load_pending(db_path, max_attempts, only_book_ids=target_book_ids)
    already_done = len(books) - len(pending)
    print(
        f"summary: queued={len(pending)}  already_done={already_done}  max_attempts={max_attempts}",
        flush=True,
    )

    if not pending:
        print("done: queue is empty — all books already processed", flush=True)
        return

    if dry_run:
        print("stage: dry-run enabled; no writes will be committed", flush=True)
    if force_images:
        print("summary: image_upload_mode=force-overwrite", flush=True)
    else:
        print("summary: image_upload_mode=create-if-missing", flush=True)

    gcs_client = None if dry_run else _make_gcs_client(gcs_credentials)

    print(
        f"stage: downloading with workers={workers} chunk_size={chunk_size}",
        flush=True,
    )

    done = skipped = failed = 0
    total = len(pending)
    t0 = time.monotonic()

    def _worker(item: tuple) -> tuple[int, str, str]:
        bid, gid, _title, source_url = item
        status, msg = _process_book(
            db_path, bid, gid, source_url, gcs_bucket, gcs_client, dry_run, force_images,
        )
        return bid, status, msg

    for chunk_start in range(0, total, chunk_size):
        chunk = pending[chunk_start : chunk_start + chunk_size]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker, item): item for item in chunk}
            for i, future in enumerate(as_completed(futures), 1):
                bid, status, msg = future.result()
                if status == "done":
                    _update_queue_status(db_path, bid, status="done")
                    done += 1
                elif status in ("skip", "dry_run"):
                    _update_queue_status(db_path, bid, status="skipped", last_error=msg[:500])
                    skipped += 1
                else:
                    _update_queue_status(
                        db_path, bid, status="failed",
                        attempt_delta=1, last_error=msg[:500],
                    )
                    failed += 1

                processed = chunk_start + i
                if processed % progress_every == 0 or processed == total:
                    elapsed = time.monotonic() - t0
                    rate = processed / elapsed if elapsed else 0
                    print(
                        f"stage: progress processed={processed}/{total}"
                        f" done={done} skipped={skipped} failed={failed}"
                        f" rate={rate:.1f}/s"
                        f" last={bid} {status}: {msg[:80]}",
                        flush=True,
                    )

    print(
        f"done: total={total} done={done} skipped={skipped} failed={failed}"
        f" already_done={already_done}",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Backfill Gutenberg HTML+images into book_contents using "
            "-h.zip, html.images, and epub*.images fallbacks."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", type=Path, default=DB_PATH, help="Path to gutenbergindex.db")
    p.add_argument("--gcs-bucket", default=DEFAULT_GCS_BUCKET, help="GCS bucket name")
    p.add_argument(
        "--gcs-credentials",
        default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        metavar="KEY_FILE",
        help="Service account JSON key file. Defaults to GOOGLE_APPLICATION_CREDENTIALS env var.",
    )
    p.add_argument("--book-ids", default=None, metavar="IDS", help="Comma-separated internal book IDs")
    p.add_argument("--gutenberg-ids", default=None, metavar="IDS", help="Comma-separated Gutenberg IDs")
    p.add_argument("--force", action="store_true", help="Re-process books that already have HTML content")
    p.add_argument(
        "--force-images",
        action="store_true",
        help="Overwrite existing image objects in GCS. Default behavior preserves existing images.",
    )
    p.add_argument("--dry-run", action="store_true", help="Discover and log without downloading or writing")
    p.add_argument("--reset-queue", action="store_true", help="Delete all queue rows before running")
    p.add_argument("--status", action="store_true", help="Print queue state and exit without processing")
    p.add_argument("--workers", type=int, default=4, help="Parallel download workers (default: 4)")
    p.add_argument("--chunk-size", type=int, default=100, help="Queue chunk size per executor batch (default: 100)")
    p.add_argument("--max-attempts", type=int, default=3, help="Skip items with this many failed attempts (default: 3)")
    p.add_argument("--limit", type=int, default=None, metavar="N", help="Stop after discovering N books (useful for test runs)")
    p.add_argument("--progress-every", type=int, default=50, help="Print progress every N books (default: 50)")
    p.add_argument("--ca-bundle", dest="cafile", help="Path to a PEM CA bundle file.")
    p.add_argument("--ca-dir", dest="capath", help="Path to a directory of CA certificates.")
    p.add_argument(
        "--no-verify",
        action="store_false",
        dest="verify",
        default=True,
        help="Disable SSL certificate verification (workaround for macOS cert issues).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cafile, capath = _resolve_ca_paths(args.cafile, args.capath, args.verify)
    _install_https_opener(cafile, capath, args.verify)

    book_ids = [int(x) for x in args.book_ids.split(",")] if args.book_ids else None
    gutenberg_ids = [int(x) for x in args.gutenberg_ids.split(",")] if args.gutenberg_ids else None

    backfill(
        str(args.db),
        gcs_bucket=args.gcs_bucket,
        gcs_credentials=args.gcs_credentials,
        book_ids=book_ids,
        gutenberg_ids=gutenberg_ids,
        force=args.force,
        dry_run=args.dry_run,
        reset_queue=args.reset_queue,
        status_only=args.status,
        workers=args.workers,
        chunk_size=args.chunk_size,
        max_attempts=args.max_attempts,
        limit=args.limit,
        progress_every=args.progress_every,
        force_images=args.force_images,
    )


if __name__ == "__main__":
    main()
