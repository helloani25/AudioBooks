"""
Merge duplicate catalog entries for the same book into one canonical record.

Modes
-----
--find-duplicates           Detect and print candidate duplicate groups (no writes).
--auto-merge                Detect all duplicates and merge them automatically.
--source N --target N       Merge a specific pair explicitly.

All modes accept --dry-run to preview without writing.

Detection
---------
Books are grouped by (normalized title, normalized author tokens). A group with
2+ entries is a merge candidate.  Titles are lowercased, punctuation-stripped,
and common stop-words removed ("the", "a", "an", "of", ...).  Author tokens are
pooled across all pipe-separated author names.

Target selection (auto)
-----------------------
Within each duplicate group the script scores each book and picks the highest as
the canonical target:

  score = content_bytes
        + 1_000_000 * has_real_desc
        +   500_000 * has_audio
        + numdownloads

The remaining books become sources and are merged into the target one at a time.

Migration (source → target)
---------------------------
book_desc        Copied if target has none, or if target only has a "catalog"
                 placeholder and source has a real CMU/Wikipedia summary.
book_contents    Copied (or replaced) when source content is larger than target.
book_audio       Copied from the book_audio table when present; otherwise built
                 from the source's downloadlinks MP3 entries.
book_audio_chapters  Copied together with book_audio.
book_cover_art   Keyed on Gutenberg ID — no rows to move.

Deletion (source only)
----------------------
Custom tables:  book_desc, book_contents, book_audio, book_audio_chapters
Core catalog:   downloadlinks, book_subjects, book_authors, titles, books

After running, restart Flask to clear in-process caches.

Examples
--------
# List candidates without touching anything
python merge_books.py --find-duplicates

# Dry-run auto-merge to preview all merges
python merge_books.py --auto-merge --dry-run

# Merge all duplicates for real
python merge_books.py --auto-merge

# Merge one explicit pair
python merge_books.py --source 22025 --target 43478

# Limit auto-merge to the first 10 duplicate groups
python merge_books.py --auto-merge --limit 10
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AudioBooks.Catalog.Gutenberg.content_validation import detect_gutenberg_id_mismatch

DB_PATH = Path(__file__).resolve().parent.parent / "DB" / "gutenbergindex.db"

MP3_DOWNLOAD_TYPE_ID = 11

_STOP_WORDS = frozenset([
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or",
    "by", "from", "with", "as", "is", "it", "its", "be", "are", "was",
])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _log(msg: str) -> None:
    print(msg, flush=True)


def _normalize_title(title: str | None) -> str:
    if not title:
        return ""
    s = title.encode("ascii", errors="ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    # Keep single-digit tokens so "Vol. 1" and "Vol. 2" produce different keys.
    tokens = [t for t in s.split() if t and t not in _STOP_WORDS and (t.isdigit() or len(t) > 1)]
    return " ".join(tokens)


def _author_key(authors_pipe: str | None) -> tuple[str, ...]:
    """Return a sorted tuple of significant tokens across all pipe-separated author names."""
    if not authors_pipe:
        return ()
    tokens: set[str] = set()
    for name in authors_pipe.split("|"):
        name_norm = re.sub(r"[^a-z]+", " ", name.lower()).strip()
        tokens.update(t for t in name_norm.split() if len(t) > 2 and t not in _STOP_WORDS)
    return tuple(sorted(tokens))


def _score_book(cur: sqlite3.Cursor, book_id: int, numdownloads: int) -> int:
    """Higher score = better canonical target."""
    cur.execute("SELECT length(raw_content) FROM book_contents WHERE bookid = ?", (book_id,))
    row = cur.fetchone()
    content_bytes = int(row[0]) if row and row[0] else 0

    cur.execute("SELECT source FROM book_desc WHERE bookid = ?", (book_id,))
    row = cur.fetchone()
    has_real_desc = 1 if (row and row[0] != "catalog") else 0

    cur.execute("SELECT 1 FROM book_audio WHERE book_id = ?", (book_id,))
    has_audio = 1 if cur.fetchone() else 0

    return content_bytes + 1_000_000 * has_real_desc + 500_000 * has_audio + (numdownloads or 0)


# ── Duplicate detection ───────────────────────────────────────────────────────

def _find_duplicate_groups(conn: sqlite3.Connection) -> list[list[dict]]:
    """
    Return a list of groups, each group being a list of book dicts with keys:
      id, title, authors, numdownloads
    Only groups with 2+ members are returned.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            b.id,
            b.numdownloads,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), '') AS title,
            COALESCE((
                SELECT GROUP_CONCAT(name, '|')
                FROM (
                    SELECT DISTINCT a2.name AS name
                    FROM authors a2
                    JOIN book_authors ba ON ba.authorid = a2.id
                    WHERE ba.bookid = b.id
                )
            ), '') AS authors
        FROM books b
        ORDER BY b.id
        """
    )
    rows = cur.fetchall()

    # Group by (normalized_title, author_key)
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        norm = _normalize_title(row["title"])
        if not norm:
            continue
        key = (norm, _author_key(row["authors"]))
        buckets[key].append({
            "id": row["id"],
            "title": row["title"],
            "authors": row["authors"].replace("|", ", "),
            "numdownloads": row["numdownloads"] or 0,
        })

    return [group for group in buckets.values() if len(group) >= 2]


# ── Data migration helpers ────────────────────────────────────────────────────

def _migrate_desc(cur: sqlite3.Cursor, source_id: int, target_id: int, dry_run: bool) -> None:
    cur.execute("SELECT source, summary FROM book_desc WHERE bookid = ?", (target_id,))
    target_desc = cur.fetchone()
    cur.execute("SELECT * FROM book_desc WHERE bookid = ?", (source_id,))
    src_desc = cur.fetchone()

    if not src_desc:
        _log("  [book_desc] source has no description — skipping.")
        return

    target_is_placeholder = target_desc and target_desc["source"] == "catalog"
    source_is_real = src_desc["source"] != "catalog"

    if target_desc and not target_is_placeholder:
        _log("  [book_desc] target already has a real description — skipping.")
        return

    if target_is_placeholder and not source_is_real:
        _log("  [book_desc] both are catalog placeholders — skipping.")
        return

    if not dry_run:
        cur.execute(
            """INSERT OR REPLACE INTO book_desc
               (bookid, wikipedia_id, freebase_id, source_title, source_author,
                publication_date, genres_text, genres_json, summary, source, download_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                target_id,
                src_desc["wikipedia_id"],
                src_desc["freebase_id"],
                src_desc["source_title"],
                src_desc["source_author"],
                src_desc["publication_date"],
                src_desc["genres_text"],
                src_desc["genres_json"],
                src_desc["summary"],
                src_desc["source"],
                src_desc["download_date"],
            ),
        )
    action = "Replaced placeholder with" if target_is_placeholder else "Copied"
    _log(f"  [book_desc] {action} source description (source={src_desc['source']}) → book_id={target_id}.")


