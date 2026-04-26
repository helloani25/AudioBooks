from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.db_utils import connect_db as _connect_db


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
RDF_CACHE_DIR = BASE_DIR.parent / "DB" / "cache" / "epub"

URL_GID_RE = re.compile(r"/(?:ebooks|files)/(\d+)")
TITLE_MARKER_RE = re.compile(r"\s*\$[a-z]\s*", re.IGNORECASE)

RDF_NS = {
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
}


@dataclass(frozen=True)
class RdfMetadata:
    gutenberg_id: int
    titles: tuple[str, ...]
    authors: tuple[str, ...]
    issued: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair catalog metadata using the Gutenberg id embedded in download links.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to the SQLite catalog database.")
    parser.add_argument(
        "--book-id",
        action="append",
        type=int,
        dest="book_ids",
        help="Repair only the given internal book id(s). Can be passed multiple times.",
    )
    parser.add_argument(
        "--gutenberg-id",
        action="append",
        type=int,
        dest="gutenberg_ids",
        help="Repair only books whose download links point at the given Gutenberg id(s). Can be passed multiple times.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after repairing this many books.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=250,
        help="Commit after this many repaired books.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print one line per repaired book instead of periodic progress only.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview repairs without writing to SQLite.")
    return parser.parse_args()


def _normalize_title(value: str | None) -> str:
    if not value:
        return ""
    value = TITLE_MARKER_RE.sub(" ", value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _extract_gutenberg_id(url: str | None) -> int | None:
    if not url:
        return None
    match = URL_GID_RE.search(url)
    if not match:
        return None
    return int(match.group(1))


def _load_author_cache(conn: sqlite3.Connection) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM authors")
    cache: dict[str, int] = {}
    for author_id, name in cur.fetchall():
        if name and name not in cache:
            cache[name] = int(author_id)
    return cache


def _load_rdf_metadata(gutenberg_id: int) -> RdfMetadata | None:
    rdf_path = RDF_CACHE_DIR / str(gutenberg_id) / f"pg{gutenberg_id}.rdf"
    if not rdf_path.exists():
        return None

    try:
        root = ET.parse(rdf_path).getroot()
    except ET.ParseError:
        return None

    titles: list[str] = []
    for elem in root.findall(".//dcterms:title", RDF_NS):
        title = _normalize_title(elem.text)
        if title and title not in titles:
            titles.append(title)
    for elem in root.findall(".//dcterms:alternative", RDF_NS):
        title = _normalize_title(elem.text)
        if title and title not in titles:
            titles.append(title)

    authors: list[str] = []
    for creator in root.findall(".//dcterms:creator", RDF_NS):
        for path in (
            ".//pgterms:name",
            ".//pgterms:alias",
        ):
            for elem in creator.findall(path, RDF_NS):
                name = (elem.text or "").strip()
                if name and name not in authors:
                    authors.append(name)

    issued = None
    issued_elem = root.find(".//dcterms:issued", RDF_NS)
    if issued_elem is not None and issued_elem.text:
        issued = issued_elem.text.strip()

    if not titles:
        return None

    return RdfMetadata(
        gutenberg_id=gutenberg_id,
        titles=tuple(titles),
        authors=tuple(authors),
        issued=issued,
    )


def _iter_candidate_books(
    conn: sqlite3.Connection,
    *,
    book_ids: set[int] | None = None,
    gutenberg_ids: set[int] | None = None,
):
    params: list[object] = []
    book_clause = ""
    gutenberg_clause = ""
    if book_ids:
        placeholders = ",".join(["?"] * len(book_ids))
        book_clause = f"AND b.id IN ({placeholders})"
        params.extend(sorted(book_ids))
    if gutenberg_ids:
        placeholders = ",".join(["?"] * len(gutenberg_ids))
        gutenberg_clause = (
            "AND (\n"
            f"    b.gutenbergbookid IN ({placeholders})\n"
            "    OR EXISTS (\n"
            "        SELECT 1\n"
            "        FROM downloadlinks d2\n"
            "        WHERE d2.bookid = b.id\n"
            "          AND d2.name LIKE '%/ebooks/%'\n"
            f"          AND CAST(substr(d2.name, instr(d2.name, '/ebooks/') + 8, instr(substr(d2.name, instr(d2.name, '/ebooks/') + 8), '.') - 1) AS INTEGER) IN ({placeholders})\n"
            "    )\n"
            ")"
        )
        params.extend(sorted(gutenberg_ids))
        params.extend(sorted(gutenberg_ids))

    query = f"""
        SELECT
            b.id,
            b.gutenbergbookid,
            COALESCE((
                SELECT MIN(d.name)
                FROM downloadlinks d
                WHERE d.bookid = b.id
                  AND d.name LIKE '%/ebooks/%'
                  AND d.name NOT LIKE '%.rdf'
            ), (
                SELECT MIN(d.name)
                FROM downloadlinks d
                WHERE d.bookid = b.id
                  AND d.name LIKE '%/files/%'
            ), (
                SELECT MIN(d.name)
                FROM downloadlinks d
                WHERE d.bookid = b.id
            )) AS sample_link,
            COALESCE((SELECT MIN(t.name) FROM titles t WHERE t.bookid = b.id), '') AS current_title,
            COALESCE((
                SELECT GROUP_CONCAT(name, '|')
                FROM (
                    SELECT DISTINCT a.name AS name
                    FROM authors a
                    JOIN book_authors ba ON ba.authorid = a.id
                    WHERE ba.bookid = b.id
                    ORDER BY a.name
                )
            ), '') AS current_authors,
            b.dateissued
        FROM books b
        WHERE EXISTS (SELECT 1 FROM downloadlinks d WHERE d.bookid = b.id AND (d.name LIKE '%/ebooks/%' OR d.name LIKE '%/files/%'))
          {book_clause}
          {gutenberg_clause}
        ORDER BY b.id
    """

    cur = conn.cursor()
    cur.execute(query, params)
    for book_id, current_gutenberg_id, sample_link, current_title, current_authors_blob, current_dateissued in cur:
        derived_gutenberg_id = _extract_gutenberg_id(sample_link)
        if derived_gutenberg_id is None:
            continue
        current_authors = tuple(a for a in (current_authors_blob or "").split("|") if a)
        yield (
            int(book_id),
            int(current_gutenberg_id) if current_gutenberg_id is not None else None,
            derived_gutenberg_id,
            (current_title or ""),
            current_authors,
            current_dateissued,
        )


def _ensure_author_id(conn: sqlite3.Connection, author_cache: dict[str, int], name: str) -> int:
    existing = author_cache.get(name)
    if existing is not None:
        return existing

    cur = conn.cursor()
    cur.execute("INSERT INTO authors (name) VALUES (?)", (name,))
    author_id = int(cur.lastrowid)
    author_cache[name] = author_id
    return author_id


def _apply_book_repair(
    conn: sqlite3.Connection,
    author_cache: dict[str, int],
    book_id: int,
    gutenberg_id: int,
    rdf_metadata: RdfMetadata,
) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE books SET gutenbergbookid = ?, dateissued = ? WHERE id = ?",
        (gutenberg_id, rdf_metadata.issued, book_id),
    )

    cur.execute("DELETE FROM titles WHERE bookid = ?", (book_id,))
    for title in rdf_metadata.titles:
        cur.execute("INSERT INTO titles (name, bookid) VALUES (?, ?)", (title, book_id))

    cur.execute("DELETE FROM book_authors WHERE bookid = ?", (book_id,))
    for author_name in rdf_metadata.authors:
        author_id = _ensure_author_id(conn, author_cache, author_name)
        cur.execute("INSERT INTO book_authors (bookid, authorid) VALUES (?, ?)", (book_id, author_id))


