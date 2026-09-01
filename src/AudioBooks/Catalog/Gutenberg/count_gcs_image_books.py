from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from google.cloud import storage

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "DB" / "gutenbergindex.db"
DEFAULT_BUCKET = os.environ.get("GCS_BUCKET")
DEFAULT_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
DEFAULT_PREFIX = "book-html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Count distinct Gutenberg IDs in GCS that have image objects under "
            "book-html/<gid>/images/, then map them to internal catalog book IDs."
        ),
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to gutenbergindex.db")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="GCS bucket name (default: GCS_BUCKET from .env)")
    parser.add_argument(
        "--gcs-credentials",
        default=DEFAULT_CREDENTIALS,
        metavar="KEY_FILE",
        help="Service account JSON key file. Defaults to GOOGLE_APPLICATION_CREDENTIALS env var.",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="GCS prefix root for HTML images (default: book-html)",
    )
    parser.add_argument(
        "--show-duplicate-mappings",
        action="store_true",
        help="Print Gutenberg IDs that map to multiple internal book IDs.",
    )
    parser.add_argument(
        "--duplicate-limit",
        type=int,
        default=20,
        help="Max duplicate Gutenberg IDs to print when --show-duplicate-mappings is set (default: 20)",
    )
    return parser.parse_args()


def _resolve_credentials_path(path_value: str | None) -> str | None:
    if not path_value:
        return None
    raw = Path(path_value).expanduser()
    candidates = [raw]
    if not raw.is_absolute():
        audiobooks_root = PROJECT_ROOT / "AudioBooks"
        candidates.extend([PROJECT_ROOT / raw, audiobooks_root / raw])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(raw)


def _make_gcs_client(credentials_path: str | None):
    if credentials_path:
        from google.oauth2 import service_account

        resolved = _resolve_credentials_path(credentials_path)
        creds = service_account.Credentials.from_service_account_file(str(resolved))
        return storage.Client(credentials=creds)
    return storage.Client()


def _extract_gids_from_bucket(client, bucket_name: str, prefix_root: str) -> set[int]:
    gid_set: set[int] = set()
    prefix = prefix_root.rstrip("/") + "/"

    for blob in client.list_blobs(bucket_name, prefix=prefix):
        # Expected format: book-html/<gid>/images/<filename>
        parts = blob.name.split("/")
        if len(parts) < 4:
            continue
        if parts[0] != prefix_root or parts[2] != "images":
            continue
        if not parts[3]:
            continue
        gid_token = parts[1]
        if gid_token.isdigit():
            gid_set.add(int(gid_token))

    return gid_set


def _chunked(values: list[int], size: int) -> list[list[int]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _map_to_internal_book_ids(db_path: str, gutenberg_ids: set[int]) -> tuple[set[int], set[int], dict[int, list[int]]]:
    if not gutenberg_ids:
        return set(), set(), {}

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        internal_ids: set[int] = set()
        matched_gids: set[int] = set()
        gid_to_bookids: dict[int, list[int]] = {}
        for chunk in _chunked(sorted(gutenberg_ids), 800):
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"SELECT id, gutenbergbookid FROM books WHERE gutenbergbookid IN ({placeholders})",
                chunk,
            )
            for book_id, gid in cur.fetchall():
                if gid is None:
                    continue
                bid = int(book_id)
                g = int(gid)
                internal_ids.add(bid)
                matched_gids.add(g)
                gid_to_bookids.setdefault(g, []).append(bid)
        return internal_ids, matched_gids, gid_to_bookids
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    if not args.bucket:
        raise ValueError("GCS bucket is required. Set GCS_BUCKET in .env or pass --bucket.")

    print(f"stage: connecting to GCS bucket={args.bucket}", flush=True)
    client = _make_gcs_client(args.gcs_credentials)
    print(f"stage: scanning prefix={args.prefix}/<gid>/images/", flush=True)
    gcs_gids = _extract_gids_from_bucket(client, args.bucket, args.prefix)

    print(f"stage: mapping Gutenberg IDs to internal book IDs via {args.db_path}", flush=True)
    internal_book_ids, matched_gids, gid_to_bookids = _map_to_internal_book_ids(args.db_path, gcs_gids)

    duplicate_gid_map = {gid: bids for gid, bids in gid_to_bookids.items() if len(bids) > 1}

    print(f"bucket={args.bucket}")
    print(f"gcs_gutenberg_ids_with_images={len(gcs_gids)}")
    print(f"db_internal_book_ids_mapped={len(internal_book_ids)}")
    print(f"db_gutenberg_ids_matched={len(matched_gids)}")
    print(f"gids_without_db_match={len(gcs_gids - matched_gids)}")
    print(f"gutenberg_ids_with_multiple_internal_books={len(duplicate_gid_map)}")

    if args.show_duplicate_mappings and duplicate_gid_map:
        print("duplicate_gid_samples:")
        shown = 0
        for gid in sorted(duplicate_gid_map):
            print(f"  gid={gid} book_ids={sorted(duplicate_gid_map[gid])}")
            shown += 1
            if shown >= max(0, args.duplicate_limit):
                break


if __name__ == "__main__":
    main()
