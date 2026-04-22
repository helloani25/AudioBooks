from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from functools import lru_cache
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import zipfile

from gutenbergpy.textget import strip_headers
from AudioBooks.Catalog.Gutenberg.db_utils import (
    connect_db as _connect_db,
    ensure_book_contents_table as _ensure_book_contents_table,
    with_sqlite_retry as _with_sqlite_retry,
)

try:
    from lxml import html as lxml_html
except ImportError:
    lxml_html = None

try:
    import chardet
except ImportError:
    chardet = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


MIN_BOOK_TEXT_CHARS = 400
MIN_BOOK_TEXT_WORDS = 40
KNOWN_FOLIO_WARNING_BOOK_IDS = {900}


SUPPORTED_DOWNLOAD_TYPES = [
    "text/plain",
    "text/plain; charset=utf-8",
    "text/plain; charset=us-ascii",
    "text/plain; charset=iso-8859-1",
    "text/plain; charset=windows-1252",
    "text/html",
    "text/html; charset=utf-8",
    "text/html; charset=us-ascii",
    "text/html; charset=iso-8859-1",
    "text/html; charset=windows-1252",
    "application/prs.tei",
    "application/epub+zip",
    "application/pdf",
    "application/octet-stream",
]

DOWNLOAD_TYPE_PRIORITY = {download_type: idx for idx, download_type in enumerate(SUPPORTED_DOWNLOAD_TYPES)}
MIRRORS_URL = "https://www.gutenberg.org/MIRRORS.ALL"
GUTENBERG_FILES_URL = "https://www.gutenberg.org/files"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill book_contents with missing Project Gutenberg texts from the local Gutenberg catalog.",
    )
    parser.add_argument(
        "--db-path",
        default=str(DB_PATH),
        help="Path to the SQLite database file.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after processing this many missing books.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and parse books without writing to SQLite.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Classify missing books before downloading and prefer live index candidates for repair/refresh cases.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Print the preflight classification and exit without downloading.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent download workers.",
    )
    parser.add_argument(
        "--mirror-tries",
        type=int,
        default=3,
        help="How many mirrors to try per book before failing over.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="How many Gutenberg ids to resolve per SQL batch when finding download links.",
    )
    parser.add_argument(
        "--ca-bundle",
        dest="cafile",
        help="Path to a PEM CA bundle file.",
    )
    parser.add_argument(
        "--ca-dir",
        dest="capath",
        help="Path to a directory of CA certificates.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_false",
        dest="verify",
        default=True,
        help="Disable SSL certificate verification.",
    )
    return parser.parse_args()


def _decode_text_bytes(text_bytes: bytes) -> str:
    detected = chardet.detect(text_bytes).get("encoding") if chardet else None
    for encoding in (detected, "utf-8", "cp1252", "latin-1"):
        if not encoding:
            continue
        try:
            return text_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return text_bytes.decode("utf-8", errors="replace")


def _tex_bytes_to_text(tex_bytes: bytes) -> str:
    text = _decode_text_bytes(tex_bytes)
    replacements = {
        r"\\%": "%",
        r"\\$": "$",
        r"\\_": "_",
        r"\\&": "&",
        r"\\#": "#",
        r"\\textbackslash{}": "\\",
        r"\\textasciitilde{}": "~",
        r"\\textasciicircum{}": "^",
    }
    for pattern, replacement in replacements.items():
        text = text.replace(pattern, replacement)
    text = re.sub(r"\\\\(?:[a-zA-Z]+)(?:\[[^\]]*\])?(?:\{[^{}]*\})?", " ", text)
    text = re.sub(r"\\begin\{[^{}]*\}|\\end\{[^{}]*\}", " ", text)
    text = re.sub(r"\$\$?|\\\[|\\\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _pdf_bytes_to_text(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        raise RuntimeError(
            "PDF fallback requires the optional 'pypdf' dependency. "
            "Install it to extract text from Project Gutenberg PDF downloads."
        )

    reader = PdfReader(BytesIO(pdf_bytes))
    chunks: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text.strip():
            chunks.append(page_text.strip())

    text = "\n\n".join(chunks).strip()
    if not text:
        raise ValueError("No extractable text found in PDF download")
    return text


