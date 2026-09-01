from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import tarfile
import unicodedata
import urllib.request
from collections import defaultdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.db_utils import connect_db as _connect_db


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
DEFAULT_SOURCE_URL = "http://www.cs.cmu.edu/~dbamman/data/booksummaries.tar.gz"
DEFAULT_SOURCE_TARBALL_PATH = BASE_DIR.parent / "DB" / "booksummaries.tar.gz"

TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "book",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "volume",
    "vol",
}

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill book_desc from the CMU Book Summary Dataset.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to the SQLite database.")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help="URL to booksummaries.tar.gz.")
    parser.add_argument(
        "--tarball-path",
        default=str(DEFAULT_SOURCE_TARBALL_PATH),
        help="Use a local booksummaries.tar.gz instead of downloading.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after processing this many summary rows.")
    parser.add_argument("--dry-run", action="store_true", help="Match and preview rows without writing.")
    parser.add_argument(
        "--unmatched-limit",
        type=int,
        default=20,
        help="How many unmatched rows to print in the summary.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=500,
        help="Commit after this many inserted rows. Use 1 for per-row commits.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=250,
        help="Print scan progress after this many summary rows.",
    )
    return parser.parse_args()


def _ensure_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("CREATE INDEX IF NOT EXISTS books_gutenbergbookid_idx ON books(gutenbergbookid)")
    cur.execute("CREATE INDEX IF NOT EXISTS titles_bookid_idx ON titles(bookid)")
    cur.execute("CREATE INDEX IF NOT EXISTS book_authors_bookid_idx ON book_authors(bookid)")
    cur.execute("CREATE INDEX IF NOT EXISTS authors_name_idx ON authors(name)")
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


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized.lower())
    return " ".join(normalized.split())


def _title_tokens(value: str | None) -> list[str]:
    return [
        token
        for token in _normalize_text(value).split()
        if len(token) > 2 and token not in TITLE_STOPWORDS
    ]


def _extract_year(date_str: str | None) -> int | None:
    if not date_str:
        return None
    match = re.match(r"^\s*(\d{4})", str(date_str))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1000 < year < 3000 else None


def _parse_genres(raw_genres: str | None) -> tuple[str, str]:
    if not raw_genres:
        return "", "[]"
    try:
        parsed = json.loads(raw_genres)
    except Exception:
        return raw_genres.strip(), json.dumps(raw_genres)

    if isinstance(parsed, dict):
        ordered_values = [parsed[key] for key in sorted(parsed, key=lambda k: parsed[k])]
    elif isinstance(parsed, list):
        ordered_values = [str(item) for item in parsed]
    else:
        ordered_values = [str(parsed)]

    genres_text = ", ".join(str(item) for item in ordered_values if str(item).strip())
    return genres_text, json.dumps(parsed, ensure_ascii=False)


def _download_tarball(source_url: str, destination_path: Path) -> Path:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"stage: downloading dataset from {source_url} to {destination_path}", flush=True)
    with urllib.request.urlopen(source_url) as response, destination_path.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    return destination_path


def _find_summary_member(tar: tarfile.TarFile) -> tarfile.TarInfo:
    members = [member for member in tar.getmembers() if member.isfile()]
    for member in members:
        if member.name.endswith("booksummaries.txt"):
            return member
    for member in members:
        if member.name.endswith(".txt"):
            return member
    raise FileNotFoundError("Could not find booksummaries.txt inside archive")


