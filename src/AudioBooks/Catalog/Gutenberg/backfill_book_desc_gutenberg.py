from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.db_utils import connect_db as _connect_db


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
RDF_CACHE_DIR = BASE_DIR.parent / "DB" / "cache" / "epub"
GUTENBERG_RDF_URL = "https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.rdf"
GUTENBERG_EBOOK_URL = "https://www.gutenberg.org/ebooks/{book_id}"
HTTP_TIMEOUT = 30

RDF_NS = {
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}
WIKIPEDIA_URL_RE = re.compile(r"https?://[a-z\-]+\.wikipedia\.org/wiki/[^\s<>\"]+", re.IGNORECASE)


@dataclass
class BookRow:
    book_id: int
    gutenberg_id: int | None
    title: str
    authors: str
    publication_date: str | None
    desc_exists: bool
    existing_summary: str | None
    existing_wikipedia_id: int | None
    existing_freebase_id: str | None
    existing_genres_text: str | None
    existing_genres_json: str | None
    existing_source_title: str | None
    existing_source_author: str | None
    existing_source: str | None


@dataclass
class CandidateMetadata:
    source_gutenberg_id: int
    summary: str | None
    summary_source: str | None
    wikipedia_urls: list[str]


class SummaryTextContainerParser(HTMLParser):
    """Extract text from the Gutenberg summary-text-container HTML node."""

    def __init__(self) -> None:
        super().__init__()
        self._capture_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        class_attr = attrs_dict.get("class", "")
        classes = {piece.strip() for piece in class_attr.split() if piece.strip()}
        if "summary-text-container" in classes:
            self._capture_depth = 1
            return
        if self._capture_depth > 0:
            self._capture_depth += 1

    def handle_endtag(self, _tag):
        if self._capture_depth > 0:
            self._capture_depth -= 1

    def handle_data(self, data):
        if self._capture_depth > 0 and data:
            self._parts.append(data)

    def get_text(self) -> str:
        text = html.unescape(" ".join(part.strip() for part in self._parts if part.strip()))
        return re.sub(r"\s+", " ", text).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill missing book_desc summaries and Wikipedia IDs from Gutenberg RDF metadata, "
            "with ebook-page summary fallback."
        ),
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to the SQLite catalog database.")
    parser.add_argument(
        "--book-id",
        action="append",
        type=int,
        dest="book_ids",
        help="Limit processing to these internal book IDs (can be passed multiple times).",
    )
    parser.add_argument(
        "--gutenberg-id",
        action="append",
        type=int,
        dest="gutenberg_ids",
        help="Limit processing to books with these gutenbergbookid values.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after processing this many target books.",
    )
    parser.add_argument(
        "--force-summary",
        action="store_true",
        help="Overwrite existing non-empty summaries using Gutenberg-derived summaries.",
    )
    parser.add_argument(
        "--force-wikipedia-id",
        action="store_true",
        help="Overwrite existing wikipedia_id values when a resolved one is available.",
    )
    parser.add_argument(
        "--refresh-live",
        action="store_true",
        help="Fetch live RDF/page content if local cache is missing.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=200,
        help="Commit writes every N updated rows.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview updates without writing.")
    parser.add_argument("--verbose", action="store_true", help="Print per-book update decisions.")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress after this many processed books.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel worker threads for I/O-bound processing (RDF reads, Wikipedia API, HTML fetches).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=64,
        help="Number of books submitted to the thread pool per batch.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Stop retrying a book after this many failed attempts.",
    )
    parser.add_argument(
        "--reset-queue",
        action="store_true",
        help="Clear the saved queue rows for the current namespace before starting.",
    )
    return parser.parse_args()


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(value.split())


