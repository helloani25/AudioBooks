# AudioBooks System Design
A modular system for browsing and listening to Gutenberg project books.

## Modules
- **[Authentication](./Authentication)**: User signup, login, and session management.
- **[Catalog](./Catalog)**: Gutenberg metadata ingestion and API for book search/filtering.
- **[Presentation](./Presentation)**: React/Vite frontend for the user interface.

## Getting Started

1. **Install Dependencies**:
   ```bash
   pip install -r ../requirements.txt
   cd Presentation/library && npm install
   sudo port install redis
   cat /opt/local/etc/redis.conf
   redis-server
   ```

2. **Start the Backend**:
   Run from the project root:
   ```bash
   export PYTHONPATH=$PYTHONPATH:.
   python3 AudioBooks/app.py
   ```
   *Note: Using `PYTHONPATH` ensures that absolute imports like `from AudioBooks...` are resolved correctly.*


3. **Install npm**:
```bash
npm install
```
4. **Start the Frontend**:
   ```bash
   cd AudioBooks/Presentation/library
   npm run dev
   ```

## Redis Caching and Sessions
The system uses Redis for both sessions and catalog metadata caching if available. 
- **Sessions**: If Redis is unavailable, it falls back to the local filesystem (`flask_session` folder).
- **Catalog Caching**: If Redis is unavailable, it falls back to in-memory caching for the current process.

To use Redis, ensure a Redis server is running on `localhost:6379` or set the `REDIS_URL` environment variable:
```bash
export REDIS_URL="redis://your-redis-host:6379"
python3 AudioBooks/app.py
```

## API Endpoints
- `GET /api/csrf-token`: Returns a CSRF token for subsequent POST requests.
- `POST /api/signup`: Creates a new user account.
- `POST /api/login`: Authenticates a user and starts a session (supports Basic Auth).
- `POST /api/logout`: Ends the current session.
- `GET /api/me`: Returns the current user's profile information.
- `GET /api/books`: Returns a paginated list of books (supports `subject`, `search`, `limit`, `offset`).
- `GET /api/books/count`: Returns the total number of books matching filters.
- `GET /api/subjects`: Returns a list of available subjects and their book counts.


## Gutenberg Data

### Project Gutenberg Book Downloader

This is the legacy Project Gutenberg RDF cache builder built around `gutenbergpy`.

#### Usage

Run the script directly:

```bash
python metadata.py
```

#### What It Does

1. Builds or refreshes the local Gutenberg RDF cache
2. Uses the cached RDF files and `gutenbergpy` parsing logic to populate metadata
3. Is not part of the current repair/refresh flow for `book_contents`

#### Note

The only supported CLI flags here are TLS options:
- `--ca-bundle`
- `--ca-dir`
- `--no-verify`

The older `--refresh-downloadlinks` and `--repair-downloadlinks` examples are no longer valid for this file.

### SQLite Guttenberg metadata is included in the project.
- `guttenbergindex.db`
```
    sqlite3 "/Users/anithas/PycharmProjects/SystemDesign/AudioBooks/Catalog/DB/gutenbergindex.db"
```

### Running Gutenberg.py

The -m flag tells Python to search sys.path for the specified module and execute it as the __main__ module. 


#### Module Name, Not File Path: 
You use the module name (e.g., pkg.submodule) instead of a file path (pkg/submodule.py).
Package Awareness: Because it runs in the context of a package, it correctly resolves relative imports (like from . import utils), which usually fail if you run the script directly. 

#### Why Project Context and sys.path[0] Matter
sys.path is the list of directories Python searches to find modules. The first entry, sys.path[0], always takes precedence. 

#### Running Directly (python folder/script.py):
Python sets sys.path[0] to the directory containing the script (in this case, folder/).
The Problem: If script.py tries to import something from its parent project directory, it will fail because the parent is not in the search path.
#### Running as a Module (python -m project.folder.script):
Python sets sys.path[0] to the current working directory (where you are standing in your terminal).
The Benefit: If you run this from the project root, the entire project structure is now in sys.path. This allows Python to find all internal modules and sub-packages without you having to manually hack sys.path.append() into your code. 