def _migrate_contents(
    cur: sqlite3.Cursor,
    source_id: int,
    target_id: int,
    dry_run: bool,
    *,
    source_gutenberg_id: int | None,
    target_gutenberg_id: int | None,
) -> None:
    cur.execute("SELECT length(raw_content) AS sz, raw_content, clean_content FROM book_contents WHERE bookid = ?", (target_id,))
    target_row = cur.fetchone()
    cur.execute("SELECT length(raw_content) AS sz, raw_content, clean_content FROM book_contents WHERE bookid = ?", (source_id,))
    source_row = cur.fetchone()

    if not source_row:
        _log("  [book_contents] source has no content — skipping.")
        return

    if source_gutenberg_id is not None and target_gutenberg_id is not None and source_gutenberg_id != target_gutenberg_id:
        _log(
            "  [book_contents] source/target Gutenberg IDs differ "
            f"({source_gutenberg_id} vs {target_gutenberg_id}) — skipping content migration."
        )
        return

    source_mismatch, source_detected_id = detect_gutenberg_id_mismatch(source_row["raw_content"], source_gutenberg_id)
    if source_mismatch:
        _log(
            "  [book_contents] source payload Gutenberg ID mismatch "
            f"(expected={source_gutenberg_id}, detected={source_detected_id}) — skipping."
        )
        return

    target_invalid = False
    if target_row:
        target_mismatch, target_detected_id = detect_gutenberg_id_mismatch(target_row["raw_content"], target_gutenberg_id)
        if target_mismatch:
            target_invalid = True
            _log(
                "  [book_contents] target payload Gutenberg ID mismatch "
                f"(expected={target_gutenberg_id}, detected={target_detected_id}) — treating as invalid."
            )

    src_sz = source_row["sz"] or 0
    tgt_sz = 0 if target_invalid else (target_row["sz"] if target_row else 0)

    if tgt_sz >= src_sz:
        _log(f"  [book_contents] target content is equal or larger ({tgt_sz} vs {src_sz} bytes) — keeping target.")
        return

    if not dry_run:
        cur.execute(
            "INSERT OR REPLACE INTO book_contents (bookid, raw_content, clean_content, download_date) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (target_id, source_row["raw_content"], source_row["clean_content"]),
        )
    action = "Replaced" if target_row else "Copied"
    _log(f"  [book_contents] {action} target with source content ({src_sz} bytes, was {tgt_sz}).")