def _get_book_title(db_path: str, gutenberg_id: int) -> str:
    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled')
            FROM books b
            WHERE b.gutenbergbookid = ?
            """,
            (gutenberg_id,),
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else "Untitled"

    return _with_sqlite_retry(query)


def _looks_like_warning_text(text: str) -> bool:
    lower_text = text.lower()
    warning_markers = (
        "do not download",
        "obsolete format",
        "warning",
        "see #",
        "alternative ids",
    )
    return any(marker in lower_text for marker in warning_markers)


def _looks_like_real_book_text(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < MIN_BOOK_TEXT_CHARS:
        return False
    if _looks_like_warning_text(stripped):
        return False
    words = re.findall(r"[A-Za-z0-9]{3,}", stripped)
    return len(words) >= MIN_BOOK_TEXT_WORDS


def _chunked(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


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


LIVE_DOWNLOAD_TYPE_BY_SUFFIX = [
    ((".txt.utf-8", ".txt.us-ascii", ".txt"), "text/plain"),
    ((".html.utf-8", ".htm.utf-8"), "text/html; charset=utf-8"),
    ((".html.us-ascii", ".htm.us-ascii"), "text/html; charset=us-ascii"),
    ((".html", ".htm", ".xhtml"), "text/html"),
    ((".tei.utf-8", ".tei"), "application/prs.tei"),
    ((".epub", ".epub.noimages", ".epub.images"), "application/epub+zip"),
    ((".zip",), "application/octet-stream"),
    ((".pdf",), "application/pdf"),
    ((".tex",), "application/prs.tex"),
    ((".mp3",), "audio/mpeg"),
    ((".ogg",), "audio/ogg"),
    ((".m4a", ".mp4"), "audio/mp4"),
    ((".mid", ".midi"), "audio/midi"),
    ((".wav",), "audio/x-wav"),
    ((".wma",), "audio/x-ms-wma"),
    ((".mpeg", ".mpg"), "video/mpeg"),
    ((".avi",), "video/x-msvideo"),
    ((".mov",), "video/quicktime"),
    ((".flv",), "video/x-flv"),
]


def _infer_live_download_type(link_name: str) -> str | None:
    lower_name = link_name.lower()
    for suffixes, download_type in LIVE_DOWNLOAD_TYPE_BY_SUFFIX:
        if lower_name.endswith(suffixes):
            return download_type
    return None


def _is_live_content_url(url: str) -> bool:
    path_name = Path(urlsplit(url).path).name.lower()
    if not path_name or path_name.endswith("/"):
        return False
    if path_name.startswith("readme_warning") or path_name.endswith(".nfo") or path_name.endswith(".rdf"):
        return False
    download_type = _infer_live_download_type(path_name)
    return download_type is not None and download_type != "application/rdf+xml"


def _url_points_to_book_id(url: str, book_id: int) -> bool:
    path = urlsplit(url).path
    needle = str(book_id)
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


def _fetch_live_file_index_links(book_id: int) -> list[tuple[str, str]]:
    index_url = f"{GUTENBERG_FILES_URL}/{book_id}/"
    response = urllib.request.urlopen(index_url)
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
        if not _is_live_content_url(absolute_url):
            continue
        download_type = _infer_live_download_type(Path(urlsplit(absolute_url).path).name)
        if not download_type:
            continue
        seen.add(absolute_url)
        links.append((absolute_url, download_type))
    links.sort(key=lambda item: item[0])
    return links


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def get_text(self) -> str:
        return " ".join(part.strip() for part in self.parts if part.strip())


def _html_bytes_to_text(html_bytes: bytes) -> str:
    html_text = _decode_text_bytes(html_bytes)
    try:
        if lxml_html is not None:
            return lxml_html.fromstring(html_text).text_content()
        parser = _TextExtractor()
        parser.feed(html_text)
        return parser.get_text()
    except Exception:
        return html_text


def _xml_bytes_to_text(xml_bytes: bytes) -> str:
    try:
        root = ET.fromstring(xml_bytes)
        return " ".join(part.strip() for part in root.itertext() if part and part.strip())
    except Exception:
        return _decode_text_bytes(xml_bytes)


def _archive_bytes_to_text(archive_bytes: bytes) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        files = [
            name
            for name in archive.namelist()
            if name.lower().endswith((".xhtml", ".html", ".htm", ".xml", ".tei", ".txt", ".tex", ".pdf"))
        ]
        for name in sorted(files):
            try:
                file_bytes = archive.read(name)
                lower_name = name.lower()
                if lower_name.endswith(".txt"):
                    chunks.append(_decode_text_bytes(file_bytes).strip())
                elif lower_name.endswith(".tex"):
                    chunks.append(_tex_bytes_to_text(file_bytes).strip())
                elif lower_name.endswith(".pdf"):
                    chunks.append(_pdf_bytes_to_text(file_bytes).strip())
                elif lower_name.endswith((".xml", ".tei")):
                    chunks.append(_xml_bytes_to_text(file_bytes).strip())
                else:
                    chunks.append(_html_bytes_to_text(file_bytes).strip())
            except Exception:
                continue

    text = "\n\n".join(chunk for chunk in chunks if chunk)
    if not text.strip():
        raise ValueError("No extractable text found in archive download")
    return text


def _download_book_text(url: str, download_type: str) -> str:
    response = urllib.request.urlopen(url)
    try:
        payload = response.read()
    finally:
        response.close()

    if download_type.startswith("text/plain"):
        return _decode_text_bytes(payload)
    if download_type.startswith("text/html"):
        return _html_bytes_to_text(payload)
    if download_type in {"application/prs.tei"}:
        return _xml_bytes_to_text(payload)
    path_name = Path(urlsplit(url).path).name.lower()
    if download_type == "application/octet-stream" and path_name.endswith(".txt"):
        return _decode_text_bytes(payload)
    if download_type == "application/pdf" or payload.startswith(b"%PDF-"):
        return _pdf_bytes_to_text(payload)
    if download_type in {"application/epub+zip", "application/octet-stream"} and zipfile.is_zipfile(BytesIO(payload)):
        return _archive_bytes_to_text(payload)

    raise ValueError(f"Unsupported download type: {download_type}")


@lru_cache(maxsize=1)
def _get_mirrors() -> list[str]:
    response = urllib.request.urlopen(MIRRORS_URL)
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


def _resolve_ca_paths(cli_cafile, cli_capath, verify=True):
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


def _install_https_opener(cafile, capath, verify=True):
    if not verify:
        context = ssl._create_unverified_context()
    else:
        context = ssl.create_default_context()
        if cafile or capath:
            context.load_verify_locations(cafile=cafile, capath=capath)

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    urllib.request.install_opener(opener)
    ssl._create_default_https_context = lambda: context


def _get_missing_books(db_path: str) -> list[tuple[int, str]]:
    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                b.gutenbergbookid,
                COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
            FROM books b
            LEFT JOIN book_contents bc ON bc.bookid = b.gutenbergbookid
            WHERE b.gutenbergbookid IS NOT NULL
              AND bc.bookid IS NULL
            ORDER BY b.numdownloads DESC
            """
        )
        rows = [(int(row[0]), row[1]) for row in cur.fetchall()]
        conn.close()
        return rows

    return _with_sqlite_retry(query)