__init__.py is necessary for Python to treat a directory as a package, but it does not make that package discoverable. For from AudioBooks.Catalog.Repository.CatalogRepository import CatalogRepository to work, Python must have the parent directory of AudioBooks on sys.path.

What usually happens here is:
•
You run Gutenberg.py directly from AudioBooks/Catalog/Gutenberg
•
Python sets sys.path[0] to that script directory
•
The repo root, which contains AudioBooks/, is not on the import path
•
So Python cannot resolve the top-level package name AudioBooks
That means the problem is not the missing __init__.py. The real issue is execution context and import path resolution.


The clean fixes are:
•
Run it as a module from the project root:
python -m AudioBooks.Catalog.Gutenberg.Gutenberg
•
Or add the project root to PYTHONPATH
•
Or keep the bootstrap code I added earlier that inserts the repo root into sys.path when the script is run directly

```python
PROJECT_ROOT = BASE_DIR.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    # Allow direct execution of this file from the Gutenberg folder.
    sys.path.insert(0, str(PROJECT_ROOT))
```

### Importing the Hugging Face Gutenberg dataset

Use `AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py` to inspect the dataset and write text into `book_contents`.

Run from the project root:
```bash
python AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --print-columns
python AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py
python AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --split fr
python AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --all-splits
python AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --dry-run --limit 20
python AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --db-path AudioBooks/Catalog/DB/gutenbergindex.db
```

Arguments:
- `--print-columns`: show dataset features and available splits, then exit.
- `--split <name>`: import one split only. Default is `en`.
- `--all-splits`: import every split in the dataset dictionary.
- `--dry-run`: match rows and count them without writing to SQLite.
- `--limit <n>`: stop after `n` rows per split.
- `--db-path <path>`: override the SQLite database location.

The importer matches Hugging Face rows by `row["id"]` against `books.gutenbergbookid`, then stores the text in `book_contents.bookid`.

### Backfilling missing books from Project Gutenberg

To fetch books that are present in the local catalog but still missing from `book_contents`, run:

```bash
python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --dry-run --limit 5
python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py
python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --limit 20
python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --no-verify
python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --workers 16 --mirror-tries 2
python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --chunk-size 1000
python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --preflight
python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --preflight-only
python AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --db-path AudioBooks/Catalog/DB/gutenbergindex.db --all-splits
```

This script uses the existing `downloadlinks` table in `gutenbergindex.db` and downloads books directly from Project Gutenberg mirrors. It does not require cloning any external repository.
It tries mirror URLs first, then falls back to the original Gutenberg URL if a mirror does not have that file.
`--chunk-size` controls how many books are kept in flight per batch when resolving candidates and running downloads.
The backfill extractor now also reads `.tex` files inside archives, and it can fall back to PDFs when `pypdf` is installed, which helps recover technical books that do not ship plain text.
XML downloadlinks are no longer treated as a content fallback. If Project Gutenberg returns a folio warning like `see #892 for HTML format, #733 for plain text`, the backfill code treats it as a failure for now. The known folio-only case is Gutenberg id `900`.

Preflight mode classifies each missing book before downloads start:
- `repair`: local cache rows look stale, so the script prefers the live Gutenberg file index.
- `refresh`: the local cache is missing usable rows, so the script also prefers the live Gutenberg file index.
- `audio-only`: the live index only exposes audio/video files, so the book is skipped for text backfill.
- `skip`: the local cache already has usable text candidates.

#### Resumable queue and discovery cache

The text backfill now keeps its run state in SQLite so repeated runs can resume instead of redoing discovery work.

Queue table:
- `book_content_backfill_queue`
- one row per `bookid` per run namespace
- tracks `status`, `attempts`, `last_error`, and the last successful source URL/type

Discovery cache table:
- `gutenberg_discovery_cache`
- caches the live Gutenberg file-index links and the local candidate lists keyed by Gutenberg id

Queue namespaces:
- `repair-all:v2`
- `missing:v2`
- `targeted:v2:<hash>`

The namespace is derived from the run mode, and seeding updates rows inside that namespace instead of creating a new queue on each run. That is what lets the importer resume after an interruption.