def _ensure_book_desc_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS book_desc (
            bookid INTEGER PRIMARY KEY,
            wikipedia_id INTEGER,
            freebase_id TEXT,
            source_title TEXT NOT NULL,
            source_author TEXT,
            publication_date TEXT,
            genres_text TEXT,
            genres_json TEXT,
            summary TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'cmu-book-summaries',
            download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_book_desc_wikipedia_id ON book_desc(wikipedia_id)")
    conn.commit()


def _load_target_books(
    conn: sqlite3.Connection,
    *,
    book_ids: set[int] | None,
    gutenberg_ids: set[int] | None,
    only_incomplete: bool = False,
) -> list[BookRow]:
    clauses = ["b.gutenbergbookid IS NOT NULL"]
    params: list[object] = []
    if book_ids:
        placeholders = ",".join(["?"] * len(book_ids))
        clauses.append(f"b.id IN ({placeholders})")
        params.extend(sorted(book_ids))
    if gutenberg_ids:
        placeholders = ",".join(["?"] * len(gutenberg_ids))
        clauses.append(f"b.gutenbergbookid IN ({placeholders})")
        params.extend(sorted(gutenberg_ids))
    if only_incomplete:
        clauses.append(
            "(bd.bookid IS NULL OR trim(COALESCE(bd.summary, '')) = '' OR bd.wikipedia_id IS NULL)"
        )

    where = " AND ".join(clauses)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            b.id,
            b.gutenbergbookid,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title,
            COALESCE((
                SELECT GROUP_CONCAT(name, '|')
                FROM (
                    SELECT DISTINCT a.name AS name
                    FROM authors a
                    JOIN book_authors ba ON ba.authorid = a.id
                    WHERE ba.bookid = b.id
                    ORDER BY a.name
                )
            ), '') AS authors,
            b.dateissued,
            bd.bookid IS NOT NULL,
            bd.summary,
            bd.wikipedia_id,
            bd.freebase_id,
            bd.genres_text,
            bd.genres_json,
            bd.source_title,
            bd.source_author,
            bd.source
        FROM books b
        LEFT JOIN book_desc bd ON bd.bookid = b.id
        WHERE {where}
        ORDER BY b.numdownloads DESC, b.id
        """,
        params,
    )

    rows: list[BookRow] = []
    for row in cur:
        rows.append(
            BookRow(
                book_id=int(row[0]),
                gutenberg_id=int(row[1]) if row[1] is not None else None,
                title=row[2] or "Untitled",
                authors=(row[3] or "").replace("|", ", "),
                publication_date=row[4],
                desc_exists=bool(row[5]),
                existing_summary=row[6],
                existing_wikipedia_id=int(row[7]) if row[7] is not None else None,
                existing_freebase_id=row[8],
                existing_genres_text=row[9],
                existing_genres_json=row[10],
                existing_source_title=row[11],
                existing_source_author=row[12],
                existing_source=row[13],
            )
        )
    return rows


def _load_work_key_to_gids(conn: sqlite3.Connection) -> dict[tuple[str, str], list[int]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            b.gutenbergbookid,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title,
            COALESCE((
                SELECT GROUP_CONCAT(name, '|')
                FROM (
                    SELECT DISTINCT a.name AS name
                    FROM authors a
                    JOIN book_authors ba ON ba.authorid = a.id
                    WHERE ba.bookid = b.id
                    ORDER BY a.name
                )
            ), '') AS authors
        FROM books b
        WHERE b.gutenbergbookid IS NOT NULL
        """
    )
    mapping: dict[tuple[str, str], list[int]] = {}
    for gutenberg_id, title, authors_blob in cur:
        if gutenberg_id is None:
            continue
        key = (_normalize_text(title), _normalize_text((authors_blob or "").replace("|", ", ")))
        mapping.setdefault(key, []).append(int(gutenberg_id))
    for key in mapping:
        mapping[key] = sorted(set(mapping[key]))
    return mapping


def _load_rdf_root(gutenberg_id: int, *, refresh_live: bool) -> ET.Element | None:
    local_path = RDF_CACHE_DIR / str(gutenberg_id) / f"pg{gutenberg_id}.rdf"
    payload: bytes | None = None
    if local_path.exists():
        payload = local_path.read_bytes()

    if payload is None and refresh_live:
        url = GUTENBERG_RDF_URL.format(book_id=gutenberg_id)
        try:
            response = urllib.request.urlopen(url, timeout=HTTP_TIMEOUT)
            try:
                payload = response.read()
            finally:
                response.close()
        except Exception:
            payload = None

    if not payload:
        return None
    try:
        return ET.fromstring(payload)
    except ET.ParseError:
        return None