def _load_catalog_index(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            b.id,
            b.gutenbergbookid,
            b.dateissued,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title,
            COALESCE((
                SELECT GROUP_CONCAT(name, '|')
                FROM (
                    SELECT DISTINCT a.name AS name
                    FROM authors a
                    JOIN book_authors ba ON ba.authorid = a.id
                    WHERE ba.bookid = b.id
                )
            ), '') AS authors
        FROM books b
        WHERE b.gutenbergbookid IS NOT NULL
        """
    )

    title_index: dict[str, list[dict]] = defaultdict(list)
    author_index: dict[str, set[int]] = defaultdict(set)
    token_index: dict[str, set[int]] = defaultdict(set)
    all_books: dict[int, dict] = {}

    for book_id, gutenberg_id, dateissued, title, authors in cur:
        record = {
            "bookid": int(book_id),
            "gutenbergbookid": int(gutenberg_id) if gutenberg_id is not None else None,
            "title": title or "Untitled",
            "title_norm": _normalize_text(title),
            "authors": [],
            "year": _extract_year(dateissued),
        }
        record["title_tokens"] = tuple(_title_tokens(record["title"]))
        record["title_token_set"] = set(record["title_tokens"])
        if authors:
            record["authors"] = [author for author in authors.split("|") if author.strip()]
        for author in record["authors"]:
            author_index[_normalize_text(author)].add(record["bookid"])
        for token in record["title_token_set"]:
            token_index[token].add(record["bookid"])
        title_index[record["title_norm"]].append(record)
        all_books[record["bookid"]] = record

    return all_books, title_index, author_index, token_index


def _load_existing_book_desc_ids(conn: sqlite3.Connection) -> set[int]:
    cur = conn.cursor()
    cur.execute("SELECT bookid FROM book_desc")
    return {int(row[0]) for row in cur}


def _split_author_names(value: str | None) -> list[str]:
    if not value:
        return []

    raw = value.strip()
    if not raw:
        return []

    parts = [raw]
    for separator in (r"\s*&\s*", r"\s+and\s+", r"\s*;\s*"):
        next_parts: list[str] = []
        for part in parts:
            next_parts.extend(re.split(separator, part, flags=re.IGNORECASE))
        parts = next_parts

    comma_parts: list[str] = []
    for part in parts:
        if part.count(",") > 1:
            comma_parts.extend([piece.strip() for piece in part.split(",") if piece.strip()])
        else:
            comma_parts.append(part.strip())

    return [part for part in comma_parts if part]


def _select_catalog_book(
    row: list[str],
    title_index: dict[str, list[dict]],
    author_index: dict[str, set[int]],
    token_index: dict[str, set[int]],
    all_books: dict[int, dict],
):
    wikipedia_id = int(row[0]) if row[0].strip().isdigit() else None
    freebase_id = row[1].strip() or None
    title = row[2].strip()
    author = row[3].strip()
    publication_date = row[4].strip() or None
    year = _extract_year(publication_date)
    title_norm = _normalize_text(title)
    author_norm = _normalize_text(author)
    author_mismatch = False
    author_match_mode: str | None = None

    candidates = list(title_index.get(title_norm, []))
    title_candidates = list(candidates)

    exact_author_ids = author_index.get(author_norm, set()) if author_norm else set()
    split_author_ids: set[int] = set()
    for author_part in _split_author_names(author):
        part_norm = _normalize_text(author_part)
        if part_norm:
            split_author_ids.update(author_index.get(part_norm, set()))

    author_book_ids = exact_author_ids or split_author_ids
    if exact_author_ids:
        author_match_mode = "author_exact_match"
    elif split_author_ids:
        author_match_mode = "author_split_match"
    elif author_norm and author_norm not in {"unknown", "n/a", "na", "none"}:
        author_mismatch = True

    if author_book_ids:
        candidates = [candidate for candidate in candidates if candidate["bookid"] in author_book_ids]

    if year is not None:
        year_candidates = [candidate for candidate in candidates if candidate["year"] == year]
        if len(year_candidates) == 1:
            candidates = year_candidates
        elif len(year_candidates) > 1:
            candidates = year_candidates

    if len(candidates) == 1:
        return candidates[0], {
            "reason": author_match_mode or ("title_only_fallback" if author_norm and not author_book_ids else "title_exact_match"),
            "author_match_mode": author_match_mode,
            "used_year": year is not None,
        }

    if len(title_candidates) == 1:
        return title_candidates[0], {
            "reason": "title_only_fallback" if author_norm and not author_book_ids else "title_exact_match",
            "author_match_mode": author_match_mode,
            "used_year": year is not None,
        }

    query_tokens = _title_tokens(title)
    loose_candidates = []
    if query_tokens:
        query_token_set = set(query_tokens)
        candidate_ids: set[int] | None = None
        for token in query_token_set:
            token_candidates = token_index.get(token)
            if not token_candidates:
                candidate_ids = set()
                break
            candidate_ids = set(token_candidates) if candidate_ids is None else candidate_ids & token_candidates
            if not candidate_ids:
                break

        candidate_pool = list(candidate_ids)
        if year is not None:
            year_pool = [candidate_id for candidate_id in candidate_pool if all_books[candidate_id]["year"] == year]
            if year_pool:
                candidate_pool = year_pool

        for candidate_id in candidate_pool:
            candidate = all_books[candidate_id]
            candidate_tokens = candidate["title_token_set"]
            if not query_token_set.issubset(candidate_tokens):
                continue
            candidate_norm = candidate["title_norm"]
            query_norm = title_norm
            score = len(query_token_set)
            if candidate_norm == query_norm:
                score += 100
            elif query_norm and query_norm in candidate_norm:
                score += 75
            elif candidate_norm in query_norm:
                score += 50

            if year is not None and candidate["year"] == year:
                score += 20

            if query_norm.startswith("book of ") or query_norm.startswith("the book of "):
                score += 10
                if any(marker in candidate_norm for marker in ("bible", "expositor", "world english", "king james", "douay")):
                    score += 15

            loose_candidates.append((score, len(candidate_tokens), len(candidate_norm), candidate))

    if loose_candidates:
        loose_candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[3]["bookid"]))
        best_score, _, _, best_candidate = loose_candidates[0]
        if best_score >= 12:
            return best_candidate, {
                "reason": "title_only_fallback" if not author_book_ids else "title_loose_match",
                "author_match_mode": author_match_mode,
                "used_year": year is not None,
            }

    if author_mismatch:
        return None, {
            "reason": "author_mismatch",
            "wikipedia_id": wikipedia_id,
            "freebase_id": freebase_id,
            "title": title,
            "author": author,
        }

    return None, {
        "reason": "no_title_match",
        "wikipedia_id": wikipedia_id,
        "freebase_id": freebase_id,
        "title": title,
        "author": author,
        "candidate_bookids": [candidate["bookid"] for candidate in candidates[:10]],
    }


def _save_book_desc(
    conn: sqlite3.Connection,
    *,
    bookid: int,
    wikipedia_id: int | None,
    freebase_id: str | None,
    title: str,
    author: str | None,
    publication_date: str | None,
    genres_text: str,
    genres_json: str,
    summary: str,
) -> None:
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'cmu-book-summaries', CURRENT_TIMESTAMP)
        """,
        (
            bookid,
            wikipedia_id,
            freebase_id,
            title,
            author,
            publication_date,
            genres_text,
            genres_json,
            summary,
        ),
    )