def _migrate_audio(cur: sqlite3.Cursor, source_id: int, target_id: int, dry_run: bool) -> None:
    cur.execute("SELECT 1 FROM book_audio WHERE book_id = ?", (target_id,))
    if cur.fetchone():
        _log("  [book_audio] target already has audio — skipping.")
        return

    # Prefer copying from book_audio table first.
    cur.execute("SELECT * FROM book_audio WHERE book_id = ?", (source_id,))
    src_audio = cur.fetchone()
    if src_audio:
        cur.execute(
            "SELECT * FROM book_audio_chapters WHERE book_id = ? ORDER BY track_order",
            (source_id,),
        )
        chapters = cur.fetchall()
        if not dry_run:
            cur.execute(
                """INSERT OR REPLACE INTO book_audio
                   (book_id, package_url, audio_format, track_count, is_chaptered,
                    narrator, narrator_source, is_synthesized, download_date)
                   VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    target_id,
                    src_audio["package_url"],
                    src_audio["audio_format"],
                    src_audio["track_count"],
                    src_audio["is_chaptered"],
                    src_audio["narrator"],
                    src_audio["narrator_source"],
                    src_audio["is_synthesized"],
                ),
            )
            cur.execute("DELETE FROM book_audio_chapters WHERE book_id = ?", (target_id,))
            for ch in chapters:
                cur.execute(
                    """INSERT INTO book_audio_chapters
                       (book_id, track_order, chapter_title, track_url, audio_format, duration, download_date)
                       VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (
                        target_id,
                        ch["track_order"],
                        ch["chapter_title"],
                        ch["track_url"],
                        ch["audio_format"],
                        ch["duration"] if "duration" in ch.keys() else None,
                    ),
                )
        _log(f"  [book_audio] Copied {len(chapters)} chapter(s) from book_audio table → book_id={target_id}.")
        return

    # Fallback: build from source downloadlinks.
    cur.execute(
        "SELECT name FROM downloadlinks WHERE bookid = ? AND downloadtypeid = ? ORDER BY name",
        (source_id, MP3_DOWNLOAD_TYPE_ID),
    )
    mp3_urls = [r["name"] for r in cur.fetchall()]
    if not mp3_urls:
        _log("  [book_audio] source has no audio (table or downloadlinks) — skipping.")
        return

    package_url = mp3_urls[0].rsplit("/", 1)[0] + "/"
    if not dry_run:
        cur.execute(
            """INSERT OR REPLACE INTO book_audio
               (book_id, package_url, audio_format, track_count, is_chaptered, is_synthesized, download_date)
               VALUES (?,?,'mp3',?,1,0,CURRENT_TIMESTAMP)""",
            (target_id, package_url, len(mp3_urls)),
        )
        cur.execute("DELETE FROM book_audio_chapters WHERE book_id = ?", (target_id,))
        for order, url in enumerate(mp3_urls):
            cur.execute(
                """INSERT INTO book_audio_chapters
                   (book_id, track_order, chapter_title, track_url, audio_format, download_date)
                   VALUES (?,?,NULL,?,'mp3',CURRENT_TIMESTAMP)""",
                (target_id, order, url),
            )
    _log(f"  [book_audio] Built {len(mp3_urls)} tracks from downloadlinks → book_id={target_id} ({package_url}).")