def _extract_wikipedia_urls_from_rdf(root: ET.Element) -> list[str]:
    urls: list[str] = []

    # Book-specific links are usually embedded in dcterms:description text.
    for desc_elem in root.findall(".//dcterms:description", RDF_NS):
        text = (desc_elem.text or "").strip()
        if not text:
            continue
        for url in WIKIPEDIA_URL_RE.findall(text):
            if url not in urls:
                urls.append(url)

    # Generic webpage links (author/translator/book pages) as fallback.
    for webpage in root.findall(".//pgterms:webpage", RDF_NS):
        href = webpage.attrib.get(f"{{{RDF_NS['rdf']}}}resource")
        if not href:
            continue
        if "wikipedia.org/wiki/" not in href.lower():
            continue
        if href not in urls:
            urls.append(href)
    return urls


def _extract_rdf_summary(root: ET.Element) -> str | None:
    for elem in root.findall(".//pgterms:marc520", RDF_NS):
        text = html.unescape((elem.text or "").strip())
        if text:
            return re.sub(r"\s+", " ", text).strip()
    return None


def _extract_page_summary(gutenberg_id: int) -> str | None:
    url = GUTENBERG_EBOOK_URL.format(book_id=gutenberg_id)
    response = urllib.request.urlopen(url, timeout=HTTP_TIMEOUT)
    try:
        html_text = response.read().decode("utf-8", errors="ignore")
    finally:
        response.close()
    parser = SummaryTextContainerParser()
    parser.feed(html_text)
    summary = parser.get_text()
    return summary or None


def _resolve_wikipedia_id(url: str | None) -> int | None:
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    if not parsed.netloc.endswith("wikipedia.org"):
        return None

    query = urllib.parse.parse_qs(parsed.query)
    if "curid" in query and query["curid"]:
        curid = query["curid"][0].strip()
        if curid.isdigit():
            return int(curid)

    title = urllib.parse.unquote(parsed.path.split("/wiki/", 1)[-1]).strip()
    if not title:
        return None

    api_url = urllib.parse.urlunsplit(
        (
            parsed.scheme or "https",
            parsed.netloc,
            "/w/api.php",
            urllib.parse.urlencode(
                {
                    "action": "query",
                    "format": "json",
                    "redirects": "1",
                    "titles": title,
                }
            ),
            "",
        )
    )
    try:
        response = urllib.request.urlopen(api_url, timeout=HTTP_TIMEOUT)
        try:
            payload = response.read().decode("utf-8", errors="ignore")
        finally:
            response.close()
        body = json.loads(payload)
        pages = (((body or {}).get("query") or {}).get("pages") or {})
        for page in pages.values():
            pageid = page.get("pageid")
            if isinstance(pageid, int) and pageid > 0:
                return pageid
    except Exception:
        return None
    return None


def _pick_best_wikipedia_url(urls: list[str]) -> str | None:
    if not urls:
        return None
    # Prefer English wikipedia links, then any wikipedia link.
    urls_sorted = sorted(urls, key=lambda u: (0 if "://en.wikipedia.org/" in u.lower() else 1, len(u)))
    return urls_sorted[0]


def _load_sibling_summary(conn: sqlite3.Connection, book_id: int, work_key: tuple[str, str]) -> tuple[str | None, str | None]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT summary, source
        FROM book_desc
        WHERE bookid != ?
          AND summary IS NOT NULL
          AND trim(summary) != ''
          AND lower(source_title) = ?
          AND lower(COALESCE(source_author, '')) = ?
        ORDER BY length(summary) DESC
        LIMIT 1
        """,
        (book_id, work_key[0], work_key[1]),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def _ensure_queue_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS book_desc_backfill_queue (
            bookid    INTEGER NOT NULL,
            namespace TEXT    NOT NULL,
            status    TEXT    NOT NULL DEFAULT 'pending',
            attempts  INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            summary_source TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (bookid, namespace)
        )
        """
    )
    conn.commit()


