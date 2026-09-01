"""
Migrate books with content_type='html' from clean_content → html_content.

Books processed by an earlier version of backfill_book_html.py have their
Gutenberg HTML stored in clean_content (image paths already rewritten to
/api/books/{id}/images/).  This script re-cleans that HTML — strips boilerplate
and CSS — and writes the result into the new html_content column.

No network I/O: works entirely from what's already in the database.
Run once after upgrading backfill_book_html.py to use the html_content column.

Usage:
  python AudioBooks/Catalog/Gutenberg/migrate_html_content.py
  python AudioBooks/Catalog/Gutenberg/migrate_html_content.py --dry-run
  python AudioBooks/Catalog/Gutenberg/migrate_html_content.py --limit 200
  python AudioBooks/Catalog/Gutenberg/migrate_html_content.py --batch-size 200
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Generator

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.db_utils import connect_db, with_sqlite_retry, migrate_book_contents_html_content

try:
    import lxml.html as _lxml_html
    _HAS_LXML = True
except ImportError:
    _lxml_html = None  # type: ignore[assignment]
    _HAS_LXML = False

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
DEFAULT_BATCH_SIZE = 200

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate existing HTML from clean_content into html_content without re-downloading.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to gutenbergindex.db")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Books per DB batch (default: 200)")
    parser.add_argument("--limit", type=int, default=None, help="Stop after processing N books")
    parser.add_argument("--dry-run", action="store_true", help="Clean in memory without writing to SQLite")
    return parser.parse_args()


def _strip_gutenberg_html_nodes(body) -> None:
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


def _clean_existing_html(html: str) -> str:
    """Strip CSS and Gutenberg boilerplate from HTML already in the DB.

    Image paths are already rewritten to /api/books/{id}/images/ so no
    path rewriting is needed here — only structural cleaning.
    """
    if not _HAS_LXML or not html:
        return html
    try:
        doc = _lxml_html.document_fromstring(html)
        for el in doc.xpath("//style|//script|//link"):
            el.drop_tree()
        body_list = doc.xpath("//body")
        if body_list:
            _strip_gutenberg_html_nodes(body_list[0])
        return _lxml_html.tostring(doc, encoding="unicode", method="html")
    except Exception:
        return html


def _load_pending(db_path: str, limit: int | None) -> list[tuple[int, str]]:
    """Return (bookid, clean_content) for HTML books that lack html_content."""
    def query() -> list[tuple[int, str]]:
        conn = connect_db(db_path)
        try:
            cur = conn.cursor()
            limit_clause = f"LIMIT {limit}" if limit is not None else ""
            cur.execute(
                f"""
                SELECT bookid, clean_content
                FROM book_contents
                WHERE content_type = 'html'
                  AND (html_content IS NULL OR html_content = '')
                ORDER BY bookid
                {limit_clause}
                """
            )
            return [(int(row[0]), row[1] or "") for row in cur.fetchall()]
        finally:
            conn.close()

    return with_sqlite_retry(query)


def _batched(items: list, size: int) -> Generator[list, None, None]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _write_batch(db_path: str, updates: list[tuple[str, int]]) -> None:
    def write() -> None:
        conn = connect_db(db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executemany(
            "UPDATE book_contents SET html_content = ?, has_images = 1 WHERE bookid = ?",
            updates,
        )
        conn.commit()
        conn.close()

    with_sqlite_retry(write)


def main() -> None:
    args = parse_args()

    print(f"stage: ensuring html_content column exists in {args.db_path}", flush=True)
    migrate_book_contents_html_content(args.db_path)

    print("stage: loading HTML books missing html_content", flush=True)
    rows = _load_pending(args.db_path, args.limit)
    print(f"summary: books_to_migrate={len(rows)}", flush=True)
    if not rows:
        print("done: nothing to migrate", flush=True)
        return

    if args.dry_run:
        print("stage: dry-run enabled; no writes will be committed", flush=True)

    batches = list(_batched(rows, args.batch_size))
    total_done = 0
    total_empty = 0

    for batch_num, batch in enumerate(batches, 1):
        updates: list[tuple[str, int]] = []
        empty = 0
        for book_id, clean_content in batch:
            if not clean_content:
                empty += 1
                continue
            new_html = _clean_existing_html(clean_content)
            updates.append((new_html, book_id))

        if not args.dry_run and updates:
            _write_batch(args.db_path, updates)

        total_done += len(updates)
        total_empty += empty
        print(
            f"stage: batch {batch_num}/{len(batches)}"
            f"  migrated={total_done}  skipped_empty={total_empty}",
            flush=True,
        )

    print(
        f"done: total_migrated={total_done}  skipped_empty={total_empty}",
        flush=True,
    )


if __name__ == "__main__":
    main()