def _self_fill_audio_from_downloadlinks(cur: sqlite3.Cursor, target_id: int, dry_run: bool) -> None:
    """If the target has MP3 downloadlinks but no book_audio row, build one."""
    cur.execute("SELECT 1 FROM book_audio WHERE book_id = ?", (target_id,))
    if cur.fetchone():
        return

    cur.execute(
        "SELECT name FROM downloadlinks WHERE bookid = ? AND downloadtypeid = ? ORDER BY name",
        (target_id, MP3_DOWNLOAD_TYPE_ID),
    )
    mp3_urls = [r["name"] for r in cur.fetchall()]
    if not mp3_urls:
        return

    package_url = mp3_urls[0].rsplit("/", 1)[0] + "/"
    if not dry_run:
        cur.execute(
            """INSERT OR REPLACE INTO book_audio
               (book_id, package_url, audio_format, track_count, is_chaptered, is_synthesized, download_date)
               VALUES (?,?,'mp3',?,1,0,CURRENT_TIMESTAMP)""",
            (target_id, package_url, len(mp3_urls)),
        )
        cur.execute("DELETE FROM book_audio_chapters WHERE book_id = ?", (target_id,))
        for order, url in enumerate(mp3_urls):
            cur.execute(
                """INSERT INTO book_audio_chapters
                   (book_id, track_order, chapter_title, track_url, audio_format, download_date)
                   VALUES (?,?,NULL,?,'mp3',CURRENT_TIMESTAMP)""",
                (target_id, order, url),
            )
    _log(f"  [book_audio] Built {len(mp3_urls)} tracks from target's own downloadlinks ({package_url}).")


def _migrate_cover_art(cur: sqlite3.Cursor, source_id: int, target_id: int, dry_run: bool) -> None:
    """Copy source's cover art (keyed on its Gutenberg ID) to target's Gutenberg ID."""
    cur.execute("SELECT gutenbergbookid FROM books WHERE id = ?", (source_id,))
    row = cur.fetchone()
    if not row or row[0] is None:
        _log("  [book_cover_art] source has no Gutenberg ID — skipping.")
        return
    source_gid = row[0]

    cur.execute("SELECT gutenbergbookid FROM books WHERE id = ?", (target_id,))
    row = cur.fetchone()
    if not row or row[0] is None:
        _log("  [book_cover_art] target has no Gutenberg ID — skipping.")
        return
    target_gid = row[0]

    if source_gid == target_gid:
        _log("  [book_cover_art] same Gutenberg ID — no migration needed.")
        return

    cur.execute("SELECT COUNT(*) FROM book_cover_art WHERE bookid = ?", (source_gid,))
    src_cnt = cur.fetchone()[0]
    if not src_cnt:
        _log("  [book_cover_art] source has no cover art — skipping.")
        return

    if not dry_run:
        cur.execute(
            """INSERT OR IGNORE INTO book_cover_art
               (bookid, size_label, sort_order, image_url, mime_type, byte_size, rdf_url, source, download_date)
               SELECT ?, size_label, sort_order, image_url, mime_type, byte_size, rdf_url, source, download_date
               FROM book_cover_art WHERE bookid = ?""",
            (target_gid, source_gid),
        )
    _log(f"  [book_cover_art] Copied {src_cnt} row(s) from gutenberg_id={source_gid} → {target_gid}.")


def _delete_source(cur: sqlite3.Cursor, source_id: int, dry_run: bool) -> None:
    for table, col in [
        ("book_desc", "bookid"),
        ("book_contents", "bookid"),
        ("book_audio_chapters", "book_id"),
        ("book_audio", "book_id"),
        ("downloadlinks", "bookid"),
        ("book_subjects", "bookid"),
        ("book_authors", "bookid"),
        ("titles", "bookid"),
    ]:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (source_id,))
        cnt = cur.fetchone()[0]
        if cnt:
            if not dry_run:
                cur.execute(f"DELETE FROM {table} WHERE {col} = ?", (source_id,))
            _log(f"  [{table}] Deleted {cnt} row(s) for source book_id={source_id}.")

    cur.execute("SELECT COUNT(*) FROM books WHERE id = ?", (source_id,))
    if cur.fetchone()[0]:
        if not dry_run:
            cur.execute("DELETE FROM books WHERE id = ?", (source_id,))
        _log(f"  [books] Removed book_id={source_id}.")


# ── Core merge ────────────────────────────────────────────────────────────────