def _get_download_candidates(db_path: str, gutenberg_id: int) -> list[tuple[str, str]]:
    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(SUPPORTED_DOWNLOAD_TYPES))
        cur.execute(
            f"""
            SELECT DISTINCT d.name, dt.name
            FROM books b
            JOIN downloadlinks d ON d.bookid = b.id
            JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
            WHERE b.gutenbergbookid = ?
              AND dt.name IN ({placeholders})
            ORDER BY d.name
            """,
            [gutenberg_id, *SUPPORTED_DOWNLOAD_TYPES],
        )
        candidates = [(row[0], row[1]) for row in cur.fetchall()]
        conn.close()
        candidates.sort(key=lambda item: DOWNLOAD_TYPE_PRIORITY.get(item[1], 999))
        return candidates

    return _with_sqlite_retry(query)


def _get_all_download_candidates(db_path: str, gutenberg_id: int) -> list[tuple[str, str]]:
    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT d.name, dt.name
            FROM books b
            JOIN downloadlinks d ON d.bookid = b.id
            JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
            WHERE b.gutenbergbookid = ?
            ORDER BY d.name
            """,
            (gutenberg_id,),
        )
        candidates = [(row[0], row[1]) for row in cur.fetchall()]
        conn.close()
        return candidates

    return _with_sqlite_retry(query)


def _get_download_candidates_for_books(
    db_path: str,
    gutenberg_ids: list[int],
    *,
    chunk_size: int = 500,
) -> dict[int, list[tuple[str, str]]]:
    if not gutenberg_ids:
        return {}

    candidates_by_book: dict[int, list[tuple[str, str]]] = {book_id: [] for book_id in gutenberg_ids}

    def query_chunk(chunk: list[int]) -> list[tuple[int, str, str]]:
        conn = _connect_db(db_path)
        cur = conn.cursor()
        book_placeholders = ",".join(["?"] * len(chunk))
        type_placeholders = ",".join(["?"] * len(SUPPORTED_DOWNLOAD_TYPES))
        cur.execute(
            f"""
            SELECT b.gutenbergbookid, d.name, dt.name
            FROM books b
            JOIN downloadlinks d ON d.bookid = b.id
            JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
            WHERE b.gutenbergbookid IN ({book_placeholders})
              AND dt.name IN ({type_placeholders})
            """,
            [*chunk, *SUPPORTED_DOWNLOAD_TYPES],
        )
        rows = [(int(row[0]), row[1], row[2]) for row in cur.fetchall()]
        conn.close()
        return rows

    for chunk in _chunked(gutenberg_ids, max(1, chunk_size)):
        rows = _with_sqlite_retry(lambda chunk=chunk: query_chunk(chunk))
        for book_id, download_url, download_type in rows:
            candidates_by_book.setdefault(book_id, []).append((download_url, download_type))

    for candidates in candidates_by_book.values():
        candidates.sort(key=lambda item: DOWNLOAD_TYPE_PRIORITY.get(item[1], 999))
    return candidates_by_book


def _get_all_download_candidates_for_books(
    db_path: str,
    gutenberg_ids: list[int],
    *,
    chunk_size: int = 500,
) -> dict[int, list[tuple[str, str]]]:
    if not gutenberg_ids:
        return {}

    candidates_by_book: dict[int, list[tuple[str, str]]] = {book_id: [] for book_id in gutenberg_ids}

    def query_chunk(chunk: list[int]) -> list[tuple[int, str, str]]:
        conn = _connect_db(db_path)
        cur = conn.cursor()
        book_placeholders = ",".join(["?"] * len(chunk))
        cur.execute(
            f"""
            SELECT b.gutenbergbookid, d.name, dt.name
            FROM books b
            JOIN downloadlinks d ON d.bookid = b.id
            JOIN downloadlinkstype dt ON dt.id = d.downloadtypeid
            WHERE b.gutenbergbookid IN ({book_placeholders})
            """,
            chunk,
        )
        rows = [(int(row[0]), row[1], row[2]) for row in cur.fetchall()]
        conn.close()
        return rows

    for chunk in _chunked(gutenberg_ids, max(1, chunk_size)):
        rows = _with_sqlite_retry(lambda chunk=chunk: query_chunk(chunk))
        for book_id, download_url, download_type in rows:
            candidates_by_book.setdefault(book_id, []).append((download_url, download_type))

    return candidates_by_book


def _save_book_content(db_path: str, book_id: int, raw_text: str, clean_text: str) -> None:
    def write():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute(
            """
            INSERT OR REPLACE INTO book_contents (bookid, raw_content, clean_content, download_date)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (book_id, raw_text, clean_text),
        )
        conn.commit()
        conn.close()

    _with_sqlite_retry(write)