def backfill(
    db_path: str,
    source_url: str,
    tarball_path: str | None,
    limit: int | None,
    dry_run: bool,
    unmatched_limit: int,
    commit_every: int,
    progress_every: int,
) -> None:
    conn = _connect_db(db_path)
    _ensure_tables(conn)

    print("stage: indexing catalog books", flush=True)
    all_books, title_index, author_index, token_index = _load_catalog_index(conn)
    print(f"summary: indexed_titles={len(title_index)}", flush=True)
    existing_book_desc_ids = _load_existing_book_desc_ids(conn)
    print(f"summary: existing_book_desc_ids={len(existing_book_desc_ids)}", flush=True)

    archive_path: Path | None = None
    if tarball_path:
        candidate_path = Path(tarball_path)
        if candidate_path.exists():
            archive_path = candidate_path
            print(f"stage: using cached tarball {archive_path}", flush=True)
        else:
            print(
                f"stage: cache miss at {candidate_path}; downloading and caching from {source_url}",
                flush=True,
            )
            archive_path = _download_tarball(source_url, candidate_path)

    if archive_path is None:
        archive_path = _download_tarball(source_url, DEFAULT_SOURCE_TARBALL_PATH)

    matched = 0
    inserted = 0
    skipped_existing = 0
    skipped = 0
    ambiguous = 0
    empty_summary = 0
    previewed = 0
    pending_writes = 0
    processed = 0
    unmatched_examples: list[str] = []
    skip_reasons: dict[str, int] = defaultdict(int)
    match_reasons: dict[str, int] = defaultdict(int)

    if dry_run:
        print("stage: dry-run enabled; no writes will be committed", flush=True)

    print("stage: extracting summaries", flush=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        member = _find_summary_member(tar)
        print(f"stage: reading {member.name}", flush=True)
        extracted = tar.extractfile(member)
        if extracted is None:
            raise FileNotFoundError(f"Could not open {member.name} in archive")

        reader = csv.reader((line.decode("utf-8", errors="ignore") for line in extracted), delimiter="\t")
        for index, row in enumerate(reader, start=1):
            if len(row) < 7:
                continue
            processed += 1
            if limit is not None and index > limit:
                break

            genres_text, genres_json = _parse_genres(row[5])
            summary = row[6].strip()
            if not summary:
                skipped += 1
                empty_summary += 1
                skip_reasons["empty_summary"] += 1
                continue

            book, result = _select_catalog_book(row, title_index, author_index, token_index, all_books)
            if book is None:
                skipped += 1
                if result:
                    reason = str(result.get("reason") or "unknown")
                    skip_reasons[reason] += 1
                    if result["reason"] == "ambiguous_match":
                        ambiguous += 1
                    if len(unmatched_examples) < unmatched_limit:
                        unmatched_examples.append(
                            f"{reason}: {result['title']} ({result.get('author') or 'Unknown'})"
                        )
                continue

            if book["bookid"] in existing_book_desc_ids:
                matched += 1
                if result:
                    match_reasons[str(result.get("reason") or "unknown")] += 1
                skipped_existing += 1
                if processed % max(1, progress_every) == 0:
                    print(
                        f"stage: progress processed={processed} matched={matched} inserted={inserted} "
                        f"skipped_existing={skipped_existing} previewed={previewed} skipped={skipped} "
                        f"pending_writes={pending_writes}",
                        flush=True,
                    )
                continue

            matched += 1
            if result:
                match_reasons[str(result.get("reason") or "unknown")] += 1
            print(
                f"summary {row[0]} -> bookid={book['bookid']} title={book['title']} author={row[3]} "
                f"genres={genres_text[:80] or 'None'}",
                flush=True,
            )
            if dry_run:
                previewed += 1
                if processed % max(1, progress_every) == 0:
                    print(
                        f"stage: progress processed={processed} matched={matched} inserted={inserted} "
                        f"previewed={previewed} skipped={skipped} pending_writes={pending_writes}",
                        flush=True,
                    )
                continue

            _save_book_desc(
                conn,
                bookid=book["bookid"],
                wikipedia_id=int(row[0]) if row[0].strip().isdigit() else None,
                freebase_id=row[1].strip() or None,
                title=row[2].strip(),
                author=row[3].strip() or None,
                publication_date=row[4].strip() or None,
                genres_text=genres_text,
                genres_json=genres_json,
                summary=summary,
                )
            inserted += 1
            existing_book_desc_ids.add(book["bookid"])
            pending_writes += 1
            if pending_writes >= max(1, commit_every):
                print(f"stage: committing {pending_writes} book_desc rows", flush=True)
                conn.commit()
                pending_writes = 0

            if processed % max(1, progress_every) == 0:
                print(
                    f"stage: progress processed={processed} matched={matched} inserted={inserted} "
                    f"skipped_existing={skipped_existing} previewed={previewed} skipped={skipped} "
                    f"pending_writes={pending_writes}",
                    flush=True,
                )

    if pending_writes:
        print(f"stage: committing final {pending_writes} book_desc rows", flush=True)
        conn.commit()

    print(
        "done: "
        f"matched={matched}, inserted={inserted}, skipped_existing={skipped_existing}, "
        f"previewed={previewed}, skipped={skipped}, ambiguous={ambiguous}, "
        f"empty_summary={empty_summary}, "
        f"no_title_match={skip_reasons.get('no_title_match', 0)}, "
        f"author_mismatch={skip_reasons.get('author_mismatch', 0)}, "
        f"ambiguous_match={skip_reasons.get('ambiguous_match', 0)}, "
        f"author_exact_match={match_reasons.get('author_exact_match', 0)}, "
        f"author_split_match={match_reasons.get('author_split_match', 0)}, "
        f"title_only_fallback={match_reasons.get('title_only_fallback', 0)}, "
        f"title_exact_match={match_reasons.get('title_exact_match', 0)}, "
        f"title_loose_match={match_reasons.get('title_loose_match', 0)}",
        flush=True,
    )
    if unmatched_examples:
        print("unmatched_examples:", flush=True)
        for example in unmatched_examples:
            print(f"  {example}", flush=True)
    conn.close()


def main() -> None:
    args = parse_args()
    backfill(
        args.db_path,
        args.source_url,
        args.tarball_path,
        args.limit,
        args.dry_run,
        args.unmatched_limit,
        args.commit_every,
        args.progress_every,
    )


if __name__ == "__main__":
    main()