def merge(source_id: int, target_id: int, *, conn: sqlite3.Connection, dry_run: bool) -> None:
    cur = conn.cursor()

    cur.execute("SELECT id, gutenbergbookid FROM books WHERE id IN (?, ?)", (source_id, target_id))
    found = {r["id"]: r["gutenbergbookid"] for r in cur.fetchall()}
    if source_id not in found:
        _log(f"  ERROR: source book_id={source_id} not found — skipping.")
        return
    if target_id not in found:
        _log(f"  ERROR: target book_id={target_id} not found — skipping.")
        return

    _log(f"Merging book_id={source_id} (gutenberg={found[source_id]}) → book_id={target_id} (gutenberg={found[target_id]})")
    _migrate_desc(cur, source_id, target_id, dry_run)
    _migrate_contents(
        cur,
        source_id,
        target_id,
        dry_run,
        source_gutenberg_id=int(found[source_id]) if found[source_id] is not None else None,
        target_gutenberg_id=int(found[target_id]) if found[target_id] is not None else None,
    )
    _migrate_audio(cur, source_id, target_id, dry_run)
    _self_fill_audio_from_downloadlinks(cur, target_id, dry_run)
    _migrate_cover_art(cur, source_id, target_id, dry_run)
    _delete_source(cur, source_id, dry_run)

    if not dry_run:
        conn.commit()
        _log("  Committed.")
    else:
        _log("  [DRY RUN] no changes written.")


# ── Multi-merge helpers ───────────────────────────────────────────────────────

def _select_targets(conn: sqlite3.Connection, group: list[dict]) -> list[tuple[int, int]]:
    """
    Return a list of (source_id, target_id) pairs for a group.
    The highest-scored book becomes the single target; all others are sources.
    """
    cur = conn.cursor()
    scored = [
        (book["id"], _score_book(cur, book["id"], book["numdownloads"]))
        for book in group
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    target_id = scored[0][0]
    return [(src_id, target_id) for src_id, _ in scored[1:]]


def find_duplicates_cmd(conn: sqlite3.Connection) -> None:
    groups = _find_duplicate_groups(conn)
    if not groups:
        _log("No duplicate groups found.")
        return

    cur = conn.cursor()
    _log(f"Found {len(groups)} duplicate group(s):\n")
    for i, group in enumerate(groups, 1):
        scored = sorted(
            [(b["id"], _score_book(cur, b["id"], b["numdownloads"])) for b in group],
            key=lambda x: x[1],
            reverse=True,
        )
        target_id = scored[0][0]
        _log(f"Group {i}  (auto-target → book_id={target_id})")
        for book in group:
            marker = "★ TARGET" if book["id"] == target_id else "  source"
            _log(f"  [{marker}] book_id={book['id']:6d}  downloads={book['numdownloads']:6d}  {book['title']!r}")
            _log(f"              authors: {book['authors']}")
        _log("")


def auto_merge_cmd(conn: sqlite3.Connection, *, dry_run: bool, limit: int | None) -> None:
    groups = _find_duplicate_groups(conn)
    if not groups:
        _log("No duplicate groups found.")
        return

    if limit is not None:
        groups = groups[:limit]

    _log(f"Processing {len(groups)} duplicate group(s)...\n")
    total_merges = 0
    for group in groups:
        pairs = _select_targets(conn, group)
        for source_id, target_id in pairs:
            merge(source_id, target_id, conn=conn, dry_run=dry_run)
            total_merges += 1
            _log("")

    suffix = " (dry run)" if dry_run else ""
    _log(f"Done{suffix}: {total_merges} merge(s) across {len(groups)} group(s).")
    if not dry_run:
        _log("Restart Flask to clear in-process caches.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge duplicate catalog book entries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--find-duplicates", action="store_true", help="Detect and list duplicate groups without merging.")
    mode.add_argument("--auto-merge", action="store_true", help="Detect and merge all duplicate groups automatically.")
    parser.add_argument("--source", type=int, help="book_id to merge FROM (will be deleted). Requires --target.")
    parser.add_argument("--target", type=int, help="book_id to merge INTO (canonical, kept). Requires --source.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to SQLite.")
    parser.add_argument("--limit", type=int, default=None, help="Max duplicate groups to process (--auto-merge only).")
    args = parser.parse_args()

    if args.find_duplicates:
        conn = _open_db()
        find_duplicates_cmd(conn)
        conn.close()
        return

    if args.auto_merge:
        conn = _open_db()
        auto_merge_cmd(conn, dry_run=args.dry_run, limit=args.limit)
        conn.close()
        return

    if args.source is not None and args.target is not None:
        conn = _open_db()
        merge(args.source, args.target, conn=conn, dry_run=args.dry_run)
        if not args.dry_run:
            _log("\nRestart Flask to clear in-process caches.")
        conn.close()
        return

    parser.error("Specify --find-duplicates, --auto-merge, or both --source and --target.")


if __name__ == "__main__":
    main()