def _is_audio_or_video_type(download_type: str) -> bool:
    return download_type.startswith("audio/") or download_type.startswith("video/")


def _classify_preflight_book(
    db_path: str,
    book_id: int,
    title: str,
    local_supported: list[tuple[str, str]],
    local_all: list[tuple[str, str]],
) -> dict:
    if local_supported:
        return {
            "book_id": book_id,
            "title": title,
            "action": "skip",
            "reason": "local_supported_candidates_available",
            "candidates": local_supported,
        }

    live_candidates = _fetch_live_file_index_links(book_id)
    if not live_candidates:
        if local_all and any(_url_points_to_book_id(url, book_id) for url, _ in local_all):
            reason = "local_links_present_but_no_live_content_discovered"
        elif local_all:
            reason = "local_links_stale_and_no_live_content_discovered"
        else:
            reason = "no_live_content_discovered"
        return {
            "book_id": book_id,
            "title": title,
            "action": "skip",
            "reason": reason,
            "candidates": [],
        }

    has_audio_only = all(_is_audio_or_video_type(download_type) for _, download_type in live_candidates)
    local_all_mismatched = bool(local_all) and all(
        not _url_points_to_book_id(url, book_id) for url, _ in local_all
    )

    if has_audio_only:
        return {
            "book_id": book_id,
            "title": title,
            "action": "audio-only",
            "reason": "live_index_contains_audio_only",
            "candidates": [],
        }

    action = "repair" if local_all and local_all_mismatched else "refresh"
    reason = "stale_local_links_replaced_from_live_index" if action == "repair" else "live_index_backfill"
    return {
        "book_id": book_id,
        "title": title,
        "action": action,
        "reason": reason,
        "candidates": live_candidates,
    }


