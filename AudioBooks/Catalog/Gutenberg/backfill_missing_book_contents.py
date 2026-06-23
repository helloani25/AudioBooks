from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import hashlib
import json
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
import sys
import xml.etree.ElementTree as ET
import zipfile

try:
    from gutenbergpy.textget import strip_headers
except ImportError:  # pragma: no cover - optional dependency
    def strip_headers(payload: bytes) -> bytes:
        return payload

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.db_utils import (
    connect_db as _connect_db,
    ensure_book_content_backfill_tables as _ensure_book_content_backfill_tables,
    ensure_book_contents_table as _ensure_book_contents_table,
    with_sqlite_retry as _with_sqlite_retry,
)
from AudioBooks.Catalog.Gutenberg.content_validation import (
    detect_gutenberg_id_mismatch,
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
HTTP_TIMEOUT_SECONDS = 30


def parse_args() -> argparse.Namespace:
    """Parse CLI flags for queue seeding, discovery caching, and repair modes."""
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
        "--gutenberg-id",
        dest="gutenberg_ids",
        action="append",
        type=int,
        help="Target a specific Gutenberg id. Can be passed multiple times.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing book_contents rows for targeted ids.",
    )
    parser.add_argument(
        "--repair-all",
        action="store_true",
        help="Scan every catalog book with a Gutenberg id and rewrite book_contents from the live Gutenberg source.",
    )
    parser.add_argument(
        "--repair-mismatched-content",
        action="store_true",
        help="Rewrite books whose stored content advertises a different Gutenberg eBook id than books.gutenbergbookid.",
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
        "--reset-queue",
        action="store_true",
        help="Discard any saved queue state for the selected run before starting.",
    )
    parser.add_argument(
        "--refresh-discovery-cache",
        action="store_true",
        help="Ignore cached live-index and candidate discovery results and rebuild them.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent download workers.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="How many failed attempts to keep in the resumable queue before pausing a book.",
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
        help="How many books to keep in flight per batch when resolving candidates and downloading.",
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
    """Decode Gutenberg payload bytes using a small encoding fallback chain."""
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
    """Strip common TeX markup so archived `.tex` downloads become readable text."""
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
    """Extract text from a Gutenberg PDF fallback when the PDF parser is installed."""
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
    """Resolve a Gutenberg id to the catalog title used in logs and queue rows."""
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
    """Detect Gutenberg warning pages so the importer does not save redirect stubs."""
    lower_text = text.lower()
    warning_markers = (
        "do not download",
        "obsolete format",
        "alternative ids",
        "redirect disabled",
    )
    return any(marker in lower_text for marker in warning_markers)


def _looks_like_real_book_text(text: str) -> bool:
    """Reject tiny or stub-like downloads before they are written into the catalog."""
    stripped = text.strip()
    if len(stripped) < MIN_BOOK_TEXT_CHARS:
        return False
    if _looks_like_warning_text(stripped):
        return False
    words = re.findall(r"[A-Za-z0-9]{3,}", stripped)
    return len(words) >= MIN_BOOK_TEXT_WORDS


TOC_MARKER_RE = re.compile(r"^\s*(?:table of\s+)?contents\b", re.IGNORECASE)
CHAPTER_LISTING_RE = re.compile(r"^\s*(chapter|book|part|section)\s+([ivxlcdm]+|\d+)\b", re.IGNORECASE)
CONTENTS_MARKER_RE = re.compile(r"^\s*contents\.?\s*$", re.IGNORECASE)
TOC_PAGE_HEADER_RE = re.compile(r"^\s*page\.?\s*$", re.IGNORECASE)
TOC_ENTRY_RE = re.compile(
    r"^\s*.+?\s{2,}\d{1,4}\s*$",
    re.IGNORECASE,
)
ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
XML_DECL_RE = re.compile(r"<\?xml[^>]*\?>", re.IGNORECASE)
CDATA_WRAPPER_RE = re.compile(r"<!\[CDATA\[|\]\]>", re.IGNORECASE)
XML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->", re.IGNORECASE)
SCRIPT_STYLE_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", re.IGNORECASE)
GENERIC_TAG_RE = re.compile(r"</?[a-z][^>]{0,200}>", re.IGNORECASE)
CDATA_BLOCKOUT_LINE_RE = re.compile(
    r"^\s*/\*\s*(?:<!\[CDATA\[\s*)?xml blockout[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
XML_STYLE_COMMENT_LINE_RE = re.compile(
    r"^\s*/\*\s*xml\s+(?:start|end|blockout)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
GUTENBERG_TRAILER_START_RE = re.compile(
    r"^\s*section\s+1\.\s+general terms of use and redistributing project gutenberg",
    re.IGNORECASE,
)
GUTENBERG_END_MARKER_RE = re.compile(
    r"^\s*(\*\*\*\s*)?end of (?:(?:this|the)\s+)?project gutenberg",
    re.IGNORECASE,
)
GUTENBERG_UPDATED_EDITIONS_RE = re.compile(
    r"^\s*updated editions will replace the previous one",
    re.IGNORECASE,
)
GUTENBERG_FILE_NAMED_RE = re.compile(
    r"^\s*\*{3,}\s*this file should be named",
    re.IGNORECASE,
)
GUTENBERG_TRADEMARK_SECTION_RE = re.compile(
    r"^\s*project gutenberg(?:-tm)?\s+is\s+a\s+registered\s+trademark",
    re.IGNORECASE,
)
GUTENBERG_START_MARKER_RE = re.compile(
    r"^\s*\*{3,}\s*start of (?:(?:this|the)\s+)?project gutenberg",
    re.IGNORECASE,
)


def _roman_to_int(value: str) -> int | None:
    """Convert a roman numeral to int, returning None when malformed."""
    token = value.strip().lower()
    if not token or any(char not in ROMAN_VALUES for char in token):
        return None

    total = 0
    prev = 0
    for char in reversed(token):
        current = ROMAN_VALUES[char]
        if current < prev:
            total -= current
        else:
            total += current
            prev = current
    return total if total > 0 else None


def _heading_number(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _roman_to_int(token)


def _strip_prefixed_toc_listing(text: str) -> str:
    """
    Remove a top-of-book table-of-contents listing when chapter numbering restarts.

    This prevents TOC chapter lines from being interpreted as real chapter bodies,
    which can shift chapter indices in the reader (e.g., chapter 22 opening chapter 1).
    """
    lines = text.splitlines()
    if len(lines) < 80:
        return text

    max_scan = min(len(lines), 4000)
    contents_idx = None
    for idx, line in enumerate(lines[:max_scan]):
        if TOC_MARKER_RE.match(line):
            contents_idx = idx
            break

    if contents_idx is None:
        return text

    heading_rows: list[tuple[int, int]] = []
    scan_end = min(len(lines), contents_idx + 2200)
    for idx in range(contents_idx + 1, scan_end):
        match = CHAPTER_LISTING_RE.match(lines[idx].strip())
        if not match:
            continue
        number = _heading_number(match.group(2))
        if number is None:
            continue
        heading_rows.append((idx, number))

    if len(heading_rows) < 8:
        return text

    first_number = heading_rows[0][1]
    for restart_pos in range(1, len(heading_rows)):
        restart_line, restart_number = heading_rows[restart_pos]
        if restart_number != first_number:
            continue

        toc_rows = heading_rows[:restart_pos]
        if len(toc_rows) < 8:
            continue

        gaps = [toc_rows[i + 1][0] - toc_rows[i][0] for i in range(len(toc_rows) - 1)]
        compact_ratio = (sum(gap <= 40 for gap in gaps) / len(gaps)) if gaps else 1.0
        if compact_ratio < 0.7:
            continue

        follow_numbers = [num for _, num in heading_rows[restart_pos : restart_pos + 4]]
        if len(follow_numbers) >= 2 and follow_numbers[1] != first_number + 1:
            continue

        cleaned_lines = lines[:contents_idx] + lines[restart_line:]
        cleaned_text = "\n".join(cleaned_lines).strip()
        return cleaned_text if cleaned_text else text

    return text


def _strip_contents_page_listing(text: str) -> str:
    """
    Remove front-matter contents pages that list topic titles with page numbers.

    Example format:
      CONTENTS.
        Page
        Obesity .... 1
        Dwarfs .... 9
    """
    lines = text.splitlines()
    if len(lines) < 20:
        return text

    max_marker_scan = min(len(lines), 3000)
    marker_idx = None
    for idx in range(max_marker_scan):
        if CONTENTS_MARKER_RE.match(lines[idx].strip()):
            marker_idx = idx
            break
    if marker_idx is None:
        return text

    max_scan = min(len(lines), marker_idx + 2600)
    row_count = 0
    end_idx = None

    for idx in range(marker_idx + 1, max_scan):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        if TOC_PAGE_HEADER_RE.match(stripped):
            continue
        if TOC_ENTRY_RE.match(stripped):
            row_count += 1
            continue

        # Some long TOC entries wrap to a second line where only the wrapped
        # fragment carries the trailing page number. If the next non-empty line
        # is still a TOC row, keep scanning instead of ending the strip region.
        next_nonblank = ""
        for look_ahead in range(idx + 1, min(idx + 4, max_scan)):
            candidate = lines[look_ahead].strip()
            if candidate:
                next_nonblank = candidate
                break
        if next_nonblank and TOC_ENTRY_RE.match(next_nonblank):
            continue

        # Stop when the listing ends and prose/content resumes.
        end_idx = idx
        break

    if row_count < 8 or end_idx is None:
        return text

    cleaned_lines = lines[:marker_idx] + lines[end_idx:]
    cleaned = "\n".join(cleaned_lines).strip()
    return cleaned if cleaned else text


def _strip_gutenberg_trailer(text: str) -> str:
    """Drop trailing Project Gutenberg license/footer sections when present."""
    lines = text.splitlines()
    trailer_start = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if (
            GUTENBERG_TRAILER_START_RE.match(stripped)
            or GUTENBERG_END_MARKER_RE.match(stripped)
            or GUTENBERG_UPDATED_EDITIONS_RE.match(stripped)
            or GUTENBERG_FILE_NAMED_RE.match(stripped)
            or GUTENBERG_TRADEMARK_SECTION_RE.match(stripped)
        ):
            trailer_start = idx
            break
    if trailer_start is None:
        return text
    trimmed = "\n".join(lines[:trailer_start]).strip()
    return trimmed if trimmed else text


def _strip_gutenberg_preamble(text: str) -> str:
    """Drop leading Project Gutenberg header/license block when a START marker exists."""
    lines = text.splitlines()
    max_scan = min(len(lines), 800)
    for idx in range(max_scan):
        if GUTENBERG_START_MARKER_RE.match(lines[idx].strip()):
            # Only strip when the marker appears near the front; this avoids
            # cutting valid text if a quoted marker appears deep in the book.
            if idx > 500:
                return text
            trimmed = "\n".join(lines[idx + 1 :]).lstrip()
            return trimmed if trimmed else text
    return text


def _looks_like_html_markup(text: str) -> bool:
    """Detect payloads that are plain-text labels but still contain HTML/XML markup."""
    lower = text.lower()
    if "<html" in lower or "<body" in lower or "<head" in lower or "<!doctype html" in lower:
        return True
    if "<![cdata[" in lower or "<?xml" in lower:
        return True
    tag_hits = len(re.findall(r"</?[a-z][^>]{0,120}>", lower))
    return tag_hits >= 10


def _strip_inline_markup_artifacts(text: str) -> str:
    """Remove XML/HTML wrappers and styling blocks that leak into text payloads."""
    if not text:
        return text

    clean_text = text
    clean_text = XML_DECL_RE.sub(" ", clean_text)
    clean_text = CDATA_WRAPPER_RE.sub(" ", clean_text)
    clean_text = CDATA_BLOCKOUT_LINE_RE.sub(" ", clean_text)
    clean_text = XML_STYLE_COMMENT_LINE_RE.sub(" ", clean_text)
    clean_text = SCRIPT_STYLE_BLOCK_RE.sub(" ", clean_text)
    clean_text = XML_COMMENT_RE.sub(" ", clean_text)
    clean_text = _strip_unclosed_css_comment_block(clean_text)
    clean_text = GENERIC_TAG_RE.sub(" ", clean_text)

    # Remove any leftover blockout marker lines after comment/tag stripping.
    clean_text = re.sub(r"^\s*xml blockout\s*$", " ", clean_text, flags=re.IGNORECASE | re.MULTILINE)
    clean_text = re.sub(r"[ \t]+\n", "\n", clean_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
    return clean_text.strip()


def _strip_unclosed_css_comment_block(text: str) -> str:
    """Drop leading CSS blocks introduced by a dangling '<!--' marker."""
    marker = text.find("<!--")
    if marker < 0 or marker > 2500:
        return text

    prefix = text[:marker]
    suffix = text[marker + 4 :]
    lines = suffix.splitlines()
    if not lines:
        return text

    css_lines = 0
    content_start = None
    max_scan = min(len(lines), 1200)
    for idx in range(max_scan):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        is_css = (
            "{" in stripped
            or "}" in stripped
            or ";" in stripped
            or stripped.startswith((".", "#", "@"))
            or re.match(r"^[a-z][a-z0-9_-]*\s*:\s*[^:]+;?$", stripped, re.IGNORECASE) is not None
            or re.match(r"^[a-z0-9_.#,\s-]+\{$", stripped, re.IGNORECASE) is not None
        )
        if is_css:
            css_lines += 1
            continue
        if css_lines >= 5:
            content_start = idx
            break
        # If we hit non-CSS too early, this is probably not a style preamble.
        return text

    if content_start is None:
        return text

    remainder = "\n".join(lines[content_start:]).lstrip()
    merged = (prefix.rstrip() + "\n\n" + remainder).strip()
    return merged if merged else text


def _normalize_clean_text(raw_text: str) -> str:
    """Apply header stripping plus targeted TOC cleanup to imported text."""
    preclean_raw_text = _strip_inline_markup_artifacts(raw_text)
    raw_bytes = preclean_raw_text.encode("utf-8")
    try:
        clean_text = strip_headers(raw_bytes).decode("utf-8", errors="ignore")
    except Exception:
        clean_text = preclean_raw_text
    clean_text = _strip_inline_markup_artifacts(clean_text)
    clean_text = _strip_gutenberg_preamble(clean_text)
    clean_text = _strip_contents_page_listing(clean_text)
    clean_text = _strip_prefixed_toc_listing(clean_text)
    clean_text = _strip_gutenberg_trailer(clean_text)
    return clean_text


def _chunked(values: list[int], size: int) -> list[list[int]]:
    """Split a list into fixed-size batches for SQL and cache lookups."""
    return [values[index : index + size] for index in range(0, len(values), size)]


def _queue_key_for_run(
    *,
    repair_all: bool,
    repair_mismatched_content: bool,
    gutenberg_ids: list[int] | None,
) -> str:
    """Build the stable queue namespace for the current backfill mode."""
    # The queue key is the namespace for resumable state:
    # - one namespace for repair-all runs
    # - one namespace for payload-id mismatch repair runs
    # - one namespace for the default missing-content run
    # - one namespace per explicit targeted id set
    # Seeding uses this key so reruns update the same rows instead of creating a new queue.
    if repair_all:
        return "repair-all:v2"
    if repair_mismatched_content:
        return "repair-mismatched:v1"
    if gutenberg_ids:
        normalized = ",".join(str(gid) for gid in sorted(set(gutenberg_ids)))
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"targeted:v2:{digest}"
    return "missing:v2"


def _cache_get_json(db_path: str, cache_key: str):
    """Read a cached JSON payload from the discovery cache table."""
    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute("SELECT payload FROM gutenberg_discovery_cache WHERE cache_key = ?", (cache_key,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return json.loads(row[0])

    return _with_sqlite_retry(query)


def _cache_set_json(db_path: str, cache_key: str, payload) -> None:
    """Store a JSON payload in the discovery cache table for reuse on reruns."""
    def write():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO gutenberg_discovery_cache (cache_key, payload, download_date, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (cache_key, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

    _with_sqlite_retry(write)


def _queue_seed_books(
    db_path: str,
    queue_key: str,
    target_books: list[tuple[int, int | None, str]],
    *,
    reset: bool = False,
) -> None:
    """Seed or refresh the resumable queue for the selected run namespace."""
    # Each row is keyed by (queue_key, bookid), so the same namespace can be
    # reseeded safely. Completed rows remain in place, and only the selected
    # namespace is updated or reset.
    def write():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        if reset:
            cur.execute("DELETE FROM book_content_backfill_queue WHERE queue_key = ?", (queue_key,))
        for priority, (book_id, gutenberg_id, title) in enumerate(target_books):
            cur.execute(
                """
                INSERT INTO book_content_backfill_queue (
                    queue_key,
                    bookid,
                    gutenbergbookid,
                    title,
                    priority,
                    status,
                    attempts,
                    last_error,
                    source_bookid,
                    source_url,
                    source_type,
                    download_date,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(queue_key, bookid) DO UPDATE SET
                    gutenbergbookid = excluded.gutenbergbookid,
                    title = excluded.title,
                    priority = excluded.priority,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (queue_key, book_id, gutenberg_id, title, priority),
            )
        conn.commit()
        conn.close()

    _with_sqlite_retry(write)


def _load_queue_books(
    db_path: str,
    queue_key: str,
    *,
    max_attempts: int,
) -> list[tuple[int, int | None, str, int, str, int]]:
    """Load only unfinished queue rows that are still eligible for processing."""
    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT bookid, gutenbergbookid, title, priority, status, attempts
            FROM book_content_backfill_queue
            WHERE queue_key = ?
              AND status IN ('pending', 'failed')
              AND attempts < ?
            ORDER BY priority, bookid
            """,
            (queue_key, max_attempts),
        )
        rows = [
            (int(row[0]), int(row[1]) if row[1] is not None else None, row[2], int(row[3]), row[4], int(row[5]))
            for row in cur.fetchall()
        ]
        conn.close()
        return rows

    return _with_sqlite_retry(query)


def _update_queue_status(
    db_path: str,
    queue_key: str,
    book_id: int,
    *,
    status: str,
    attempt_delta: int = 0,
    last_error: str | None = None,
    source_bookid: int | None = None,
    source_url: str | None = None,
    source_type: str | None = None,
) -> None:
    """Persist the latest queue status so interrupted runs can resume cleanly."""
    def write():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE book_content_backfill_queue
            SET
                status = ?,
                attempts = attempts + ?,
                last_error = ?,
                source_bookid = ?,
                source_url = ?,
                source_type = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE queue_key = ? AND bookid = ?
            """,
            (status, attempt_delta, last_error, source_bookid, source_url, source_type, queue_key, book_id),
        )
        conn.commit()
        conn.close()

    _with_sqlite_retry(write)


def _get_download_candidates_cached(db_path: str, gutenberg_id: int, *, refresh: bool = False) -> list[tuple[str, str]]:
    """Fetch or cache the supported local download candidates for one Gutenberg id."""
    cache_key = f"download-candidates:v2:{gutenberg_id}:supported"
    if not refresh:
        cached = _cache_get_json(db_path, cache_key)
        if cached is not None:
            return [tuple(item) for item in cached]
    candidates = _get_download_candidates(db_path, gutenberg_id)
    _cache_set_json(db_path, cache_key, candidates)
    return candidates


def _get_all_download_candidates_cached(db_path: str, gutenberg_id: int, *, refresh: bool = False) -> list[tuple[str, str]]:
    """Fetch or cache every local download candidate for one Gutenberg id."""
    cache_key = f"download-candidates:v2:{gutenberg_id}:all"
    if not refresh:
        cached = _cache_get_json(db_path, cache_key)
        if cached is not None:
            return [tuple(item) for item in cached]
    candidates = _get_all_download_candidates(db_path, gutenberg_id)
    _cache_set_json(db_path, cache_key, candidates)
    return candidates


def _fetch_live_file_index_links_cached(
    db_path: str,
    gutenberg_id: int,
    *,
    refresh: bool = False,
) -> list[tuple[str, str]]:
    """Fetch or cache the live Gutenberg file index for one Gutenberg id."""
    cache_key = f"live-index:v2:{gutenberg_id}"
    if not refresh:
        cached = _cache_get_json(db_path, cache_key)
        if cached is not None:
            return [tuple(item) for item in cached]
    live_links = _fetch_live_file_index_links(gutenberg_id)
    _cache_set_json(db_path, cache_key, live_links)
    return live_links


class _HrefCollector(HTMLParser):
    """Collect anchor hrefs from a Gutenberg directory listing page."""
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        """Record anchor links so the importer can derive live download URLs."""
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
    ((".tex",), "application/prs.tex"),
    ((".epub", ".epub.noimages", ".epub.images"), "application/epub+zip"),
    ((".zip",), "application/octet-stream"),
    ((".pdf",), "application/pdf"),
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
    """Map a Gutenberg file suffix to the downloader's content type."""
    lower_name = link_name.lower()
    for suffixes, download_type in LIVE_DOWNLOAD_TYPE_BY_SUFFIX:
        if lower_name.endswith(suffixes):
            return download_type
    return None


def _is_live_content_url(url: str) -> bool:
    """Filter directory entries down to real content files and ignore RDF or warnings."""
    path_name = Path(urlsplit(url).path).name.lower()
    if not path_name or path_name.endswith("/"):
        return False
    if path_name.startswith("readme_warning") or path_name.endswith(".nfo") or path_name.endswith(".rdf"):
        return False
    download_type = _infer_live_download_type(path_name)
    return download_type is not None and download_type != "application/rdf+xml"


def _url_points_to_book_id(url: str, book_id: int) -> bool:
    """Check whether a candidate URL appears to belong to the requested book id."""
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
    """Scrape the live Gutenberg file index for content-bearing download links."""
    index_url = f"{GUTENBERG_FILES_URL}/{book_id}/"
    response = urllib.request.urlopen(index_url, timeout=HTTP_TIMEOUT_SECONDS)
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
    """Fallback HTML text extractor used when lxml is unavailable."""
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        """Skip script/style blocks in fallback mode."""
        if tag and tag.lower() in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        """Resume text capture after script/style blocks."""
        if tag and tag.lower() in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        """Accumulate visible HTML text while discarding markup."""
        if data and self._skip_depth == 0:
            self.parts.append(data)

    def get_text(self) -> str:
        """Return the concatenated text captured from the HTML document."""
        return " ".join(part.strip() for part in self.parts if part.strip())


def _html_bytes_to_text(html_bytes: bytes) -> str:
    """Convert Gutenberg HTML downloads into plain text."""
    html_text = _decode_text_bytes(html_bytes)
    try:
        if lxml_html is not None:
            root = lxml_html.fromstring(html_text)
            for node in root.xpath("//script|//style|//head"):
                node.drop_tree()
            body_nodes = root.xpath("//body")
            target = body_nodes[0] if body_nodes else root
            return target.text_content()
        parser = _TextExtractor()
        parser.feed(html_text)
        return parser.get_text()
    except Exception:
        try:
            parser = _TextExtractor()
            parser.feed(html_text)
            return parser.get_text()
        except Exception:
            return html_text


def _xml_bytes_to_text(xml_bytes: bytes) -> str:
    """Convert Gutenberg XML or TEI payloads into plain text."""
    try:
        root = ET.fromstring(xml_bytes)
        return " ".join(part.strip() for part in root.itertext() if part and part.strip())
    except Exception:
        return _decode_text_bytes(xml_bytes)


def _archive_bytes_to_text(archive_bytes: bytes) -> str:
    """Extract and concatenate readable text from EPUB or ZIP archives."""
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
    """Download a Gutenberg asset and normalize it to plain text."""
    response = urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS)
    try:
        payload = response.read()
    finally:
        response.close()

    if download_type.startswith("text/plain"):
        decoded_text = _decode_text_bytes(payload)
        if _looks_like_html_markup(decoded_text):
            return _html_bytes_to_text(payload)
        return decoded_text
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
    """Fetch and cache the current list of Project Gutenberg mirrors."""
    response = urllib.request.urlopen(MIRRORS_URL, timeout=HTTP_TIMEOUT_SECONDS)
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
    """Rewrite a Gutenberg URL to point at a specific mirror host."""
    source = urlsplit(url)
    target = urlsplit(mirror)
    return urlunsplit((target.scheme, target.netloc, source.path, source.query, source.fragment))


def _resolve_ca_paths(cli_cafile, cli_capath, verify=True):
    """Resolve TLS certificate inputs before installing the HTTPS opener."""
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
    """Install a shared HTTPS opener with the requested certificate policy."""
    if not verify:
        context = ssl._create_unverified_context()
    else:
        context = ssl.create_default_context()
        if cafile or capath:
            context.load_verify_locations(cafile=cafile, capath=capath)

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    urllib.request.install_opener(opener)
    ssl._create_default_https_context = lambda: context


def _get_missing_books(db_path: str) -> list[tuple[int, int | None, str]]:
    """Return catalog books that still lack internal book_contents rows."""
    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                b.id,
                b.gutenbergbookid,
                COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
            FROM books b
            LEFT JOIN book_contents bc ON bc.bookid = b.id
            WHERE b.gutenbergbookid IS NOT NULL
              AND bc.bookid IS NULL
            ORDER BY b.numdownloads DESC
            """
        )
        rows = [(int(row[0]), int(row[1]) if row[1] is not None else None, row[2]) for row in cur.fetchall()]
        conn.close()
        return rows

    return _with_sqlite_retry(query)


def _get_all_books(db_path: str) -> list[tuple[int, int | None, str]]:
    """Return every catalog book that has a Gutenberg id for repair-all runs."""
    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                b.id,
                b.gutenbergbookid,
                COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
            FROM books b
            WHERE b.gutenbergbookid IS NOT NULL
            ORDER BY b.numdownloads DESC
            """
        )
        rows = [(int(row[0]), int(row[1]) if row[1] is not None else None, row[2]) for row in cur.fetchall()]
        conn.close()
        return rows

    return _with_sqlite_retry(query)


def _get_mismatched_content_books(db_path: str) -> list[tuple[int, int | None, str]]:
    """Return books whose stored payload marker conflicts with books.gutenbergbookid."""

    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                b.id,
                b.gutenbergbookid,
                COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title,
                substr(bc.raw_content, 1, 30000)
            FROM books b
            JOIN book_contents bc ON bc.bookid = b.id
            WHERE b.gutenbergbookid IS NOT NULL
            ORDER BY b.numdownloads DESC
            """
        )
        rows = cur.fetchall()
        conn.close()

        scanned = 0
        marker_found = 0
        mismatched: list[tuple[int, int | None, str]] = []
        for row in rows:
            scanned += 1
            book_id = int(row[0])
            gutenberg_id = int(row[1]) if row[1] is not None else None
            title = row[2]
            snippet = row[3] or ""
            mismatch, detected = detect_gutenberg_id_mismatch(snippet, gutenberg_id)
            if detected is not None:
                marker_found += 1
            if mismatch:
                mismatched.append((book_id, gutenberg_id, title))

        print(
            "summary: "
            f"mismatch_scan_total={scanned}, marker_found={marker_found}, mismatched={len(mismatched)}",
            flush=True,
        )
        return mismatched

    return _with_sqlite_retry(query)


def _get_target_books(db_path: str, gutenberg_ids: list[int]) -> list[tuple[int, int | None, str]]:
    """Return the explicit subset of books requested by Gutenberg id."""
    if not gutenberg_ids:
        return []

    def query():
        conn = _connect_db(db_path)
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(gutenberg_ids))
        cur.execute(
            f"""
            SELECT
                b.id,
                b.gutenbergbookid,
                COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title
            FROM books b
            WHERE b.gutenbergbookid IN ({placeholders})
            ORDER BY b.id
            """,
            gutenberg_ids,
        )
        rows = [(int(row[0]), int(row[1]) if row[1] is not None else None, row[2]) for row in cur.fetchall()]
        conn.close()
        return rows

    return _with_sqlite_retry(query)


def _get_download_candidates(db_path: str, gutenberg_id: int) -> list[tuple[str, str]]:
    """Return locally cached supported download links for one Gutenberg id."""
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
    """Return every cached download link for one Gutenberg id, including unsupported ones."""
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
    """Batch-fetch supported download candidates for many Gutenberg ids at once."""
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
    """Batch-fetch all cached download candidates for many Gutenberg ids at once."""
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


def _save_book_content(
    db_path: str,
    book_id: int,
    raw_text: str,
    clean_text: str,
    *,
    expected_gutenberg_id: int | None = None,
) -> None:
    """Write the canonical text payload for a book into book_contents."""
    def write():
        mismatch, detected_id = detect_gutenberg_id_mismatch(raw_text, expected_gutenberg_id)
        if mismatch:
            raise ValueError(
                f"payload Gutenberg ID mismatch for book_id={book_id}: "
                f"expected={expected_gutenberg_id}, detected={detected_id}"
            )
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
    """Return True when a candidate is not text and should not be imported here."""
    return download_type.startswith("audio/") or download_type.startswith("video/")


def _classify_preflight_book(
    db_path: str,
    book_id: int,
    gutenberg_id: int | None,
    title: str,
    *,
    refresh_cache: bool = False,
) -> dict:
    """Decide whether a book should be repaired, refreshed, skipped, or treated as audio-only."""
    local_supported = _get_download_candidates_cached(db_path, gutenberg_id, refresh=refresh_cache) if gutenberg_id is not None else []
    if local_supported:
        return {
            "book_id": book_id,
            "title": title,
            "action": "skip",
            "reason": "local_supported_candidates_available",
            "candidates": local_supported,
        }

    local_all = _get_all_download_candidates_cached(db_path, gutenberg_id, refresh=refresh_cache) if gutenberg_id is not None else []
    live_candidates = _fetch_live_file_index_links_cached(db_path, gutenberg_id, refresh=refresh_cache) if gutenberg_id is not None else []
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


def _process_backfill_queue_item(
    db_path: str,
    queue_key: str,
    book_id: int,
    gutenberg_id: int | None,
    title: str,
    *,
    mirrors: list[str],
    repair_all: bool,
    preflight: bool,
    refresh_cache: bool,
    dry_run: bool,
    mirror_tries: int,
) -> dict:
    """Process one queued book and update the resumable queue with the result."""
    local_supported = _get_download_candidates_cached(db_path, gutenberg_id, refresh=refresh_cache) if gutenberg_id is not None else []
    if repair_all:
        local_all = _get_all_download_candidates_cached(db_path, gutenberg_id, refresh=refresh_cache) if gutenberg_id is not None else []
        live_candidates = _fetch_live_file_index_links_cached(db_path, gutenberg_id, refresh=refresh_cache) if gutenberg_id is not None else []
        if live_candidates:
            has_audio_only = all(_is_audio_or_video_type(download_type) for _, download_type in live_candidates)
            if has_audio_only:
                plan = {
                    "book_id": book_id,
                    "title": title,
                    "action": "audio-only",
                    "reason": "live_index_contains_audio_only",
                    "candidates": [],
                }
            else:
                action = "repair" if local_all and all(
                    not _url_points_to_book_id(url, book_id) for url, _ in local_all
                ) else "refresh"
                plan = {
                    "book_id": book_id,
                    "title": title,
                    "action": action,
                    "reason": "stale_local_links_replaced_from_live_index" if action == "repair" else "live_index_backfill",
                    "candidates": live_candidates,
                }
        elif local_supported:
            plan = {
                "book_id": book_id,
                "title": title,
                "action": "download",
                "reason": "local_supported_candidates_available",
                "candidates": local_supported,
            }
        else:
            plan = {
                "book_id": book_id,
                "title": title,
                "action": "skip",
                "reason": "no_live_content_discovered",
                "candidates": [],
            }
    elif preflight:
        plan = _classify_preflight_book(
            db_path,
            book_id,
            gutenberg_id,
            title,
            refresh_cache=refresh_cache,
        )
    else:
        plan = {
            "book_id": book_id,
            "title": title,
            "action": "download" if local_supported else "skip",
            "reason": "local_supported_candidates_available" if local_supported else "preflight_disabled",
            "candidates": local_supported,
        }

    action = plan.get("action", "skip")
    candidates = plan.get("candidates", [])

    if action in {"skip", "audio-only"} or not candidates:
        if not dry_run:
            _update_queue_status(
                db_path,
                queue_key,
                book_id,
                status="skipped",
                last_error=plan.get("reason") or "no supported download links",
            )
        return {
            "status": "no_candidate" if not candidates else action,
            "book_id": book_id,
            "title": title,
            "reason": plan.get("reason") or "no supported download links",
        }

    source_book_id = gutenberg_id or book_id
    result = _download_missing_book(
        db_path,
        mirrors,
        book_id,
        source_book_id,
        title,
        candidates,
        mirror_tries,
        expected_gutenberg_id=gutenberg_id,
    )

    if result["status"] == "success":
        if not dry_run:
            try:
                _save_book_content(
                    db_path,
                    book_id,
                    result["raw_text"],
                    result["clean_text"],
                    expected_gutenberg_id=gutenberg_id,
                )
            except Exception as exc:
                _update_queue_status(
                    db_path,
                    queue_key,
                    book_id,
                    status="failed",
                    attempt_delta=1,
                    last_error=str(exc),
                )
                return {
                    "status": "failed",
                    "book_id": book_id,
                    "source_book_id": result.get("source_book_id", source_book_id),
                    "title": title,
                    "error": exc,
                }
            _update_queue_status(
                db_path,
                queue_key,
                book_id,
                status="done",
                source_bookid=result.get("source_book_id", source_book_id),
                source_url=result.get("source_url"),
                source_type=result.get("source_type"),
            )
        return result

    if result["status"] == "no_candidate":
        if not dry_run:
            _update_queue_status(
                db_path,
                queue_key,
                book_id,
                status="skipped",
                last_error="no supported download links",
            )
        return result

    if not dry_run:
        _update_queue_status(
            db_path,
            queue_key,
            book_id,
            status="failed",
            attempt_delta=1,
            last_error=str(result.get("error") or "no parseable download"),
        )
    return result


def _rotate_list(values: list[str], offset: int) -> list[str]:
    """Rotate a list so mirror selection spreads load across hosts."""
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
    *,
    expected_gutenberg_id: int | None = None,
    seen_source_ids: set[int] | None = None,
) -> dict:
    """Download, validate, and normalize one Gutenberg text candidate."""
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
                    mismatch, detected_id = detect_gutenberg_id_mismatch(raw_text, expected_gutenberg_id)
                    if mismatch:
                        last_error = ValueError(
                            f"payload Gutenberg ID mismatch for book_id={target_book_id}: "
                            f"expected={expected_gutenberg_id}, detected={detected_id}"
                        )
                        continue

                if not _looks_like_real_book_text(raw_text):
                    last_error = ValueError(
                        f"Downloaded text from {source_book_id} looked like a stub, not book content"
                    )
                    continue

                clean_text = _normalize_clean_text(raw_text)
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
                mismatch, detected_id = detect_gutenberg_id_mismatch(raw_text, expected_gutenberg_id)
                if mismatch:
                    last_error = ValueError(
                        f"payload Gutenberg ID mismatch for book_id={target_book_id}: "
                        f"expected={expected_gutenberg_id}, detected={detected_id}"
                    )
                    continue

                if not _looks_like_real_book_text(raw_text):
                    last_error = ValueError(
                        f"Downloaded text from {source_book_id} looked like a stub, not book content"
                    )
                    continue

                clean_text = _normalize_clean_text(raw_text)
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
    gutenberg_ids: list[int] | None = None,
    force: bool = False,
    repair_all: bool = False,
    repair_mismatched_content: bool = False,
    preflight: bool = False,
    preflight_only: bool = False,
    reset_queue: bool = False,
    refresh_discovery_cache: bool = False,
    workers: int = 8,
    max_attempts: int = 3,
    mirror_tries: int = 3,
    chunk_size: int = 500,
) -> None:
    # Flow:
    # 1. Seed a durable queue row for each target book under a stable namespace.
    # 2. Reload only unfinished queue rows so reruns resume instead of restarting.
    # 3. Use cached discovery data first, then live Gutenberg fetches when needed.
    # 4. Download, validate, and write book contents by internal books.id.
    # 5. Persist queue status so done/skipped/failed work survives the next run.
    #
    # Mode mapping:
    # - default run: missing-content queue, local candidates first
    # - preflight: classify repair/refresh/skip/audio-only without committing content
    # - repair-all: full sweep with live discovery; choose repair vs refresh per book
    # - targeted ids: one hashed namespace for the exact Gutenberg id set
    """Seed the queue, reuse cached discovery, and backfill book_contents."""
    _ensure_book_contents_table(db_path)
    _ensure_book_content_backfill_tables(db_path)
    preflight = preflight or repair_all
    print("stage: scanning catalog for books", flush=True)
    if repair_all:
        target_books = _get_all_books(db_path)
    elif repair_mismatched_content:
        target_books = _get_mismatched_content_books(db_path)
    elif gutenberg_ids:
        target_books = _get_target_books(db_path, gutenberg_ids)
    else:
        target_books = _get_missing_books(db_path)
    if limit is not None:
        target_books = target_books[:limit]

    mirrors: list[str] = []
    if mirror_tries > 0:
        print("stage: loading Project Gutenberg mirrors", flush=True)
        mirrors = _get_mirrors()

    queue_key = _queue_key_for_run(
        repair_all=repair_all,
        repair_mismatched_content=repair_mismatched_content,
        gutenberg_ids=gutenberg_ids,
    )
    # Seed rows under the namespace selected above, then reload the unfinished
    # rows from that same namespace so interrupted runs can resume.
    _queue_seed_books(db_path, queue_key, target_books, reset=reset_queue)

    queue_rows = _load_queue_books(db_path, queue_key, max_attempts=max_attempts)
    print(f"summary: target_books={len(target_books)} queue_key={queue_key}", flush=True)
    print(f"summary: queued_books={len(queue_rows)}", flush=True)
    print(f"summary: mirrors_loaded={len(mirrors)}", flush=True)
    if preflight:
        print("stage: cached discovery is enabled for live index and per-book candidates", flush=True)
    if preflight_only:
        print("stage: preflight-only requested; no downloads will run", flush=True)
        return

    print(
        f"stage: downloading with workers={max(1, workers)} mirror_tries={mirror_tries} chunk_size={max(1, chunk_size)}",
        flush=True,
    )

    processed = 0
    matched = 0
    written = 0
    previewed = 0
    skipped_no_candidate = 0
    failed = 0

    batch_size = max(1, chunk_size)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for queue_batch in _chunked(queue_rows, batch_size):
            futures = {
                executor.submit(
                    _process_backfill_queue_item,
                    db_path,
                    queue_key,
                    book_id,
                    gutenberg_id,
                    title,
                    mirrors=mirrors,
                    repair_all=repair_all,
                    preflight=preflight,
                    refresh_cache=refresh_discovery_cache,
                    dry_run=dry_run,
                    mirror_tries=mirror_tries,
                ): (book_id, title)
                for book_id, gutenberg_id, title, _priority, _status, _attempts in queue_batch
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
                if status in {"no_candidate", "skip", "audio-only"}:
                    skipped_no_candidate += 1
                    print(f"skip {book_id}: {result.get('reason') or 'no supported download links'} ({title})")
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
    """Entry point that wires CLI flags into the resumable backfill flow."""
    args = parse_args()
    cafile, capath = _resolve_ca_paths(args.cafile, args.capath, args.verify)
    _install_https_opener(cafile, capath, args.verify)
    backfill(
        args.db_path,
        limit=args.limit,
        dry_run=args.dry_run,
        gutenberg_ids=args.gutenberg_ids,
        force=args.force,
        repair_all=args.repair_all,
        repair_mismatched_content=args.repair_mismatched_content,
        preflight=args.preflight,
        preflight_only=args.preflight_only,
        reset_queue=args.reset_queue,
        refresh_discovery_cache=args.refresh_discovery_cache,
        workers=args.workers,
        max_attempts=args.max_attempts,
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
