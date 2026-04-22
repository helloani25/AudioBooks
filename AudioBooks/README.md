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
`--chunk-size` controls how many Gutenberg ids are resolved per SQL batch before downloading starts.
The backfill extractor now also reads `.tex` files inside archives, and it can fall back to PDFs when `pypdf` is installed, which helps recover technical books that do not ship plain text.
XML downloadlinks are no longer treated as a content fallback. If Project Gutenberg returns a folio warning like `see #892 for HTML format, #733 for plain text`, the backfill code treats it as a failure for now. The known folio-only case is Gutenberg id `900`.

Preflight mode classifies each missing book before downloads start:
- `repair`: local cache rows look stale, so the script prefers the live Gutenberg file index.
- `refresh`: the local cache is missing usable rows, so the script also prefers the live Gutenberg file index.
- `audio-only`: the live index only exposes audio/video files, so the book is skipped for text backfill.
- `skip`: the local cache already has usable text candidates.

### Refreshing the Gutenberg download cache

If the local `downloadlinks` cache is missing file variants, run the metadata refresh mode in `AudioBooks/Catalog/Gutenberg/metadata.py`:

```bash
python AudioBooks/Catalog/Gutenberg/metadata.py --refresh-downloadlinks
python AudioBooks/Catalog/Gutenberg/metadata.py --refresh-downloadlinks --limit 100
python AudioBooks/Catalog/Gutenberg/metadata.py --refresh-downloadlinks --workers 16
python AudioBooks/Catalog/Gutenberg/metadata.py --repair-downloadlinks --gutenberg-id 57477
python AudioBooks/Catalog/Gutenberg/metadata.py --refresh-downloadlinks --gutenberg-id 900
```

Arguments:
- `--refresh-downloadlinks`: scrape `https://www.gutenberg.org/files/<id>/` pages and backfill new rows into `downloadlinks`.
- `--repair-downloadlinks`: delete and rebuild `downloadlinks` rows for the selected Gutenberg ids.
- `--gutenberg-id <id>`: limit refresh/repair to one or more specific Gutenberg ids.
- `--workers <n>`: number of concurrent file-index fetches.
- `--limit <n>`: scan only the first `n` Gutenberg books, which is useful for testing.

This mode updates the local cache in SQLite so later runs of `backfill_missing_book_contents.py` can see more formats without cloning another repository.

### Audio catalog backfill

Use `AudioBooks/Catalog/Gutenberg/backfill_audio.py` to populate the audio tables from the existing Gutenberg download links:

```bash
python AudioBooks/Catalog/Gutenberg/backfill_audio.py --dry-run --gutenberg-id 9036
python AudioBooks/Catalog/Gutenberg/backfill_audio.py
```

The audio schema is normalized as:

- `book_audio` for the book-level audio package
- `book_audio_chapters` for chapter or track-level audio URLs

Single-file recordings stay as one `book_audio` row. Chaptered audiobooks also get one row per track in `book_audio_chapters`.

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