def _rotate_list(values: list[str], offset: int) -> list[str]:
    if not values:
        return values
    offset %= len(values)
    return values[offset:] + values[:offset]


def _download_missing_book(
    db_path: str,
    mirrors: list[str],
    target_book_id: int,
    source_book_id: int,
    title: str,
    candidates: list[tuple[str, str]],
    mirror_tries: int,
    seen_source_ids: set[int] | None = None,
) -> dict:
    if seen_source_ids is None:
        seen_source_ids = {source_book_id}
    if not candidates:
        return {
            "status": "no_candidate",
            "book_id": target_book_id,
            "source_book_id": source_book_id,
            "title": title,
        }

    print(
        f"fetching {source_book_id}: {title} "
        f"(target={target_book_id}, candidates={len(candidates)}, mirrors={len(mirrors)})",
        flush=True,
    )

    mirror_order = _rotate_list(mirrors, source_book_id)
    if mirror_tries > 0:
        mirror_order = mirror_order[:mirror_tries]

    last_error: Exception | None = None
    for url, download_type in candidates:
        for mirror in mirror_order:
            mirror_url = _mirror_url(url, mirror)
            try:
                raw_text = _download_book_text(mirror_url, download_type)
                if raw_text.strip():
                    if _looks_like_warning_text(raw_text):
                        # Folio warning pages are not content. We intentionally do not
                        # follow "see #..." redirects here because the project only has
                        # one known folio-only case (900), and the alternate ids do not
                        # reliably map to the same work.
                        if source_book_id in KNOWN_FOLIO_WARNING_BOOK_IDS:
                            print(
                                f"note {source_book_id}: folio warning treated as failure; redirect disabled",
                                flush=True,
                            )
                        last_error = ValueError(
                            f"Folio warning text; redirect disabled for {source_book_id}"
                        )
                        continue

                    if not _looks_like_real_book_text(raw_text):
                        last_error = ValueError(
                            f"Downloaded text from {source_book_id} looked like a stub, not book content"
                        )
                        continue

                    raw_bytes = raw_text.encode("utf-8")
                    try:
                        clean_text = strip_headers(raw_bytes).decode("utf-8", errors="ignore")
                    except Exception:
                        clean_text = raw_text
                    return {
                        "status": "success",
                        "book_id": target_book_id,
                        "source_book_id": source_book_id,
                        "title": title,
                        "source_type": download_type,
                        "source_url": mirror_url,
                        "raw_text": raw_text,
                        "clean_text": clean_text,
                    }
            except Exception as exc:
                last_error = exc

        try:
            raw_text = _download_book_text(url, download_type)
            if raw_text.strip():
                if _looks_like_warning_text(raw_text):
                    # Same folio handling as the mirror path above: do not recurse to
                    # alternate ids, because the warning text is not book content.
                    if source_book_id in KNOWN_FOLIO_WARNING_BOOK_IDS:
                        print(
                            f"note {source_book_id}: folio warning treated as failure; redirect disabled",
                            flush=True,
                        )
                    last_error = ValueError(
                        f"Folio warning text; redirect disabled for {source_book_id}"
                    )
                    continue

                if not _looks_like_real_book_text(raw_text):
                    last_error = ValueError(
                        f"Downloaded text from {source_book_id} looked like a stub, not book content"
                    )
                    continue

                raw_bytes = raw_text.encode("utf-8")
                try:
                    clean_text = strip_headers(raw_bytes).decode("utf-8", errors="ignore")
                except Exception:
                    clean_text = raw_text
                return {
                    "status": "success",
                    "book_id": target_book_id,
                    "source_book_id": source_book_id,
                    "title": title,
                    "source_type": download_type,
                    "source_url": url,
                    "raw_text": raw_text,
                    "clean_text": clean_text,
                }
        except Exception as exc:
            last_error = exc

    return {
        "status": "failed",
        "book_id": target_book_id,
        "source_book_id": source_book_id,
        "title": title,
        "error": last_error,
    }