def repair_catalog(
    db_path: str,
    *,
    book_ids: set[int] | None = None,
    gutenberg_ids: set[int] | None = None,
    limit: int | None = None,
    batch_size: int = 250,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    conn = _connect_db(db_path)
    try:
        author_cache = _load_author_cache(conn)
        print("repairing catalog from download links", flush=True)
        repaired = 0
        skipped_missing_cache = 0
        scanned = 0
        for book_id, current_gutenberg_id, derived_gutenberg_id, current_title, current_authors, current_dateissued in _iter_candidate_books(
            conn,
            book_ids=book_ids,
            gutenberg_ids=gutenberg_ids,
        ):
            if limit is not None and repaired >= limit:
                break

            scanned += 1
            rdf_metadata = _load_rdf_metadata(derived_gutenberg_id)
            if rdf_metadata is None:
                skipped_missing_cache += 1
                continue

            if (
                current_gutenberg_id == derived_gutenberg_id
                and current_title == (rdf_metadata.titles[0] if rdf_metadata.titles else "")
                and current_authors == rdf_metadata.authors
                and (current_dateissued or None) == rdf_metadata.issued
            ):
                continue

            if verbose:
                print(
                    f"book {book_id}: gid {derived_gutenberg_id} | "
                    f"title={rdf_metadata.titles[0]!r} | "
                    f"authors={', '.join(rdf_metadata.authors[:4]) if rdf_metadata.authors else 'Unknown'} | "
                    f"issued={rdf_metadata.issued or 'unknown'}",
                    flush=True,
                )
            if dry_run:
                repaired += 1
                if repaired % max(batch_size, 1) == 0:
                    print(f"progress: repaired={repaired} scanned={scanned}", flush=True)
                continue

            _apply_book_repair(conn, author_cache, book_id, derived_gutenberg_id, rdf_metadata)
            repaired += 1
            if repaired % max(batch_size, 1) == 0:
                conn.commit()
                print(f"committed {repaired} repaired books (scanned={scanned})", flush=True)

        conn.commit()
        print(
            f"done: repaired={repaired} scanned={scanned} skipped_missing_cache={skipped_missing_cache}",
            flush=True,
        )
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    book_ids = set(args.book_ids) if args.book_ids else None
    gutenberg_ids = set(args.gutenberg_ids) if args.gutenberg_ids else None
    repair_catalog(
        args.db_path,
        book_ids=book_ids,
        gutenberg_ids=gutenberg_ids,
        limit=args.limit,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