Useful flags:
- `--repair-all`: scan every catalog book with a Gutenberg id
- `--reset-queue`: clear the saved queue rows for the current namespace before starting
- `--refresh-discovery-cache`: ignore cached live-index/candidate data and rebuild it
- `--max-attempts`: stop retrying a book after repeated failures

Run combinations:
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py`: default missing-content run; classifies each book as skip or download and uses cached local candidates first.
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --preflight`: classification-only pass; prints repair/refresh/skip/audio-only without writing book text.
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --preflight-only`: print the plan and exit before any download work starts.
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --repair-all`: full catalog sweep; uses live Gutenberg discovery and decides repair vs refresh per book.
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --gutenberg-id 39074`: targeted run for one book; uses the `targeted:v2:<hash>` namespace.
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --gutenberg-id 39074 --force`: overwrite the targeted row even if `book_contents` already exists.
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --repair-all --reset-queue`: rerun the full sweep from scratch for the `repair-all:v2` namespace.
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --repair-all --refresh-discovery-cache`: force live-index and candidate discovery to be rebuilt instead of reused.
- `python AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --dry-run`: download and validate text in memory, but do not write to SQLite.

Tuning notes:
- `--chunk-size`: bigger chunks reduce SQL round trips and executor churn, but use more memory per batch.
- `--workers`: higher values increase parallel downloads, but returns diminish once network or Gutenberg starts throttling.
- `--mirror-tries`: more retries improve resilience on flaky mirrors, but each extra try adds network time per book.

The queue is seeded on every run, but existing rows are updated instead of duplicated. Completed rows stay completed across reruns until you reset the queue.

Pseudo code for the flow:

```text
run backfill(mode):
  ensure queue/cache tables exist
  queue_key = derive namespace from mode
  target_books = select books for mode
  seed queue rows with ON CONFLICT update

  queue_rows = load rows where status in pending/failed
  for each queue row:
    if repair-all:
      load cached local candidates
      load cached live Gutenberg index
      if live candidates exist:
        if audio/video only: skip
        else if local links are mismatched: repair
        else: refresh
      else if local candidates exist:
        download
      else:
        skip
    else if preflight:
      classify as repair/refresh/skip/audio-only
    else:
      use local supported candidates or skip

    if no usable candidates:
      mark skipped and continue

    download candidate text, trying mirrors first
    if valid text:
      save book_contents by internal book id
      mark queue row done
    else:
      mark queue row failed and increment attempts
```

### Download-link repair

Use `AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py` when the catalog metadata (title, author, Gutenberg ID, date) has drifted from what the book's actual download links point at. The script reads the Gutenberg ID embedded in each `downloadlinks` row, loads the matching RDF file from the local cache (`Catalog/DB/cache/epub/<id>/pg<id>.rdf`), and rewrites `books.gutenbergbookid`, `titles`, `book_authors`, and `books.dateissued` to match.

This is different from `backfill_missing_book_contents.py`, which fetches or refreshes **text content** — use `repair_catalog_from_downloadlinks.py` only when catalog metadata itself is wrong.

```bash
# Preview what would change without writing
python AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py --dry-run

# Repair a single book by internal id
python AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py --book-id 22025

# Repair all books whose download links point at Gutenberg id 98
python AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py --gutenberg-id 98

# Repair up to 500 books, committing every 100, with per-book output
python AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py --limit 500 --batch-size 100 --verbose
```

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--book-id N` | — | Repair only the given internal `books.id` (repeatable) |
| `--gutenberg-id N` | — | Repair books whose links point at this Gutenberg id (repeatable) |
| `--limit N` | — | Stop after N repaired books |
| `--batch-size N` | 250 | Commit every N repaired books |
| `--verbose` | off | Print one line per repaired book |
| `--dry-run` | off | Preview repairs without writing |
| `--db-path PATH` | auto | Override the SQLite database path |

The script skips books that have no `/ebooks/` or `/files/` download links, and books whose RDF file is not in the local cache. Run `metadata.py` first to populate the RDF cache if needed.

### Short-chapter cleanup

Use `AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py` to remove bad catalog entries where a chaptered book has every interior chapter shorter than 10 non-empty lines. The first and last chapter blocks are ignored, so front matter and closing notes do not trigger deletion by themselves. The script deletes the book row and its dependent catalog rows from the SQLite database.

```bash
# Preview all deletions without writing
python AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py --dry-run