def backfill(
    db_path: str,
    limit: int | None = None,
    dry_run: bool = False,
    preflight: bool = False,
    preflight_only: bool = False,
    workers: int = 8,
    mirror_tries: int = 3,
    chunk_size: int = 500,
) -> None:
    _ensure_book_contents_table(db_path)
    print("stage: scanning catalog for missing books", flush=True)
    missing_books = _get_missing_books(db_path)
    if limit is not None:
        missing_books = missing_books[:limit]

    print("stage: loading Project Gutenberg mirrors", flush=True)
    mirrors = _get_mirrors()
    print(f"summary: missing_books={len(missing_books)}", flush=True)
    print(f"summary: mirrors_loaded={len(mirrors)}", flush=True)
    book_ids = [book_id for book_id, _ in missing_books]
    local_supported_by_book = _get_download_candidates_for_books(
        db_path,
        book_ids,
        chunk_size=chunk_size,
    )

    planned_by_book: dict[int, dict] = {}
    if preflight or preflight_only:
        print("stage: preflight classifying missing books", flush=True)
        local_all_by_book = _get_all_download_candidates_for_books(
            db_path,
            book_ids,
            chunk_size=chunk_size,
        )
        counts = {"repair": 0, "refresh": 0, "audio-only": 0, "skip": 0}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    _classify_preflight_book,
                    db_path,
                    book_id,
                    title,
                    local_supported_by_book.get(book_id, []),
                    local_all_by_book.get(book_id, []),
                ): (book_id, title)
                for book_id, title in missing_books
            }

            for future in as_completed(futures):
                book_id, title = futures[future]
                try:
                    plan = future.result()
                except Exception as exc:
                    counts["skip"] += 1
                    planned_by_book[book_id] = {
                        "book_id": book_id,
                        "title": title,
                        "action": "skip",
                        "reason": f"preflight_failed: {exc}",
                        "candidates": local_supported_by_book.get(book_id, []),
                    }
                    print(f"preflight {book_id}: action=skip reason=preflight_failed ({title})", flush=True)
                    continue

                planned_by_book[book_id] = plan
                action = plan["action"]
                counts[action] += 1
                print(
                    f"preflight {book_id}: action={action} reason={plan['reason']} "
                    f"candidates={len(plan.get('candidates', []))} ({title})",
                    flush=True,
                )

        print(
            "preflight-summary: "
            f"repair={counts['repair']}, refresh={counts['refresh']}, "
            f"audio_only={counts['audio-only']}, skip={counts['skip']}",
            flush=True,
        )

        if preflight_only:
            return
    else:
        for book_id, title in missing_books:
            planned_by_book[book_id] = {
                "book_id": book_id,
                "title": title,
                "action": "skip",
                "reason": "preflight_disabled",
                "candidates": local_supported_by_book.get(book_id, []),
            }

    candidates_by_book: dict[int, list[tuple[str, str]]] = {}
    for book_id, title in missing_books:
        plan = planned_by_book.get(book_id, {})
        action = plan.get("action", "skip")
        if action in {"repair", "refresh"}:
            candidates_by_book[book_id] = plan.get("candidates", [])
        else:
            candidates_by_book[book_id] = local_supported_by_book.get(book_id, [])

    matched_books = sum(1 for candidates in candidates_by_book.values() if candidates)
    print(
        f"summary: download_links_resolved={matched_books}/{len(missing_books)}",
        flush=True,
    )
    print(f"stage: downloading with workers={max(1, workers)} mirror_tries={mirror_tries}", flush=True)

    processed = 0
    matched = 0
    written = 0
    previewed = 0
    skipped_no_candidate = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            # Use the preflight title when available so repair/refresh cases stay traceable.
            executor.submit(
                _download_missing_book,
                db_path,
                mirrors,
                book_id,
                book_id,
                planned_by_book.get(book_id, {}).get("title", title),
                candidates_by_book.get(book_id, []),
                mirror_tries,
            ): (book_id, title)
            for book_id, title in missing_books
        }

        for future in as_completed(futures):
            processed += 1
            book_id, title = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                failed += 1
                print(f"fail {book_id}: {exc} ({title})")
                continue

            status = result["status"]
            if status == "no_candidate":
                skipped_no_candidate += 1
                print(f"skip {book_id}: no supported download links ({title})")
                continue
            if status == "failed":
                failed += 1
                print(f"fail {book_id}: {result.get('error') or 'no parseable download'} ({title})")
                continue

            matched += 1
            source_type = result["source_type"]
            source_url = result.get("source_url", "")
            source_book_id = result.get("source_book_id", book_id)
            if dry_run:
                previewed += 1
                print(
                    f"dry-run {book_id} using {source_type} from {source_url} "
                    f"(source_book_id={source_book_id}): {title}",
                    flush=True,
                )
            else:
                _save_book_content(db_path, book_id, result["raw_text"], result["clean_text"])
                written += 1
                print(
                    f"saved {book_id} using {source_type} from {source_url} "
                    f"(source_book_id={source_book_id}): {title}",
                    flush=True,
                )

    print(
        "done: "
        f"processed={processed}, matched={matched}, written={written}, previewed={previewed}, "
        f"skipped_no_candidate={skipped_no_candidate}, failed={failed}"
    )


def main() -> None:
    args = parse_args()
    cafile, capath = _resolve_ca_paths(args.cafile, args.capath, args.verify)
    _install_https_opener(cafile, capath, args.verify)
    backfill(
        args.db_path,
        limit=args.limit,
        dry_run=args.dry_run,
        preflight=args.preflight,
        preflight_only=args.preflight_only,
        workers=args.workers,
        mirror_tries=args.mirror_tries,
        chunk_size=args.chunk_size,
    )


if __name__ == "__main__":
    try:
        main()
    except (ssl.SSLCertVerificationError, urllib.error.URLError) as exc:
        if isinstance(exc, ssl.SSLCertVerificationError) or (
            isinstance(exc, urllib.error.URLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc)
        ):
            raise RuntimeError(
                "TLS verification failed. Provide a trusted CA bundle path via "
                "GUTENBERG_CA_BUNDLE, SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE, "
                "or pass --ca-bundle/--ca-dir. Alternatively, use --no-verify."
            ) from exc
        raise