def _derive_namespace(
    book_ids: set[int] | None,
    gutenberg_ids: set[int] | None,
    force_summary: bool,
    force_wikipedia_id: bool,
) -> str:
    if book_ids or gutenberg_ids:
        all_ids = sorted((book_ids or set()) | (gutenberg_ids or set()))
        h = hashlib.sha1(",".join(str(i) for i in all_ids).encode()).hexdigest()[:8]
        return f"targeted:v1:{h}"
    if force_summary or force_wikipedia_id:
        return "force:v1"
    return "missing:v1"


def _seed_queue(conn: sqlite3.Connection, books: list[BookRow], namespace: str) -> None:
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO book_desc_backfill_queue (bookid, namespace, status, attempts)
        VALUES (?, ?, 'pending', 0)
        ON CONFLICT (bookid, namespace) DO UPDATE SET
            status = CASE WHEN status = 'done' OR status = 'skipped' THEN status ELSE 'pending' END,
            updated_at = CURRENT_TIMESTAMP
        """,
        [(book.book_id, namespace) for book in books],
    )
    conn.commit()


def _reset_queue(conn: sqlite3.Connection, namespace: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM book_desc_backfill_queue WHERE namespace = ?",
        (namespace,),
    )
    conn.commit()


def _load_pending_ids(conn: sqlite3.Connection, namespace: str, max_attempts: int) -> set[int]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT bookid FROM book_desc_backfill_queue
        WHERE namespace = ? AND status IN ('pending', 'failed') AND attempts < ?
        ORDER BY bookid
        """,
        (namespace, max_attempts),
    )
    return {int(row[0]) for row in cur}


def _mark_queue_done(conn: sqlite3.Connection, bookid: int, namespace: str, summary_source: str | None) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE book_desc_backfill_queue
        SET status = 'done', summary_source = ?, updated_at = CURRENT_TIMESTAMP
        WHERE bookid = ? AND namespace = ?
        """,
        (summary_source, bookid, namespace),
    )


def _mark_queue_skipped(conn: sqlite3.Connection, bookid: int, namespace: str) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE book_desc_backfill_queue
        SET status = 'skipped', updated_at = CURRENT_TIMESTAMP
        WHERE bookid = ? AND namespace = ?
        """,
        (bookid, namespace),
    )