# Remove only a single book by internal id
python AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py --book-id 22025

# Delete the first 50 matches and print every scanned row
python AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py --limit 50 --verbose
```

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--book-id N` | — | Scan only the given internal `books.id` (repeatable) |
| `--min-lines N` | 10 | Minimum non-empty lines required for interior chapters |
| `--limit N` | — | Stop after deleting N books |
| `--verbose` | off | Print per-book scan output |
| `--dry-run` | off | Preview deletions without writing |
| `--db-path PATH` | auto | Override the SQLite database path |

The script uses the same chapter heading detection as the chapter-splitting test harness, so it only deletes books that actually split into 3 or more chapter blocks and have all middle chapters below the line threshold.

### Sensitive-topic cleanup

Use `AudioBooks/Catalog/Gutenberg/repair_sensitive_topic_books.py` to remove catalog entries that match explicit topic filters for erotic, gory violence, psychic, Mahabharata, intent to glorify violence or occult worship, or Bible-attack / anti-Bible content. The script checks the book title, subjects, and description for the general topic rules, and also scans the text for the Bible-related and intent-based violence/occult rules, then deletes only when one of the filters matches.

The violence rule is intentionally narrow: it is meant for text that reads like explicit gore, torture, mutilation, dismemberment, or `Saw`-style graphic content. A classic tragedy such as `Macbeth` should not match just because it contains murder or violent themes.

The occult and violence-glorification rules are intent-based. They are meant for passages where praise, worship, celebration, or endorsement appears in the same context as occult or violent themes, not for ordinary mentions of horror, tragedy, or religion.

```bash
# Preview all deletions without writing
python AudioBooks/Catalog/Gutenberg/repair_sensitive_topic_books.py --dry-run

# Remove only a single book by internal id
python AudioBooks/Catalog/Gutenberg/repair_sensitive_topic_books.py --book-id 22025

# Delete the first 50 matches and print every scanned row
python AudioBooks/Catalog/Gutenberg/repair_sensitive_topic_books.py --limit 50 --verbose
```

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--book-id N` | — | Scan only the given internal `books.id` (repeatable) |
| `--limit N` | — | Stop after deleting N books |
| `--verbose` | off | Print per-book scan output |
| `--dry-run` | off | Preview deletions without writing |
| `--db-path PATH` | auto | Override the SQLite database path |

This cleanup is intentionally keyword-based rather than semantic. If you need a narrower or broader policy, adjust the rule list in the script before running it on the live database.

### Merging duplicate catalog entries

Use `AudioBooks/Catalog/Gutenberg/merge_books.py` when two or more `books` rows represent the same work. The script can detect duplicates automatically across the whole catalog, or merge an explicit pair.

```bash
# List all duplicate groups without writing anything
python AudioBooks/Catalog/Gutenberg/merge_books.py --find-duplicates

# Preview all auto-detected merges
python AudioBooks/Catalog/Gutenberg/merge_books.py --auto-merge --dry-run

# Execute all auto-detected merges
python AudioBooks/Catalog/Gutenberg/merge_books.py --auto-merge

# Merge only the first 20 groups
python AudioBooks/Catalog/Gutenberg/merge_books.py --auto-merge --limit 20

