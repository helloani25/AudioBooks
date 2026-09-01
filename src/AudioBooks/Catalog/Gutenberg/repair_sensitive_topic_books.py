from __future__ import annotations

import argparse
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"

DELETE_TABLES: tuple[tuple[str, str], ...] = (
    ("book_audio_chapters", "book_id"),
    ("book_audio", "book_id"),
    ("book_desc", "bookid"),
    ("book_contents", "bookid"),
    ("book_cover_art", "bookid"),
    ("book_content_backfill_queue", "bookid"),
    ("downloadlinks", "bookid"),
    ("book_subjects", "bookid"),
    ("book_authors", "bookid"),
    ("titles", "bookid"),
    ("books", "id"),
)


@dataclass(frozen=True)
class SensitiveRule:
    name: str
    patterns: tuple[re.Pattern[str], ...]
    include_text: bool = False


def _build_bidirectional_intent_patterns(
    action_terms: tuple[str, ...],
    theme_terms: tuple[str, ...],
    *,
    max_words_between: int = 4,
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    # Intent is treated as a local context signal: an endorsement verb must appear near the theme.
    action_group = "|".join(re.escape(term) for term in action_terms)
    theme_group = "|".join(re.escape(term) for term in theme_terms)
    gap = rf"(?:\W+\w+){{0,{max_words_between}}}\W+"
    forward = re.compile(rf"\b(?:{action_group})\b{gap}\b(?:{theme_group})\b", re.IGNORECASE)
    reverse = re.compile(rf"\b(?:{theme_group})\b{gap}\b(?:{action_group})\b", re.IGNORECASE)
    return forward, reverse


INTENT_ACTION_TERMS = (
    "worship",
    "praise",
    "glorify",
    "glorifies",
    "hail",
    "celebrate",
    "celebrates",
    "revel",
    "embrace",
    "serve",
    "devote",
    "honor",
    "adore",
    "invoke",
    "summon",
    "extol",
)

OCCULT_THEME_TERMS = (
    "satan",
    "devil",
    "lucifer",
    "demon",
    "demonic",
    "occult",
    "infernal",
    "hell",
)

VIOLENCE_THEME_TERMS = (
    "violence",
    "violent",
    "gore",
    "gory",
    "gruesome",
    "grisly",
    "bloodshed",
    "blood-soaked",
    "mutilation",
    "dismemberment",
    "torture",
    "decapitation",
    "beheading",
    "evisceration",
)


SENSITIVE_RULES: tuple[SensitiveRule, ...] = (
    SensitiveRule(
        name="erotic",
        patterns=(
            re.compile(r"\berotic\b", re.IGNORECASE),
            re.compile(r"\berotica\b", re.IGNORECASE),
            re.compile(r"\beroticism\b", re.IGNORECASE),
        ),
    ),
    SensitiveRule(
        name="gory-violence",
        patterns=(
            re.compile(r"\bgore\b", re.IGNORECASE),
            re.compile(r"\bgory\b", re.IGNORECASE),
            re.compile(r"\bgruesome\b", re.IGNORECASE),
            re.compile(r"\bgrisly\b", re.IGNORECASE),
            re.compile(r"\bgraphic violence\b", re.IGNORECASE),
            re.compile(r"\bslasher\b", re.IGNORECASE),
            re.compile(r"\bsplatter\b", re.IGNORECASE),
            re.compile(r"\bmutilat(?:e|ed|ion|ions)\b", re.IGNORECASE),
            re.compile(r"\bdismember(?:ed|ment|ments)?\b", re.IGNORECASE),
            re.compile(r"\beviscerat(?:e|ed|ion|ions)\b", re.IGNORECASE),
            re.compile(r"\bdecapitat(?:e|ed|ion|ions)\b", re.IGNORECASE),
            re.compile(r"\bbehead(?:ed|ing|ings)?\b", re.IGNORECASE),
            re.compile(r"\btortur(?:e|ed|ing|er|ers)?\b", re.IGNORECASE),
            re.compile(r"\bblood[- ]soaked\b", re.IGNORECASE),
        ),
        include_text=True,
    ),
    SensitiveRule(
        name="psychic",
        patterns=(
            re.compile(r"\bpsychic\b", re.IGNORECASE),
            re.compile(r"\bpsychics?\b", re.IGNORECASE),
            re.compile(r"\bpsychical\b", re.IGNORECASE),
        ),
    ),
    SensitiveRule(
        name="mahabharata",
        patterns=(
            re.compile(r"\bmahabharata\b", re.IGNORECASE),
            re.compile(r"\bmahābhārata\b", re.IGNORECASE),
        ),
    ),
    SensitiveRule(
        name="occult-intent",
        patterns=_build_bidirectional_intent_patterns(INTENT_ACTION_TERMS, OCCULT_THEME_TERMS),
        include_text=True,
    ),
    SensitiveRule(
        name="violence-intent",
        patterns=_build_bidirectional_intent_patterns(INTENT_ACTION_TERMS, VIOLENCE_THEME_TERMS),
        include_text=True,
    ),
    SensitiveRule(
        name="bible-attack",
        patterns=(
            re.compile(r"\bgnostic bible\b", re.IGNORECASE),
            re.compile(r"\bgnostic gospels?\b", re.IGNORECASE),
            re.compile(r"\banti[- ]?bible\b", re.IGNORECASE),
            re.compile(r"\bagainst the bible\b", re.IGNORECASE),
            re.compile(r"\battacking the bible\b", re.IGNORECASE),
            re.compile(r"\battack(?:ing)? on the bible\b", re.IGNORECASE),
            re.compile(r"\bbible criticism\b", re.IGNORECASE),
            re.compile(r"\bcriticism of the bible\b", re.IGNORECASE),
            re.compile(r"\banti[- ]?christian\b", re.IGNORECASE),
            re.compile(r"\bbible is false\b", re.IGNORECASE),
            re.compile(r"\battack(?:ing)? christianity\b", re.IGNORECASE),
        ),
        include_text=True,
    ),
)

TEXT_INCLUDED_RULES: tuple[SensitiveRule, ...] = tuple(rule for rule in SENSITIVE_RULES if rule.include_text)

# Conservative SQL prefilter terms for the text pass. This is used only to avoid
# loading every full book body when a row cannot possibly match any text rule.
GORY_PREFILTER_TERMS: tuple[str, ...] = (
    "gore",
    "gory",
    "gruesome",
    "grisly",
    "graphic violence",
    "slasher",
    "splatter",
    "mutilat",
    "dismember",
    "eviscerat",
    "decapitat",
    "behead",
    "tortur",
    "blood",
)

BIBLE_ATTACK_PREFILTER_TERMS: tuple[str, ...] = (
    "gnostic",
    "bible",
    "christian",
    "christianity",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete books whose metadata or text matches a small set of explicit sensitive-topic rules.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to the SQLite catalog database.")
    parser.add_argument("--dry-run", action="store_true", help="Preview deletions without writing to SQLite.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after deleting this many books.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print one line per scanned book instead of only matched deletions.",
    )
    parser.add_argument(
        "--book-id",
        action="append",
        type=int,
        dest="book_ids",
        help="Scan only the given internal book id(s). Can be passed multiple times.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=100,
        help="Commit SQLite deletes every N matched books (ignored in dry-run mode).",
    )
    parser.add_argument(
        "--db-fetch-size",
        type=int,
        default=500,
        help="Number of rows fetched per SQLite batch.",
    )
    parser.add_argument(
        "--disable-text-prefilter",
        action="store_true",
        help="Disable SQL text prefiltering and scan all book bodies for text rules.",
    )
    return parser.parse_args()


def _connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _iter_metadata_rows(
    conn: sqlite3.Connection,
    book_ids: set[int] | None,
    fetch_size: int,
):
    params: list[object] = []
    clause = ""
    if book_ids:
        placeholders = ",".join(["?"] * len(book_ids))
        clause = f"WHERE b.id IN ({placeholders})"
        params.extend(sorted(book_ids))

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            b.id AS book_id,
            b.gutenbergbookid,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title,
            COALESCE((
                SELECT GROUP_CONCAT(name, ' | ')
                FROM (
                    SELECT DISTINCT s.name AS name
                    FROM book_subjects bs
                    JOIN subjects s ON s.id = bs.subjectid
                    WHERE bs.bookid = b.id
                    ORDER BY s.name
                )
            ), '') AS subjects,
            COALESCE(d.summary, '') AS summary
        FROM books b
        LEFT JOIN book_desc d ON d.bookid = b.id
        {clause}
        ORDER BY b.id
        """,
        params,
    )
    batch_size = max(1, fetch_size)
    try:
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield rows
    finally:
        try:
            cur.close()
        except sqlite3.ProgrammingError:
            pass


def _build_like_any_clause(column_expr: str, terms: tuple[str, ...]) -> tuple[str, list[object]]:
    clause = " OR ".join(f"{column_expr} LIKE ?" for _ in terms)
    params: list[object] = [f"%{term}%" for term in terms]
    return f"({clause})", params


def _iter_text_rows(
    conn: sqlite3.Connection,
    book_ids: set[int] | None,
    fetch_size: int,
    disable_text_prefilter: bool,
):
    content_expr = "COALESCE(c.clean_content, c.raw_content, '')"
    conditions: list[str] = [f"{content_expr} != ''"]
    params: list[object] = []

    if book_ids:
        placeholders = ",".join(["?"] * len(book_ids))
        conditions.append(f"b.id IN ({placeholders})")
        params.extend(sorted(book_ids))
    elif not disable_text_prefilter:
        action_clause, action_params = _build_like_any_clause(content_expr, INTENT_ACTION_TERMS)
        occult_clause, occult_params = _build_like_any_clause(content_expr, OCCULT_THEME_TERMS)
        violence_clause, violence_params = _build_like_any_clause(content_expr, VIOLENCE_THEME_TERMS)
        gory_clause, gory_params = _build_like_any_clause(content_expr, GORY_PREFILTER_TERMS)
        bible_clause, bible_params = _build_like_any_clause(content_expr, BIBLE_ATTACK_PREFILTER_TERMS)
        intent_clause = f"({action_clause} AND ({occult_clause} OR {violence_clause}))"
        conditions.append(f"({gory_clause} OR {bible_clause} OR {intent_clause})")
        params.extend(gory_params)
        params.extend(bible_params)
        params.extend(action_params)
        params.extend(occult_params)
        params.extend(violence_params)

    clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            b.id AS book_id,
            b.gutenbergbookid,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title,
            {content_expr} AS text
        FROM books b
        JOIN book_contents c ON c.bookid = b.id
        {clause}
        ORDER BY b.id
        """,
        params,
    )
    batch_size = max(1, fetch_size)
    try:
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield rows
    finally:
        try:
            cur.close()
        except sqlite3.ProgrammingError:
            pass


def _build_metadata_blob(row: sqlite3.Row) -> str:
    return " \n ".join(
        [
            str(row["title"] or ""),
            str(row["subjects"] or ""),
            str(row["summary"] or ""),
        ]
    )


def _match_metadata_rules(metadata_blob: str) -> list[str]:
    matched: list[str] = []
    for rule in SENSITIVE_RULES:
        if any(pattern.search(metadata_blob) for pattern in rule.patterns):
            matched.append(rule.name)
    return matched


def _match_text_rules(content_blob: str) -> list[str]:
    matched: list[str] = []
    for rule in TEXT_INCLUDED_RULES:
        if any(pattern.search(content_blob) for pattern in rule.patterns):
            matched.append(rule.name)
    return matched


def _delete_book(conn: sqlite3.Connection, book_id: int) -> None:
    cur = conn.cursor()
    for table, column in DELETE_TABLES:
        cur.execute(f"DELETE FROM {table} WHERE {column} = ?", (book_id,))


def main() -> int:
    args = parse_args()
    if args.commit_every < 1:
        raise SystemExit("--commit-every must be >= 1")
    if args.db_fetch_size < 1:
        raise SystemExit("--db-fetch-size must be >= 1")

    conn = _connect_db(args.db_path)
    try:
        book_ids = set(args.book_ids) if args.book_ids else None
        scanned = 0
        text_scanned = 0
        matched = 0
        deleted = 0
        pending_commits = 0
        matched_book_ids: set[int] = set()

        # Pass 1: metadata scan (title/subjects/summary only).
        for batch in _iter_metadata_rows(conn, book_ids, args.db_fetch_size):
            for row in batch:
                scanned += 1
                book_id = int(row["book_id"])
                metadata_blob = _build_metadata_blob(row)
                reasons = _match_metadata_rules(metadata_blob)
                if not reasons:
                    if args.verbose:
                        print(f"scan book_id={row['book_id']} metadata-clear")
                    continue

                matched += 1
                matched_book_ids.add(book_id)
                title = str(row["title"] or "Untitled")
                gutenberg_id = row["gutenbergbookid"]
                print(
                    f"{'would delete' if args.dry_run else 'deleting'} "
                    f"book_id={row['book_id']} gutenberg_id={gutenberg_id} title={title!r} "
                    f"reasons={','.join(reasons)}"
                )

                if not args.dry_run:
                    _delete_book(conn, int(row["book_id"]))
                    deleted += 1
                    pending_commits += 1
                    if pending_commits >= args.commit_every:
                        conn.commit()
                        pending_commits = 0

                reached_limit = args.limit is not None and (
                    matched >= args.limit if args.dry_run else deleted >= args.limit
                )
                if reached_limit:
                    break
            else:
                continue
            break

        reached_limit = args.limit is not None and (matched >= args.limit if args.dry_run else deleted >= args.limit)
        # Pass 2: text-only scan for include_text rules; skip rows already matched from metadata.
        if not reached_limit:
            for batch in _iter_text_rows(
                conn,
                book_ids,
                args.db_fetch_size,
                args.disable_text_prefilter,
            ):
                for row in batch:
                    book_id = int(row["book_id"])
                    if book_id in matched_book_ids:
                        continue
                    text_scanned += 1
                    content_blob = str(row["text"] or "")
                    reasons = _match_text_rules(content_blob)
                    if not reasons:
                        if args.verbose:
                            print(f"scan book_id={book_id} text-clear")
                        continue

                    matched += 1
                    matched_book_ids.add(book_id)
                    title = str(row["title"] or "Untitled")
                    gutenberg_id = row["gutenbergbookid"]
                    print(
                        f"{'would delete' if args.dry_run else 'deleting'} "
                        f"book_id={book_id} gutenberg_id={gutenberg_id} title={title!r} "
                        f"reasons={','.join(reasons)}"
                    )

                    if not args.dry_run:
                        _delete_book(conn, book_id)
                        deleted += 1
                        pending_commits += 1
                        if pending_commits >= args.commit_every:
                            conn.commit()
                            pending_commits = 0

                    reached_limit = args.limit is not None and (
                        matched >= args.limit if args.dry_run else deleted >= args.limit
                    )
                    if reached_limit:
                        break
                else:
                    continue
                break

        if not args.dry_run and pending_commits:
            conn.commit()

        prefilter_status = "off" if args.disable_text_prefilter or book_ids else "on"
        print(
            f"completed scanned={scanned} text_scanned={text_scanned} "
            f"matched={matched} "
            f"text_prefilter={prefilter_status} "
            f"{'deleted=' + str(deleted) if not args.dry_run else 'deleted=0 (dry-run)'}"
        )
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