def _mark_queue_failed(conn: sqlite3.Connection, bookid: int, namespace: str, error: str) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE book_desc_backfill_queue
        SET status = 'failed', attempts = attempts + 1, last_error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE bookid = ? AND namespace = ?
        """,
        (error[:500], bookid, namespace),
    )


def _update_or_insert_book_desc(
    conn: sqlite3.Connection,
    *,
    book: BookRow,
    summary: str | None,
    summary_source: str | None,
    wikipedia_id: int | None,
    dry_run: bool,
) -> bool:
    summary_to_write = summary if summary is not None else (book.existing_summary or "").strip()
    if not summary_to_write:
        return False

    source_title = (book.existing_source_title or book.title or "Untitled").strip() or "Untitled"
    source_author = (book.existing_source_author or book.authors or None)
    publication_date = book.publication_date or None
    genres_text = book.existing_genres_text
    genres_json = book.existing_genres_json
    freebase_id = book.existing_freebase_id
    source = summary_source or book.existing_source or "gutenberg-rdf"

    if dry_run:
        return True

    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO book_desc (
            bookid,
            wikipedia_id,
            freebase_id,
            source_title,
            source_author,
            publication_date,
            genres_text,
            genres_json,
            summary,
            source,
            download_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            book.book_id,
            wikipedia_id,
            freebase_id,
            source_title,
            source_author,
            publication_date,
            genres_text,
            genres_json,
            summary_to_write,
            source,
        ),
    )
    return True


def _process_book_io(
    db_path: str,
    book: BookRow,
    work_key_to_gids: dict[tuple[str, str], list[int]],
    needs_summary: bool,
    needs_wikipedia_id: bool,
    refresh_live: bool,
) -> dict:
    """Runs in a worker thread. Opens its own read-only SQLite connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        work_key = (_normalize_text(book.title), _normalize_text(book.authors))
        candidate_gids: list[int] = []
        if book.gutenberg_id is not None:
            candidate_gids.append(book.gutenberg_id)
        candidate_gids.extend(work_key_to_gids.get(work_key, []))
        candidate_gids = list(dict.fromkeys(candidate_gids))

        existing_summary = (book.existing_summary or "").strip()
        chosen_summary: str | None = existing_summary if not needs_summary else None
        chosen_summary_source: str | None = book.existing_source
        wikipedia_urls: list[str] = []

        # 1) sibling existing summary
        if needs_summary and not chosen_summary:
            sibling_summary, sibling_source = _load_sibling_summary(conn, book.book_id, work_key)
            if sibling_summary:
                chosen_summary = sibling_summary.strip()
                chosen_summary_source = sibling_source or "book-desc-sibling"

        # 2) RDF summaries + wikipedia URLs
        for gid in candidate_gids:
            root = _load_rdf_root(gid, refresh_live=refresh_live)
            if root is None:
                continue
            for url in _extract_wikipedia_urls_from_rdf(root):
                if url not in wikipedia_urls:
                    wikipedia_urls.append(url)
            if needs_summary and not chosen_summary:
                rdf_summary = _extract_rdf_summary(root)
                if rdf_summary:
                    chosen_summary = rdf_summary
                    chosen_summary_source = "gutenberg-rdf"

        # 3) HTML summary-text-container fallback (live fetch only)
        if needs_summary and not chosen_summary and refresh_live:
            for gid in candidate_gids:
                try:
                    html_summary = _extract_page_summary(gid)
                except Exception:
                    html_summary = None
                if html_summary:
                    chosen_summary = html_summary
                    chosen_summary_source = "gutenberg-ebook-page"
                    break

        # 4) Wikipedia ID resolution
        wikipedia_url = _pick_best_wikipedia_url(wikipedia_urls)
        resolved_wikipedia_id = book.existing_wikipedia_id
        if needs_wikipedia_id and wikipedia_url:
            resolved_wikipedia_id = _resolve_wikipedia_id(wikipedia_url) or book.existing_wikipedia_id

        return {
            "book": book,
            "chosen_summary": chosen_summary,
            "chosen_summary_source": chosen_summary_source,
            "resolved_wikipedia_id": resolved_wikipedia_id,
            "wikipedia_url": wikipedia_url,
            "needs_summary": needs_summary,
            "needs_wikipedia_id": needs_wikipedia_id,
            "error": None,
        }
    except Exception as exc:
        return {"book": book, "error": str(exc)}
    finally:
        conn.close()