# Merge one explicit pair (source is deleted, target is kept)
python AudioBooks/Catalog/Gutenberg/merge_books.py --source 43478 --target 22025 --dry-run
python AudioBooks/Catalog/Gutenberg/merge_books.py --source 43478 --target 22025
```

#### Duplicate detection

Books are grouped by **normalized title + author token set**. Titles are lowercased, punctuation-stripped, and common stop-words removed; numeric tokens (volume/part numbers) are retained so "Vol. 1" and "Vol. 2" produce different keys and are not merged. A group with 2+ books is a merge candidate.

#### Target selection (auto-merge)

Within each group, each book is scored and the highest-scored book becomes the canonical target:

```
score = content_bytes + 1_000_000 × has_real_desc + 500_000 × has_audio + numdownloads
```

All other books in the group become sources and are merged into the target one at a time.

#### What gets migrated (source → target)

| Data | Behaviour |
|------|-----------|
| `book_desc` | Copied if target has none, or if target only has a catalog placeholder and source has a real summary |
| `book_contents` | Copied (or replaced) when source content is larger than target |
| `book_audio` + `book_audio_chapters` | Copied from source's `book_audio` table first; falls back to building from source `downloadlinks` MP3 entries |
| Target's own downloadlinks | If the target has MP3 `downloadlinks` but no `book_audio` row after migration, its audio is populated from its own links |
| `book_cover_art` | Keyed on Gutenberg ID — no migration needed |

#### What gets deleted (source only)

Custom tables: `book_desc`, `book_contents`, `book_audio`, `book_audio_chapters`  
Core catalog: `downloadlinks`, `book_subjects`, `book_authors`, `titles`, `books`

After running, restart Flask to clear in-process caches.

#### Flags

| Flag | Description |
|------|-------------|
| `--find-duplicates` | Detect and list duplicate groups without merging |
| `--auto-merge` | Detect and merge all duplicate groups automatically |
| `--source N` | Internal `books.id` to merge from (will be deleted) |
| `--target N` | Internal `books.id` to merge into (canonical, kept) |
| `--dry-run` | Preview changes without writing to SQLite |
| `--limit N` | Max duplicate groups to process (--auto-merge only) |

### Audio catalog backfill

Use `AudioBooks/Catalog/Gutenberg/backfill_audio.py` to populate the audio tables from Gutenberg download links, repair stale URLs from the live file index, enrich narrator and chapter metadata from readme files, and fill gaps using LibriVox:

```bash
# Basic runs
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --dry-run --gutenberg-id 9036
python AudioBooks/Catalog/Gutenberg/backfill_audio.py
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --mirror-tries 2
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --repair-all --workers 4
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --chunk-size 64 --workers 4

# LibriVox gap-filling (books with no Gutenberg audio or machine-read audio)
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --fill-librivox
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --fill-librivox --dry-run

# Skip readme fetching (faster, no narrator/chapter enrichment)
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --skip-readme

# Post-process only: enrich existing audio rows with narrator/chapter metadata from readme
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --enrich-readme
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --enrich-readme --dry-run
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --enrich-readme --limit 50
```

#### Audio schema

- `book_audio` — book-level audio package with narrator metadata
- `book_audio_chapters` — one row per track with title and duration

| Column | Table | Description |
|--------|-------|-------------|
| `package_url` | `book_audio` | Primary download URL (zip or first track) |
| `audio_format` | `book_audio` | MIME type e.g. `audio/mpeg` |
| `narrator` | `book_audio` | Human narrator name, or `null` if unknown/synthesized |
| `narrator_source` | `book_audio` | `readme` · `librivox` · `synthesized` · `null` |
| `is_synthesized` | `book_audio` | `1` for machine-read (MIT TTS) audio |
| `chapter_title` | `book_audio_chapters` | Chapter or track title from the readme |
| `duration` | `book_audio_chapters` | Track duration as `HH:MM:SS` |

Single-file recordings produce one `book_audio` row with no chapter rows. Chaptered audiobooks also get one row per track in `book_audio_chapters`. All rows are keyed by internal `books.id`.

#### Narrator and chapter metadata

After resolving audio URLs the script fetches each book's `readme.txt` from Gutenberg (e.g. `files/22950/22950-readme.txt`). LibriVox readme files follow a standard format:

```
This audio reading of … is read by

Narrator Name

