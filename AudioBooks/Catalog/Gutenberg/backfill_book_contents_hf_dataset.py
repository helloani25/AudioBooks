from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from datasets import Dataset, DatasetDict, load_dataset, load_dataset_builder
from dotenv import load_dotenv
from gutenbergpy.textget import strip_headers
from AudioBooks.Catalog.Gutenberg.db_utils import (
    connect_db as _connect_db,
    ensure_book_contents_table as _ensure_book_contents_table,
)


DATASET_NAME = "manu/project_gutenberg"
DEFAULT_SPLIT = "en"
DB_PATH = Path(__file__).resolve().parent.parent / "DB" / "gutenbergindex.db"
GUTENBERG_ID_RE = re.compile(r"eBook\s*#(\d+)", re.IGNORECASE)

load_dotenv()
hf_token = os.getenv('HF_TOKEN')

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect and import the Project Gutenberg HF dataset into book_contents.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--split",
        default=None,
        help="Dataset split to load. For manu/project_gutenberg this is usually a language code like 'en'.",
    )
    group.add_argument(
        "--all-splits",
        action="store_true",
        help="Load and import every split in the dataset dictionary.",
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
        help="Stop after processing this many dataset rows.",
    )
    parser.add_argument(
        "--print-columns",
        action="store_true",
        help="Print the dataset columns and a sample row, then exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Match dataset rows to DB books without writing to book_contents.",
    )
    return parser.parse_args()


def _to_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def _extract_gutenberg_id(row: dict) -> int | None:
    raw_id = row.get("id")
    if raw_id is not None:
        raw_id = str(raw_id).strip()
        prefix = raw_id.split("-", 1)[0]
        if prefix.isdigit():
            return int(prefix)

    text = row.get("text", "")
    if text:
        match = GUTENBERG_ID_RE.search(text)
        if match:
            return int(match.group(1))

    return None


def load_books_dataset(split: str):
    return load_dataset(DATASET_NAME, split=split)


def load_all_books_datasets() -> DatasetDict:
    return load_dataset(DATASET_NAME)


def get_existing_gutenberg_ids(db_path: str) -> set[int]:
    conn = _connect_db(db_path)
    cur = conn.cursor()
    cur.execute("SELECT gutenbergbookid FROM books WHERE gutenbergbookid IS NOT NULL")
    ids = {int(row[0]) for row in cur.fetchall() if row[0] is not None}
    conn.close()
    return ids


def get_existing_book_content_ids(db_path: str) -> set[int]:
    conn = _connect_db(db_path)
    cur = conn.cursor()
    cur.execute("SELECT bookid FROM book_contents")
    ids = {int(row[0]) for row in cur.fetchall() if row[0] is not None}
    conn.close()
    return ids


def upsert_book_content(db_path: str, book_id: int, raw_text: str, clean_text: str) -> None:
    conn = _connect_db(db_path)
    cur = conn.cursor()
    cur.execute(
        """
            INSERT OR REPLACE INTO book_contents (bookid, raw_content, clean_content, download_date)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (book_id, raw_text, clean_text),
    )
    conn.commit()
    conn.close()

def _import_one_dataset(
    dataset: Dataset,
    db_path: str,
    *,
    label: str = "",
    limit: int | None = None,
    dry_run: bool = False,
    existing_gutenberg_ids: set[int],
    existing_content_ids: set[int],
) -> dict[str, int]:
    prefix = f"[{label}] " if label else ""
    print(f"{prefix}dataset columns: {dataset.column_names}")
    if len(dataset) > 0:
        print(f"{prefix}sample row keys: {list(dataset[0].keys())}")

    matched = 0
    inserted = 0
    skipped_missing = 0
    skipped_existing = 0

    for index, row in enumerate(dataset):
        if limit is not None and index >= limit:
            break

        gutenberg_id = _extract_gutenberg_id(row)
        if gutenberg_id is None:
            skipped_missing += 1
            continue

        text = row.get("text", "")
        if not text:
            skipped_missing += 1
            continue

        if gutenberg_id not in existing_gutenberg_ids:
            skipped_missing += 1
            continue

        matched += 1

        if gutenberg_id in existing_content_ids:
            skipped_existing += 1
            continue

        raw_text = _to_text(text)
        try:
            clean_text = _to_text(strip_headers(raw_text.encode("utf-8")))
        except Exception:
            clean_text = raw_text

        if not dry_run:
            upsert_book_content(db_path, gutenberg_id, raw_text, clean_text)
            existing_content_ids.add(gutenberg_id)
        inserted += 1

        if inserted % 100 == 0:
            print(f"{prefix}processed {inserted} books")

    stats = {
        "matched": matched,
        "inserted": inserted,
        "skipped_missing": skipped_missing,
        "skipped_existing": skipped_existing,
    }
    print(
        f"{prefix}done: "
        f"matched={matched}, inserted={inserted}, "
        f"skipped_missing={skipped_missing}, skipped_existing={skipped_existing}"
    )
    return stats


def import_dataset(
    dataset: Dataset | DatasetDict,
    db_path: str,
    limit: int | None = None,
    dry_run: bool = False,
) -> None:
    existing_gutenberg_ids = get_existing_gutenberg_ids(db_path)
    existing_content_ids = get_existing_book_content_ids(db_path)

    total_stats = {
        "matched": 0,
        "inserted": 0,
        "skipped_missing": 0,
        "skipped_existing": 0,
    }

    if isinstance(dataset, DatasetDict):
        for split_name, split_dataset in dataset.items():
            split_stats = _import_one_dataset(
                split_dataset,
                db_path,
                label=split_name,
                limit=limit,
                dry_run=dry_run,
                existing_gutenberg_ids=existing_gutenberg_ids,
                existing_content_ids=existing_content_ids,
            )
            for key in total_stats:
                total_stats[key] += split_stats[key]
    else:
        total_stats = _import_one_dataset(
            dataset,
            db_path,
            limit=limit,
            dry_run=dry_run,
            existing_gutenberg_ids=existing_gutenberg_ids,
            existing_content_ids=existing_content_ids,
        )

    print(
        "overall: "
        f"matched={total_stats['matched']}, inserted={total_stats['inserted']}, "
        f"skipped_missing={total_stats['skipped_missing']}, "
        f"skipped_existing={total_stats['skipped_existing']}"
    )


def main() -> None:
    args = parse_args()

    if args.print_columns:
        builder = load_dataset_builder(DATASET_NAME)
        print(f"dataset features: {builder.info.features}")
        print(f"available splits: {list(builder.info.splits.keys())}")
        return

    if args.all_splits:
        dataset = load_all_books_datasets()
    else:
        dataset = load_books_dataset(args.split or DEFAULT_SPLIT)
    import_dataset(dataset, args.db_path, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