def backfill(
    db_path: str,
    *,
    book_ids: set[int] | None,
    gutenberg_ids: set[int] | None,
    limit: int | None,
    force_summary: bool,
    force_wikipedia_id: bool,
    refresh_live: bool,
    commit_every: int,
    dry_run: bool,
    verbose: bool,
    progress_every: int,
    workers: int,
    chunk_size: int,
    max_attempts: int,
    reset_queue: bool,
) -> None:
    conn = _connect_db(db_path)
    _ensure_book_desc_table(conn)
    _ensure_queue_table(conn)

    print("stage: indexing catalog work keys", flush=True)
    work_key_to_gids = _load_work_key_to_gids(conn)
    print(f"summary: work_keys={len(work_key_to_gids)}", flush=True)

    print("stage: loading target books", flush=True)
    only_incomplete = not book_ids and not gutenberg_ids and not force_summary and not force_wikipedia_id
    targets = _load_target_books(
        conn,
        book_ids=book_ids,
        gutenberg_ids=gutenberg_ids,
        only_incomplete=only_incomplete,
    )
    if limit is not None:
        targets = targets[: max(0, limit)]
    print(f"summary: target_books={len(targets)}", flush=True)

    namespace = _derive_namespace(book_ids, gutenberg_ids, force_summary, force_wikipedia_id)
    print(f"summary: namespace={namespace}", flush=True)

    if reset_queue:
        _reset_queue(conn, namespace)
        print(f"stage: queue reset for namespace={namespace}", flush=True)

    _seed_queue(conn, targets, namespace)

    pending_ids = _load_pending_ids(conn, namespace, max_attempts)
    pending_books = [b for b in targets if b.book_id in pending_ids]
    already_done = len(targets) - len(pending_books)
    print(
        f"summary: pending={len(pending_books)} already_done={already_done} max_attempts={max_attempts}",
        flush=True,
    )

    if dry_run:
        print("stage: dry-run enabled; no writes will be committed", flush=True)

    processed = 0
    updated = 0
    skipped = 0
    failed = 0
    committed = 0
    total = len(pending_books)

    print(f"stage: processing with workers={workers} chunk_size={chunk_size}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for chunk_start in range(0, total, chunk_size):
            chunk = pending_books[chunk_start : chunk_start + chunk_size]

            futures = {
                executor.submit(
                    _process_book_io,
                    db_path,
                    book,
                    work_key_to_gids,
                    force_summary or not (book.existing_summary or "").strip(),
                    force_wikipedia_id or book.existing_wikipedia_id is None,
                    refresh_live,
                ): book
                for book in chunk
            }

            for future in as_completed(futures):
                result = future.result()
                book = result["book"]
                processed += 1

                if result.get("error"):
                    failed += 1
                    _mark_queue_failed(conn, book.book_id, namespace, result["error"])
                    if not dry_run:
                        conn.commit()
                    if verbose:
                        print(
                            f"  result=error book_id={book.book_id} gid={book.gutenberg_id} "
                            f"error={result['error']}",
                            flush=True,
                        )
                    if processed % max(1, progress_every) == 0:
                        print(
                            f"stage: progress processed={processed}/{total} updated={updated} "
                            f"skipped={skipped} failed={failed} pending_commit={committed}",
                            flush=True,
                        )
                    continue

                did_write = _update_or_insert_book_desc(
                    conn,
                    book=book,
                    summary=result["chosen_summary"] if result["needs_summary"] else None,
                    summary_source=result["chosen_summary_source"],
                    wikipedia_id=result["resolved_wikipedia_id"],
                    dry_run=dry_run,
                )

                if did_write:
                    updated += 1
                    committed += 1
                    _mark_queue_done(conn, book.book_id, namespace, result["chosen_summary_source"])
                    if verbose:
                        print(
                            f"  result=update book_id={book.book_id} gid={book.gutenberg_id} "
                            f"summary_source={result['chosen_summary_source'] or 'unchanged'} "
                            f"wiki_url={result.get('wikipedia_url') or '-'} "
                            f"wikipedia_id={result['resolved_wikipedia_id']}",
                            flush=True,
                        )
                    if not dry_run and committed >= max(1, commit_every):
                        print(f"stage: committing {committed} book_desc rows", flush=True)
                        conn.commit()
                        committed = 0
                else:
                    skipped += 1
                    _mark_queue_skipped(conn, book.book_id, namespace)
                    if verbose:
                        print(
                            f"  result=skip book_id={book.book_id} gid={book.gutenberg_id} "
                            f"(no summary candidate found)",
                            flush=True,
                        )

                if processed % max(1, progress_every) == 0:
                    print(
                        f"stage: progress processed={processed}/{total} updated={updated} "
                        f"skipped={skipped} failed={failed} pending_commit={committed}",
                        flush=True,
                    )

    if not dry_run and committed > 0:
        print(f"stage: committing final {committed} book_desc rows", flush=True)
        conn.commit()

    print(
        "done: "
        f"processed={processed}, updated={updated}, skipped={skipped}, "
        f"failed={failed}, already_done={already_done}, dry_run={dry_run}",
        flush=True,
    )
    conn.close()


def main() -> None:
    args = parse_args()
    backfill(
        args.db_path,
        book_ids=set(args.book_ids or []),
        gutenberg_ids=set(args.gutenberg_ids or []),
        limit=args.limit,
        force_summary=args.force_summary,
        force_wikipedia_id=args.force_wikipedia_id,
        refresh_live=args.refresh_live,
        commit_every=args.commit_every,
        dry_run=args.dry_run,
        verbose=args.verbose,
        progress_every=args.progress_every,
        workers=args.workers,
        chunk_size=args.chunk_size,
        max_attempts=args.max_attempts,
        reset_queue=args.reset_queue,
    )


if __name__ == "__main__":
    main()