# Chapter 01 - 00:17:43
# Chapter 02 - 00:19:36
```

The narrator name and per-chapter title + duration are extracted and stored alongside the audio rows. Pass `--skip-readme` to disable this step.

Machine-read audio (Gutenberg MIT TTS, path pattern `/{id}-m/`) is detected automatically and marked `is_synthesized = 1`. The narrator is left `null` for these books.

#### LibriVox gap-filling (`--fill-librivox`)

Run after the normal backfill to:

1. **Fill gaps** — books with no `book_audio` row are searched on the LibriVox API (`/api/feed/audiobooks?gutenberg_id=…` then title search as fallback). Found books are imported with their sections (track URLs, chapter titles, durations) and reader names.
2. **Replace synthesized audio** — books marked `is_synthesized = 1` are also searched on LibriVox. If a human narration is found it replaces the machine-read rows.

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | off | Print what would be written without touching SQLite |
| `--repair-all` | off | Prefer live Gutenberg file index for every target; delete stale rows |
| `--fill-librivox` | off | Search LibriVox for gaps and synthesized-audio replacements |
| `--skip-readme` | off | Skip readme.txt fetching (narrator/chapter enrichment) |
| `--enrich-readme` | off | Post-process only: update existing rows with narrator/chapter metadata; skip URL resolution |
| `--gutenberg-id N` | — | Process only the specified Gutenberg id (repeatable) |
| `--limit N` | — | Stop after N books in the main Gutenberg pass (or N books in `--enrich-readme` pass) |
| `--chunk-size N` | 64 | Books per batch commit |
| `--workers N` | 4 | Parallel URL resolution workers |
| `--mirror-tries N` | 3 | Mirror hosts to try per file before falling back to source |

#### Practical run combinations

| Command | Effect |
|---------|--------|
| `backfill_audio.py` | Refresh audio rows from local catalog metadata + readme enrichment |
| `backfill_audio.py --repair-all` | Rewrite from live Gutenberg indexes; clear stale rows |
| `backfill_audio.py --fill-librivox` | Normal pass + LibriVox gap-fill and synthesized-audio replacement |
| `backfill_audio.py --repair-all --fill-librivox` | Full repair pass then LibriVox fill |
| `backfill_audio.py --skip-readme` | URL-only pass, no narrator or chapter enrichment |
| `backfill_audio.py --enrich-readme` | Narrator/chapter enrichment only; skip URL resolution entirely |
| `backfill_audio.py --enrich-readme --limit N` | Enrich only the first N eligible books |
| `backfill_audio.py --dry-run` | Preview changes without writing |

### Cover art backfill

Use `AudioBooks/Catalog/Gutenberg/backfill_cover_art.py` to populate `book_cover_art` from the Gutenberg RDF cache. Cover images are extracted from each book's RDF file and stored with their size label (`small`, `medium`, etc.) and direct URL.

```bash
python AudioBooks/Catalog/Gutenberg/backfill_cover_art.py --dry-run --limit 10
python AudioBooks/Catalog/Gutenberg/backfill_cover_art.py --no-verify
python AudioBooks/Catalog/Gutenberg/backfill_cover_art.py --refresh-live
python AudioBooks/Catalog/Gutenberg/backfill_cover_art.py --gutenberg-id 1342
```

| Flag | Description |
|------|-------------|
| `--dry-run` | Preview without writing |
| `--refresh-live` | Fetch the live Gutenberg RDF even when a local cache exists |
| `--no-verify` | Disable SSL certificate verification |
| `--gutenberg-id N` | Process only the specified id (repeatable) |
| `--limit N` | Stop after N books |
| `--ca-bundle PATH` | Path to a PEM CA bundle |
| `--ca-dir PATH` | Path to a directory of CA certificates |

### Book summaries backfill

Use `AudioBooks/Catalog/Gutenberg/backfill_book_desc.py` to import plot summaries from the CMU Book Summary Dataset into `book_desc`:

```bash
python AudioBooks/Catalog/Gutenberg/backfill_book_desc.py --dry-run --limit 20
python AudioBooks/Catalog/Gutenberg/backfill_book_desc.py
python AudioBooks/Catalog/Gutenberg/backfill_book_desc.py --tarball-path /path/to/booksummaries.tar.gz
```

The importer downloads `booksummaries.tar.gz` from the CMU dataset page by default, parses the tab-separated `booksummaries.txt` file inside the archive, and stores:

- `wikipedia_id`
- `freebase_id`
- `source_title`
- `source_author`
- `publication_date`
- `genres_text`
- `genres_json`
- `summary`

Rows are matched to existing catalog books by normalized title and author, then written to `book_desc.bookid`.
