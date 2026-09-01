# AudioBooks
A modular system for browsing, reading, listening to, and presenting summarized Project Gutenberg books.

## Modules
- **[Authentication](./src/AudioBooks/Authentication)**: User signup, login, and session management.
- **[Catalog](./src/AudioBooks/Catalog)**: Gutenberg metadata ingestion and API for book search/filtering.
- **[Presentation](./src/AudioBooks/Presentation)**: Vite frontend for the user interface.
- **[BookSummary](./src/AudioBooks/BookSummary)**: AI summarization pipeline — generates chapter-by-chapter summaries and character profiles via Hugging Face or an OpenAI-compatible RunPod endpoint.

## Getting Started

1. **Create an isolated environment and install the web/catalog dependencies**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   python -m pip install -e . --no-deps
   npm --prefix src/AudioBooks/Presentation/library ci
   ```

   To run the local or hosted summarization pipelines too, install the optional
   ML stack instead:
   ```bash
   python -m pip install -r requirements-summarization.txt
   ```

   For the local Jupyter notebooks, install the notebook tools as well:
   ```bash
   python -m pip install -r requirements-notebooks.txt
   ```

2. **Create the local environment file and set a random `SECRET_KEY`**:
   Flask uses this key to sign session data and CSRF tokens. A predictable key allows cookie/token forgery, and changing the key invalidates existing sessions. Use a long random value per environment.
   ```bash
   cp .env.example src/AudioBooks/.env
   python -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```
   Paste the generated value after `SECRET_KEY=` in `src/AudioBooks/.env`.

3. **Start the Backend**:
   Run from the project root:
   ```bash
   .venv/bin/python -m AudioBooks.app
   ```

4. **Start the Frontend**:
   ```bash
   npm --prefix src/AudioBooks/Presentation/library run dev
   ```

The backend listens on `http://localhost:5001`; Vite listens on
`http://localhost:5173` and proxies `/api` requests to Flask.

5. **Optional Redis**:
   Redis is not required for local development. Without it, Flask sessions use
   `src/AudioBooks/flask_session/` and catalog caching stays in-process. To use
   Redis on macOS with MacPorts:
   ```bash
   sudo port install redis
   redis-server
   ```

## Terraform GPU Instance and Quota Troubleshooting

The Terraform configuration in `src/AudioBooks/Terraform/provider.tf` creates one
G2 VM for local Qwen summarization:

- `google_compute_instance.qwen_worker`: the VM instance
- `google_compute_disk.qwen_boot_disk`: the persistent boot disk attached to the VM

If `terraform plan` says `2 to add`, that does not mean two VM instances. It
means Terraform will create one VM plus its boot disk. If the boot disk already
exists in Terraform state, the expected plan is:

```text
Plan: 1 to add, 0 to change, 0 to destroy.
```

### Hugging Face token

Terraform needs the Hugging Face API token for the startup script. The safest
CLI form is:

```bash
export TF_VAR_hf_api_token="$HF_API_TOKEN"
terraform -chdir=src/AudioBooks/Terraform plan
terraform -chdir=src/AudioBooks/Terraform apply
```

### Check L4 accelerator availability

First verify that NVIDIA L4 is available in the configured region and zone.
The current Terraform defaults are `northamerica-northeast2` and
`northamerica-northeast2-a`.

```bash
gcloud compute accelerator-types list \
  --filter="name~'^nvidia-l4$' AND zone~'northamerica-northeast2'" \
  --format="table(name,zone.basename():label=ZONE,description)"
```

Expected output should include the configured zone:

```text
NAME       ZONE                       DESCRIPTION
nvidia-l4  northamerica-northeast2-a  NVIDIA L4
```

### Check regional L4 quota

Regional L4 quota must be at least `1` for one `g2-standard-*` instance:

```bash
gcloud compute regions describe northamerica-northeast2 \
  --format="table(quotas.filter('metric~NVIDIA_L4_GPUS'):format='table(metric,limit,usage)')"
```

For a spot G2 instance, these are the relevant rows:

```text
METRIC                      LIMIT  USAGE
NVIDIA_L4_GPUS              1.0    0.0
PREEMPTIBLE_NVIDIA_L4_GPUS  1.0    0.0
COMMITTED_NVIDIA_L4_GPUS    1.0    0.0
```

### Check project-wide GPU quota

Even when regional L4 quota is available, Google Cloud also enforces the
project-wide GPU quota `GPUS_ALL_REGIONS`. If this is `0`, any GPU VM creation
fails.

```bash
gcloud compute project-info describe \
  --format="table(quotas.filter('metric=GPUS_ALL_REGIONS'):format='table(metric,limit,usage)')"
```

If the output is:

```text
METRIC            LIMIT  USAGE
GPUS_ALL_REGIONS  0.0    0.0
```

then Terraform apply will fail with:

```text
Quota 'GPUS_ALL_REGIONS' exceeded. Limit: 0.0 globally.
metric name = compute.googleapis.com/gpus_all_regions
limit name = GPUS-ALL-REGIONS-per-project
dimensions = map[global:global]
```

This is not a Terraform bug. Request a quota increase in Google Cloud Console:

- Go to **IAM & Admin -> Quotas & System Limits**
- Filter for `GPUS_ALL_REGIONS`
- Select the Compute Engine API quota
- Request `GPUS-ALL-REGIONS-per-project` with a value of `1`

For one L4 VM, both quotas must allow at least one GPU:

```text
GPUS_ALL_REGIONS >= 1
NVIDIA_L4_GPUS in northamerica-northeast2 >= 1
```

After the quota increase is approved, rerun:

```bash
terraform -chdir=src/AudioBooks/Terraform apply
```

## Redis Caching and Sessions
The system uses Redis for both sessions and catalog metadata caching if available. 
- **Sessions**: If Redis is unavailable, it falls back to the local filesystem (`flask_session` folder).
- **Catalog Caching**: If Redis is unavailable, it falls back to in-memory caching for the current process.

To use Redis, ensure a Redis server is running on `localhost:6379` or set the `REDIS_URL` environment variable:
```bash
export REDIS_URL="redis://your-redis-host:6379"
python3 src/AudioBooks/app.py
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


## Catalog Search — Hybrid Search Design

> **Status: design, not implemented.** The sections below describe the target hybrid search (title + author + description) and the migration path to get there. Current behaviour is described under "Where it stands today".

Search should match on three fields with a strict priority order:

1. **Title** — exact match first, then variant matches (`The Tale of 2 Cities` ≡ `A Tale of Two Cities`).
2. **Author** — normalized and fuzzy, tolerating initials, punctuation, and misspellings (`JK Cator` → `John K Cantor`).
3. **Description / plot** — last, and semantic: `French Revolution` should surface *A Tale of Two Cities*.

### Where it stands today

`CatalogRepository.get_books()` tokenizes the query (`_tokenize_search`: lowercase, split on non-word chars, digit→word expansion, stop-word removal) and runs `LOWER(name) LIKE '%token%'` against `titles` and `authors` only. Ranking is the count of per-token title matches, then `numdownloads`. Descriptions are never searched, author variants never match, and `LIKE '%…%'` cannot use an index.

### Data shape

Measured against `Catalog/DB/gutenbergindex.db`:

| Table | Rows | Notes |
|-------|------|-------|
| `books` | 76,879 | |
| `titles` | 82,090 | includes language-suffixed variants (`A tale of two cities. Finnish`) |
| `authors` | 56,014 | many near-duplicate rows for the same person |
| `book_authors` | 200,995 | |
| `subjects` / `book_subjects` | 42,137 / 258,459 | LCSH terms — the richest genre signal available |
| `book_desc` | 74,880 | `summary` averages 1,252 chars — **92.9 MB total** |
| `book_contents` | 76,879 | raw + clean + html text — **82.4 GB of the 88 GB file** |

The two numbers that drive every decision below: the entire searchable corpus is **under 100 MB**, and the content blobs are **94% of the database**.

### Store decision: Postgres, not Weaviate

At 76k books — one vector per summary, or ~1.5M if chapter-level — the corpus is small. Weaviate's real advantages (managed ANN at 100M+ scale, built-in BM25 + vector fusion) do not pay for themselves at this size, and it costs the thing this ranking needs most: **the ranking is relational**. Author matching joins `book_authors` → `authors`; faceting uses `book_subjects`; results filter on `book_audio` existence and sort by `numdownloads`. In Weaviate all of that becomes denormalized properties re-indexed on every change, plus a second store to keep in sync — and its BM25 still would not match `JK Cator` to `John K Cantor`, so that normalizer has to be written either way.

Postgres covers every tier in one query and one transaction:

| Signal | Extension |
|--------|-----------|
| Lexical title / summary | `tsvector` + GIN |
| Fuzzy title / author | `pg_trgm` |
| Accent and spelling variants | `unaccent`, `fuzzystrmatch` (levenshtein) |
| Plot semantics | `pgvector` |

**Only the search slice migrates.** `books`, `titles`, `authors`, `book_authors`, `book_subjects`, `subjects`, `book_desc`, `book_cover_art` — under 500 MB. `book_contents` stays in SQLite or moves to GCS with URL pointers, which is worth doing regardless of search: an 88 GB single-file DB is already a liability for backups and deploys, and splitting it makes the migration nearly free.

**Staying on SQLite** is viable at this scale too — FTS5 (with the trigram tokenizer) plus `sqlite-vec` covers all tiers, and brute-force cosine over 75k vectors is only ~30 ms. It gives up `pg_trgm` similarity ranking, `unaccent`, and indexed ANN. Phase 0 below takes this path deliberately, to ship ranking improvements before committing to a migration.

### Ranking: bands, not a blended score

Because the priority order is strict, a weighted-sum or RRF fusion at the top level is wrong — either lets a strong description match outrank a weak title match. Use integer bands, with within-band tiebreaking:

```
1000  title_norm exact                     "tale of two cities"
 900  title_key match (order/digit-blind)  "The Tale of 2 Cities" ≡ "A Tale of Two Cities"
 800  title fuzzy (trigram ≥ 0.55, or all tokens AND-match in FTS)
 700  author match      × author_score (0..1)
 600  subject / genre lexical
 500  summary lexical (tsvector — exact phrase "French Revolution")
 400  summary semantic  × cosine
```

`final = band + within_band * 50 + log(numdownloads) * 10`. Each signal is a CTE, `UNION ALL`, then `GROUP BY bookid` taking `MAX(score)` — one round trip.

**Short-circuit:** if band 1000 or 900 hits, skip the query embedding entirely. Saves the 5–20 ms embed on the common case.

### Title normalization

A generated `title_norm` column, b-tree indexed, makes band 1000 a single index lookup:

```
unaccent → lowercase
  → strip trailing language suffix   (". Finnish", ". German", ". Spanish")
  → drop leading article             ("the", "a", "an")
  → expand digits and roman numerals ("2" → "two", "iii" → "three")
  → strip punctuation and apostrophes
  → collapse whitespace
```

`title_key` is the same token stream with stop-words removed, sorted, and rejoined. That turns `The Tale of 2 Cities` ≡ `Tale of two Cities` into an *equality* test rather than a fuzzy one — cheap and deterministic. Titles are also split on `:` so the pre-subtitle form is indexed separately.

### Author matching

The author table is the messiest input. Real rows:

```
Twain, Mark                     Zola, Émile
Twain, Mark (Samuel Clemens)    Zola, Emile
Clemens, Samuel Langhorne       Zola, Émile Édouard Charles Antoine
Dickens, Charles                Fenn, G. M. (George Manville)
Dickens, Charles John Huffam
```

Two separate jobs:

**1. Canonicalize offline.** Cluster the 56,014 rows into `author_canonical` + `author_alias`. The parenthetical is a free alias mapping (`Twain, Mark (Samuel Clemens)` → both names, one person); accent variants collapse under `unaccent`; `Charles Dickens` / `Charles John Huffam Dickens` collapse on surname + first given name. A hit on any alias then returns the union of that person's books — today a search for `Zola` returns three disjoint sets.

**2. Match at query time — candidate-generate, then rerank.** Levenshtein across 56k rows per query is not viable. Use a GIN trigram index on the surname column for candidate generation, then rescore the candidates:

```
# "JK Cator"       -> surname "cator",  givens ["j", "k"]     (glued initials split)
# "Cantor, John K" -> surname "cantor", givens ["john", "k"]
surname_score = 1.0 if equal else max(trigram_sim, 1 - lev / len)   # cator/cantor: lev=1 -> 0.83
given_score   = fraction of query givens that are initial-compatible  # j~john ✓, k~k ✓ -> 1.0
author_score  = surname_score * (0.6 + 0.4 * given_score)
```

Surname carries the weight and initials only modulate it, because `JK` vs `John K` is a formatting difference rather than a different person. Reject below ~0.6.

Note: `dmetaphone` is the wrong tool for this case — `Cator` and `Cantor` hash to `KTR` and `KNTR`. Edit distance catches it, phonetic hashing does not.

### Description / plot tier

Embed `book_desc.summary` — at ~1,252 chars (≈300 tokens) it is one vector per book, no chunking needed. `bge-small-en-v1.5` at 384 dims is sufficient; 75k × 384 × 4 B ≈ 115 MB, trivial for HNSW. Run it on the GPU instance already provisioned in `Terraform/` — 93 MB of text is roughly 25M tokens, a single pass rather than a recurring cost.

Chapter summaries (see the AI Summarization Pipeline section; 3,623 books currently in `BookSummary/Artifacts/summary_results_local.jsonl`) become a second, higher-recall index later: embed per chapter, aggregate to book with `MAX(chapter_score)` plus a small bonus when several chapters hit. That is what finds `French Revolution` → *A Tale of Two Cities* through the actual Bastille chapters rather than the blurb.

**Genre gating caveat.** `book_desc.genres_text` is empty in practice, and `category` has only four values:

| Category | Books |
|----------|-------|
| `dramatic` | 48,563 |
| `biographical` | 15,906 |
| `analytical` | 8,612 |
| `practical` | 1,799 |

Too coarse to separate "novel about the Revolution" from "history of the Revolution." The `subjects` table (42,137 LCSH terms across 258,459 links) is the real signal — derive a fiction/non-fiction flag and genre facets from it, and use `category` only as a weak boost.

### Phasing

| Phase | Work | Result |
|-------|------|--------|
| **0** | On SQLite: add `title_norm` / `title_key` columns, the author canonicalization table, FTS5 over titles + authors + summaries | Bands 1000–500, no new infrastructure, no migration risk |
| **1** | Stand up Postgres with the metadata slice; blobs to GCS; port bands to SQL with `pg_trgm` + `tsvector` | Same ranking, indexed and scalable |
| **2** | `pgvector` over `book_desc` summaries | Band 400 — semantic plot search |
| **3** | Chapter-level embeddings as the summarizer backfills | Higher recall on plot queries |

Phase 0 is useful on its own and is the only phase with no migration risk. `_tokenize_search` in `CatalogRepository` already does digit→word expansion and stop-word removal, so it is the right starting point for the normalizers.

---

## Background — How the Postgres Search Pieces Work

> Reference notes for the design above. **Nothing here is provisioned yet** — the AudioBooks catalog has not been set up in Postgres and `pgvector` is not installed. The `to_tsvector` outputs below were verified against a local PostgreSQL 15 install; the SQL was syntax-checked against the real column names with a throwaway schema.

Full-text search in Postgres is the pairing of two things: the `tsvector` type, which converts text into normalized words (lexemes), and a **GIN** (Generalized Inverted Index), which maps each lexeme to the rows containing it. Together they avoid the full-table scan that `LIKE '%…%'` forces today.

A GIN index works like the index at the back of a textbook. A normal B-tree index maps a row to its value; an inverted index maps a value — the word — to every row that contains it. During the search phase Postgres never reads the text column at all. It looks words up in a sorted structure, intersects the resulting row-ID lists, and fetches only the rows that already have a proven match.

Setup is three steps:

1. **Create a generated column** (Postgres 12+) — handles tokenization, weighting, and updates on `INSERT`/`UPDATE` without triggers.
2. **Define the GIN index** on that column.
3. **Query** with the match operator `@@` against `to_tsquery`.

### `tsvector` — turning text into lexemes

Before anything is indexed, `tsvector` reduces raw text through three stages:

- **Tokenization** — splits the string into words, punctuation, and spaces.
- **Normalization (stemming)** — lowercases and strips suffixes, so *running*, *runs*, and *ran* all collapse to the root `run`.
- **Stop-word removal** — discards high-frequency words like *the*, *a*, and *and*.

```sql
SELECT to_tsvector('english', 'The quick brown foxes are jumping!');
-- 'brown':3 'fox':4 'jump':6 'quick':2
```

*The* and *are* are gone, `foxes` stemmed to `fox`, `jumping` to `jump`. The integers are word positions in the original string — which is what makes phrase search and proximity ranking possible later.

### GIN — the inverted index

Internally GIN splits the `tsvector` data into three parts:

| Structure | What it holds |
|-----------|---------------|
| **Entry tree** | A sorted B-tree of every unique lexeme across the whole table |
| **Posting list** | The row IDs (TIDs) stored alongside each lexeme |
| **Posting tree** | For a lexeme appearing in thousands of rows, the flat list is promoted to its own B-tree to keep lookups fast |

Three rows, and what they actually stem to:

```
Row 1  "Database optimization tips"      ->  'databas':1 'optim':2 'tip':3
Row 2  "Postgres database performance"   ->  'databas':2 'perform':3 'postgr':1
Row 3  "Performance tips and tricks"     ->  'perform':1 'tip':2 'trick':4
```

Note that the stemmer is more aggressive than it looks: `database` → `databas` and `postgres` → `postgr`. You index and query the stem, never the surface word. (`trick` sits at position 4 because the stop-word *and* still consumes position 3.)

GIN stores that inverted:

| Lexeme | Posting list |
|--------|--------------|
| `databas` | Row 1, Row 2 |
| `optim` | Row 1 |
| `perform` | Row 2, Row 3 |
| `postgr` | Row 2 |
| `tip` | Row 1, Row 3 |
| `trick` | Row 3 |

### How a query executes

`to_tsquery('english', 'database & performance')` normalizes to `'databas' & 'perform'`, then:

1. Look up `databas` → Row 1, Row 2
2. Look up `perform` → Row 2, Row 3
3. Apply `&` (AND) → intersect the two lists
4. Only Row 2 survives; Postgres fetches that single row from disk

Verified against a live server — Row 2 matches, Rows 1 and 3 do not.

### The trade-off: fast reads, slower writes

Mapping every word in a document to a row list means writes are expensive: inserting one 500-word document touches 500 places in the index.

Postgres mitigates this with `fastupdate = on` (the default for GIN). New entries are appended to an unsorted **pending list** instead of being merged immediately. When that list fills (`gin_pending_list_limit`, 4 MB by default) or `VACUUM` runs, the entries are flushed into the main structure in one batch.

For this catalog that trade lands well — the corpus is written once by backfill jobs and then read constantly.

### `pgvector` — matching concepts, not words

`pgvector` does not look at words at all; it compares the *mathematical position* of text. An embedding model places related concepts near each other, so *grief* and *loss* sit close to *funeral*, *mourning*, and *heartbreak*.

Given a summary like:

> "After his mother passed away, a young boy struggles to navigate the silent, empty house and the overwhelming sadness of her sudden absence."

- **Keyword search fails completely** — neither "grief" nor "loss" appears in the text.
- **Semantic search succeeds** — "passed away", "empty house", and "overwhelming sadness" put the vector in the right neighbourhood.

The `<=>` operator computes cosine distance between the query vector and each stored vector, pushing the closest rows to the top. This is exactly the behaviour band 400 needs: *French Revolution* → *A Tale of Two Cities*.

The reverse failure matters too. A pure vector search can suffer **semantic wash** — compressing a whole summary into one vector can bury a short exact phrase under the surrounding context. If "grief and loss" is literally in the title, `tsvector` catches it decisively and the vector search may not. Neither method alone is sufficient, which is the entire argument for hybrid.

### Reciprocal Rank Fusion (RRF)

Keyword search returns a relevance score; vector search returns a distance. The two are not on a comparable scale, so they cannot simply be added. RRF sidesteps this by discarding raw scores and using only each document's **rank** in each list:

```
RRF score = 1 / (60 + rank_keyword) + 1 / (60 + rank_vector)
```

The constant 60 is the standard damping parameter — it stops a #1 finish in one list from completely swamping a mid-table finish in the other.

Searching "grief and loss":

| | Keyword rank | Vector rank | RRF score |
|---|---|---|---|
| **Book A** — phrase is in the title, summary reads dry | 1 | 25 | `1/61 + 1/85` = **0.02816** |
| **Book B** — emotional novel about a funeral, never uses the words | not found | 1 | `0 + 1/61` = **0.01639** |

Book A rises to the top for scoring in both paradigms, while Book B — invisible to keyword search entirely — still lands right behind it.

**How this relates to the banded design above.** RRF is the standard fusion method and is what Weaviate provides natively, but it deliberately blends signals, which is precisely what the priority requirement here forbids: a strong description match must never outrank a weak title match. So the top-level ranking uses fixed bands instead. RRF remains the right tool *within* the semantic tier — fusing summary-lexical and summary-vector results into a single band-400/500 ordering — or as a replacement for the whole scheme if the strict ordering requirement is ever relaxed.

### Hybrid query in one SQL statement

Both retrievals as CTEs, fused by RRF, with relational filtering applied in the same statement. Written against the real catalog schema (`titles.bookid`, `book_authors.bookid`/`authorid`, `book_audio.book_id`); `$1` is the query embedding:

```sql
WITH keyword_search AS (
    SELECT d.bookid,
           ROW_NUMBER() OVER (ORDER BY ts_rank(d.search_vector, q.query) DESC) AS rank
    FROM book_desc d, to_tsquery('english', 'grief & loss') AS q(query)
    WHERE d.search_vector @@ q.query
    LIMIT 100
),
vector_search AS (
    SELECT d.bookid,
           ROW_NUMBER() OVER (ORDER BY d.embedding <=> $1) AS rank
    FROM book_desc d
    ORDER BY d.embedding <=> $1
    LIMIT 100
)
SELECT b.id,
       t.name AS title,
       a.name AS authors,
       b.numdownloads,
       COALESCE(1.0 / (60 + k.rank), 0.0) + COALESCE(1.0 / (60 + v.rank), 0.0) AS rrf_score
FROM keyword_search k
FULL OUTER JOIN vector_search v ON k.bookid = v.bookid
JOIN books b ON b.id = COALESCE(k.bookid, v.bookid)
LEFT JOIN LATERAL (
    SELECT name FROM titles WHERE bookid = b.id LIMIT 1
) t ON TRUE
LEFT JOIN LATERAL (
    SELECT string_agg(a2.name, ', ') AS name
    FROM book_authors ba JOIN authors a2 ON a2.id = ba.authorid
    WHERE ba.bookid = b.id
) a ON TRUE
WHERE EXISTS (SELECT 1 FROM book_audio au WHERE au.book_id = b.id)
ORDER BY rrf_score DESC, b.numdownloads DESC
LIMIT 20;
```

`FULL OUTER JOIN` is what lets a book appear via either retrieval alone — Book B above has no keyword rank at all, and `COALESCE` scores that side as zero rather than dropping the row.

The `WHERE EXISTS` clause is the point of the whole architecture: filtering to books that have audio is an ordinary relational predicate evaluated in the same query as the vector math, against live data, with no denormalization.

### When Weaviate would actually be the right call

Weaviate is a separate system with no knowledge of the Postgres tables, which has two consequences.

**It requires a re-indexing pipeline.** Updating a book title in Postgres does not update Weaviate. The application has to observe the change, re-embed the text, and issue an update API call. If that pipeline lags, the index silently serves stale results.

**It becomes a second store.** Weaviate cannot perform live relational joins, so any field used as a filter must be copied into it. `authors`, `subjects`, and `numdownloads` — clean separate tables in SQL — get flattened into every book object. The same author name is duplicated thousands of times purely to be filterable, and a name correction or download-count change must be pushed to both systems.

That cost buys real capability at the right scale:

| Weaviate is the right call when | Why |
|---|---|
| **Multi-modal search** | Searching cover images or audio clips from a text prompt |
| **10M–100M+ vectors** | Postgres struggles to hold that many high-dimensional vectors in RAM for HNSW traversal; Weaviate shards across nodes natively |
| **Workload isolation** | Heavy search traffic gets its own hardware instead of competing with ordinary SQL |
| **Managed pipelines** | Built-in RRF fusion and direct integration with embedding providers, no custom embedding code |

None of those apply here. At 76k books — or ~1.5M chapter vectors — the whole HNSW index fits comfortably in normal server RAM, and the hard part of this problem is relational: authors, subjects, formats, and download counts. Postgres handles relations natively and vectors well; Weaviate handles vectors natively and relations poorly. Staying in Postgres avoids two databases, a sync pipeline, and a denormalized copy of the catalog.

### Further reading

- [PostgreSQL — Tables and Indexes for Text Search](https://www.postgresql.org/docs/current/textsearch-tables.html) — official `tsvector`/GIN setup and behaviour
- [PostgreSQL — GIN Indexes](https://www.postgresql.org/docs/current/gin.html) — internals, `fastupdate`, and the pending list
- [PostgreSQL — Text Search Controls](https://www.postgresql.org/docs/current/textsearch-controls.html) — `to_tsquery`, phrase search, `ts_rank` weighting
- [pgvector](https://github.com/pgvector/pgvector) — installation, `<=>` operators, HNSW vs IVFFlat
- [Neon — Full-text search in Postgres](https://neon.com/postgresql/indexes/full-text-search) — practical walkthrough
- [Building hybrid search with pgvector, full-text search, and RRF](https://dev.to/lpossamai/building-hybrid-search-for-rag-combining-pgvector-and-full-text-search-with-reciprocal-rank-fusion-6nk) — close to the query above
- [OpenSearch — Introducing Reciprocal Rank Fusion](https://opensearch.org/blog/introducing-reciprocal-rank-fusion-hybrid-search/) — RRF rationale
- [ParadeDB — Reciprocal Rank Fusion](https://www.paradedb.com/learn/search-concepts/reciprocal-rank-fusion) — formula and constant explained
- [Weaviate docs — FAQ](https://docs.weaviate.io/weaviate/more-resources/faq) — denormalization and filtering constraints


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
    sqlite3 "/Users/anithas/PycharmProjects/SystemDesign/src/AudioBooks/Catalog/DB/gutenbergindex.db"
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
You run Gutenberg.py directly from src/AudioBooks/Catalog/Gutenberg
•
Python sets sys.path[0] to that script directory
•
The repo root, which contains src/AudioBooks/, is not on the import path
•
So Python cannot resolve the top-level package name AudioBooks
That means the problem is not the missing __init__.py. The real issue is execution context and import path resolution.


The clean fixes are:
•
Run it as a module from the project root:
python -m AudioBooks.Catalog.Gutenberg.Gutenberg
•
Or add `src` to PYTHONPATH
•
Or keep the bootstrap code I added earlier that inserts the repo root into sys.path when the script is run directly

```python
PROJECT_ROOT = BASE_DIR.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    # Allow direct execution of this file from the Gutenberg folder.
    sys.path.insert(0, str(PROJECT_ROOT))
```

### Importing the Hugging Face Gutenberg dataset

Use `src/AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py` to inspect the dataset and write text into `book_contents`.

Run from the project root:
```bash
python src/AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --print-columns
python src/AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py
python src/AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --split fr
python src/AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --all-splits
python src/AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --dry-run --limit 20
python src/AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --db-path src/AudioBooks/Catalog/DB/gutenbergindex.db
```

| Flag | Default | Description |
|------|---------|-------------|
| `--print-columns` | off | Show dataset features and available splits, then exit |
| `--split NAME` | `en` | Import one split only |
| `--all-splits` | off | Import every split in the dataset dictionary |
| `--force` | off | Overwrite existing `book_contents` rows |
| `--dry-run` | off | Match rows and count them without writing to SQLite |
| `--limit N` | — | Stop after N rows per split |
| `--db-path PATH` | auto | Override the SQLite database path |

The importer matches Hugging Face rows by `row["id"]` against `books.gutenbergbookid`, then stores the text in `book_contents.bookid`.

### Backfilling missing books from Project Gutenberg

To fetch books that are present in the local catalog but still missing from `book_contents`, run:

```bash
python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --dry-run --limit 5
python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py
python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --limit 20
python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --no-verify
python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --workers 16 --mirror-tries 2
python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --chunk-size 1000
python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --preflight
python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --preflight-only
python src/AudioBooks/Catalog/Gutenberg/backfill_book_contents_hf_dataset.py --db-path src/AudioBooks/Catalog/DB/gutenbergindex.db --all-splits
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

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--gutenberg-id N` | — | Target a specific Gutenberg id (repeatable) |
| `--force` | off | Overwrite existing `book_contents` rows for targeted ids |
| `--repair-all` | off | Scan every catalog book with a Gutenberg id and rewrite from live source |
| `--repair-mismatched-content` | off | Rewrite books whose stored payload advertises a different Gutenberg id |
| `--preflight` | off | Classify books as repair/refresh/skip/audio-only before downloading |
| `--preflight-only` | off | Print the classification plan and exit without downloading |
| `--dry-run` | off | Download and validate text in memory without writing to SQLite |
| `--reset-queue` | off | Discard saved queue state for the current namespace before starting |
| `--refresh-discovery-cache` | off | Rebuild live-index and candidate discovery instead of reusing cached data |
| `--workers N` | 8 | Number of concurrent download workers |
| `--max-attempts N` | 3 | Stop retrying a book after N consecutive failures |
| `--mirror-tries N` | 3 | Mirror hosts to try per book before falling back to the source URL |
| `--chunk-size N` | 500 | Books kept in flight per batch when resolving candidates and downloading |
| `--limit N` | — | Stop after processing N books |
| `--ca-bundle PATH` | — | Path to a PEM CA bundle file |
| `--ca-dir PATH` | — | Path to a directory of CA certificates |
| `--no-verify` | off | Disable SSL certificate verification |
| `--db-path PATH` | auto | Override the SQLite database path |

Run combinations:
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py`: default missing-content run; classifies each book as skip or download and uses cached local candidates first.
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --preflight`: classification-only pass; prints repair/refresh/skip/audio-only without writing book text.
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --preflight-only`: print the plan and exit before any download work starts.
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --repair-all`: full catalog sweep; uses live Gutenberg discovery and decides repair vs refresh per book.
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --gutenberg-id 39074`: targeted run for one book; uses the `targeted:v2:<hash>` namespace.
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --gutenberg-id 39074 --force`: overwrite the targeted row even if `book_contents` already exists.
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --repair-all --reset-queue`: rerun the full sweep from scratch for the `repair-all:v2` namespace.
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --repair-all --refresh-discovery-cache`: force live-index and candidate discovery to be rebuilt instead of reused.
- `python src/AudioBooks/Catalog/Gutenberg/backfill_missing_book_contents.py --dry-run`: download and validate text in memory, but do not write to SQLite.

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

Use `src/AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py` when the catalog metadata (title, author, Gutenberg ID, date) has drifted from what the book's actual download links point at. The script reads the Gutenberg ID embedded in each `downloadlinks` row, loads the matching RDF file from the local cache (`Catalog/DB/cache/epub/<id>/pg<id>.rdf`), and rewrites `books.gutenbergbookid`, `titles`, `book_authors`, and `books.dateissued` to match.

This is different from `backfill_missing_book_contents.py`, which fetches or refreshes **text content** — use `repair_catalog_from_downloadlinks.py` only when catalog metadata itself is wrong.

```bash
# Preview what would change without writing
python src/AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py --dry-run

# Repair a single book by internal id
python src/AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py --book-id 22025

# Repair all books whose download links point at Gutenberg id 98
python src/AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py --gutenberg-id 98

# Repair up to 500 books, committing every 100, with per-book output
python src/AudioBooks/Catalog/Gutenberg/repair_catalog_from_downloadlinks.py --limit 500 --batch-size 100 --verbose
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

Use `src/AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py` to remove bad catalog entries where a chaptered book has every interior chapter shorter than 10 non-empty lines. The first and last chapter blocks are ignored, so front matter and closing notes do not trigger deletion by themselves. The script deletes the book row and its dependent catalog rows from the SQLite database.

```bash
# Preview all deletions without writing
python src/AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py --dry-run

# Remove only a single book by internal id
python src/AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py --book-id 22025

# Delete the first 50 matches and print every scanned row
python src/AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py --limit 50 --verbose

# Parallel scan with worker processes and batched commits
python src/AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py --workers 8 --worker-chunk-size 16 --commit-every 200

# Fast bounded dry-run while tuning performance
python src/AudioBooks/Catalog/Gutenberg/repair_short_chapter_books.py --dry-run --workers 8 --scan-limit 200 --db-fetch-size 200
```

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--book-id N` | — | Scan only the given internal `books.id` (repeatable) |
| `--min-lines N` | 10 | Minimum non-empty lines required for interior chapters |
| `--limit N` | — | Stop after deleting N books |
| `--scan-limit N` | — | Stop after scanning N candidate books (dry-run friendly) |
| `--workers N` | 1 | Parallel workers for chapter scanning |
| `--worker-chunk-size N` | 8 | Chunk size for worker map batches |
| `--commit-every N` | 100 | Commit deletes every N matched books |
| `--db-fetch-size N` | 500 | SQLite rows fetched per batch before worker dispatch |
| `--verbose` | off | Print per-book scan output |
| `--dry-run` | off | Preview deletions without writing |
| `--db-path PATH` | auto | Override the SQLite database path |

The script uses the same chapter heading detection as the chapter-splitting test harness, so it only deletes books that actually split into 3 or more chapter blocks and have all middle chapters below the line threshold.
If process workers are unavailable in the runtime, the script automatically falls back to thread workers when `--workers > 1`.

### Sensitive-topic cleanup

Use `src/AudioBooks/Catalog/Gutenberg/repair_sensitive_topic_books.py` to remove catalog entries that match explicit topic filters for erotic, gory violence, psychic, intent to glorify violence or occult worship, or Bible-attack / anti-Bible content. The script checks the book title, subjects, and description for the general topic rules, and also scans the text for the Bible-related and intent-based violence/occult rules, then deletes only when one of the filters matches.

The violence rule is intentionally narrow: it is meant for text that reads like explicit gore, torture, mutilation, dismemberment, or `Saw`-style graphic content. A classic tragedy such as `Macbeth` should not match just because it contains murder or violent themes.

The occult and violence-glorification rules are intent-based. They are meant for passages where praise, worship, celebration, or endorsement appears in the same context as occult or violent themes, not for ordinary mentions of horror, tragedy, or religion.

```bash
# Preview all deletions without writing
python src/AudioBooks/Catalog/Gutenberg/repair_sensitive_topic_books.py --dry-run

# Remove only a single book by internal id
python src/AudioBooks/Catalog/Gutenberg/repair_sensitive_topic_books.py --book-id 22025

# Delete the first 50 matches and print every scanned row
python src/AudioBooks/Catalog/Gutenberg/repair_sensitive_topic_books.py --limit 50 --verbose
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

Use `src/AudioBooks/Catalog/Gutenberg/merge_books.py` when two or more `books` rows represent the same work. The script can detect duplicates automatically across the whole catalog, or merge an explicit pair.

```bash
# List all duplicate groups without writing anything
python src/AudioBooks/Catalog/Gutenberg/merge_books.py --find-duplicates

# Preview all auto-detected merges
python src/AudioBooks/Catalog/Gutenberg/merge_books.py --auto-merge --dry-run

# Execute all auto-detected merges
python src/AudioBooks/Catalog/Gutenberg/merge_books.py --auto-merge

# Merge only the first 20 groups
python src/AudioBooks/Catalog/Gutenberg/merge_books.py --auto-merge --limit 20

# Merge one explicit pair (source is deleted, target is kept)
python src/AudioBooks/Catalog/Gutenberg/merge_books.py --source 43478 --target 22025 --dry-run
python src/AudioBooks/Catalog/Gutenberg/merge_books.py --source 43478 --target 22025
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

| Flag | Default | Description |
|------|---------|-------------|
| `--find-duplicates` | off | Detect and list duplicate groups without merging |
| `--auto-merge` | off | Detect and merge all duplicate groups automatically |
| `--source N` | — | Internal `books.id` to merge from (will be deleted) |
| `--target N` | — | Internal `books.id` to merge into (canonical, kept) |
| `--dry-run` | off | Preview changes without writing to SQLite |
| `--limit N` | — | Max duplicate groups to process (`--auto-merge` only) |

### Audio catalog backfill

Use `src/AudioBooks/Catalog/Gutenberg/backfill_audio.py` to populate the audio tables from Gutenberg download links, repair stale URLs from the live file index, enrich narrator and chapter metadata from readme files, and fill gaps using LibriVox:

```bash
# Basic runs
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --dry-run --gutenberg-id 9036
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --mirror-tries 2
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --repair-all --workers 4
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --chunk-size 64 --workers 4

# LibriVox gap-filling (books with no Gutenberg audio or machine-read audio)
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --fill-librivox
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --fill-librivox --dry-run

# Skip readme fetching (faster, no narrator/chapter enrichment)
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --skip-readme

# Post-process only: enrich existing audio rows with narrator/chapter metadata from readme
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --enrich-readme
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --enrich-readme --dry-run
python src/AudioBooks/Catalog/Gutenberg/backfill_audio.py --enrich-readme --limit 50
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

Use `src/AudioBooks/Catalog/Gutenberg/backfill_cover_art.py` to populate `book_cover_art` from the Gutenberg RDF cache. Cover images are extracted from each book's RDF file and stored with their size label (`small`, `medium`, etc.) and direct URL.

```bash
python src/AudioBooks/Catalog/Gutenberg/backfill_cover_art.py --dry-run --limit 10
python src/AudioBooks/Catalog/Gutenberg/backfill_cover_art.py --no-verify
python src/AudioBooks/Catalog/Gutenberg/backfill_cover_art.py --refresh-live
python src/AudioBooks/Catalog/Gutenberg/backfill_cover_art.py --gutenberg-id 1342
```

| Flag | Default | Description |
|------|---------|-------------|
| `--gutenberg-id N` | — | Process only the specified Gutenberg id (repeatable) |
| `--refresh-live` | off | Fetch the live Gutenberg RDF even when a local cache exists |
| `--dry-run` | off | Preview without writing |
| `--no-verify` | off | Disable SSL certificate verification |
| `--limit N` | — | Stop after N books |
| `--ca-bundle PATH` | — | Path to a PEM CA bundle file |
| `--ca-dir PATH` | — | Path to a directory of CA certificates |
| `--db-path PATH` | auto | Override the SQLite database path |

### Book summaries backfill

Use `src/AudioBooks/Catalog/Gutenberg/backfill_book_desc.py` to import plot summaries from the CMU Book Summary Dataset into `book_desc`:

```bash
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc.py --dry-run --limit 20
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc.py
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc.py --tarball-path /path/to/booksummaries.tar.gz
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

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | off | Preview matches without writing |
| `--limit N` | — | Stop after N summary rows |
| `--tarball-path PATH` | auto | Use a local `booksummaries.tar.gz` instead of downloading |
| `--source-url URL` | CMU URL | Override the download URL |
| `--commit-every N` | 500 | Commit after N inserted rows |
| `--progress-every N` | 250 | Print progress after N rows |
| `--unmatched-limit N` | 20 | Number of unmatched rows to show in the summary |

Run CMU first (broad coverage), then follow up with the Gutenberg backfill below for gaps.

### Gutenberg HTML + images backfill

Use `src/AudioBooks/Catalog/Gutenberg/backfill_book_html.py` to download the illustrated HTML edition of each book (the `-h.zip` archive), upload its images to Google Cloud Storage, rewrite image paths so they route through the Flask image endpoint, and store the resulting HTML in `book_contents` with `content_type='html'`.

#### Why this exists

Project Gutenberg publishes an illustrated HTML edition (`-h.zip`) for most books. These editions contain inline images that are **absent from the plain-text file** — the only way to read these books as originally published is to render the HTML edition with its illustrations intact.

#### Source of the illustrations

The images are the original illustrations from each book's first publication, digitised by Project Gutenberg volunteers and released into the public domain along with the text. They include:

- **Engravings and woodcuts** from 19th-century novels and poetry collections
- **Photographs** from travel memoirs, natural history books, and biographies
- **Maps and diagrams** from geography, science, and history titles
- **Decorative frontispieces and chapter headings** from classic literature

Gutenberg's deep-linking policy forbids serving URLs that point directly to `gutenberg.org/files/...`, so we must self-host copies of those images. The cleaned HTML is stored in SQLite; the images are stored in Google Cloud Storage (`gs://gutenberg-books`).

#### Scale and storage requirements

| Metric | Value |
|--------|-------|
| Books with a `-h.zip` entry in `downloadlinks` | **75,764** |
| Total `downloadlinks` rows with type 8 | **925,761** (multiple per book across mirrors) |
| Average `-h.zip` size | ~500 KB – 2 MB |
| Average images per illustrated book | 10 – 20 files |
| **Estimated total image storage** | **20 – 50 GB** |
| HTML text per book (stored in SQLite) | ~100 – 800 KB |

This is why images go to GCS rather than SQLite:
- 50 GB of binary blobs would make `gutenbergindex.db` unmanageable and slow
- GCS provides CDN-friendly delivery and signed URL access control without loading the Flask process with binary data
- SQLite is kept for structured metadata and the text/HTML content itself

#### Why GCS signed URLs

Images are served via `GET /api/books/<book_id>/images/<filename>`, which generates a short-lived GCS signed URL (1-hour TTL) and returns a 302 redirect. This approach:
- Satisfies Gutenberg's deep-linking requirement (we serve from our own domain)
- Keeps image bytes out of the Flask process (browser fetches directly from GCS after redirect)
- Allows access control to be changed later without rebuilding the HTML

Signed URLs require a **service account key** (JSON file). Provide it via `--gcs-credentials` or the `GOOGLE_APPLICATION_CREDENTIALS` environment variable.

#### What gets stored

| Table | Column | Value |
|-------|--------|-------|
| `book_contents` | `content_type` | `'html'` |
| `book_contents` | `raw_content` | Original HTML from the zip (Gutenberg output, unmodified) |
| `book_contents` | `clean_content` | Same HTML with all image `src`/`href` paths rewritten to `/api/books/{id}/images/{filename}` |
| GCS | `book-html/{gutenberg_id}/images/{filename}` | Image files extracted from the zip |

#### DB migration

The `content_type` column did not exist in the original `book_contents` schema. The script runs `ALTER TABLE book_contents ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'` automatically on first run. Existing plain-text rows get `content_type = 'text'` with no data loss.

```bash
# Dry-run for one book (check what would be downloaded)
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --dry-run --gutenberg-ids 43477

# Single book with explicit service account credentials
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html \
  --gutenberg-ids 43477 \
  --gcs-credentials /path/to/service-account.json

# Full run with 8 parallel workers
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --workers 8

# Resume an interrupted run (already-done books are skipped automatically)
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --workers 4

# Re-process books that already have HTML content
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --force --reset-queue

# Check queue state from a previous run without processing anything
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --status

# Works around macOS SSL cert issues
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --no-verify
```

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--db PATH` | auto | Override the SQLite database path |
| `--gcs-bucket NAME` | `gutenberg-books` | GCS bucket name |
| `--gcs-credentials PATH` | env | Path to service account JSON key. Defaults to `GOOGLE_APPLICATION_CREDENTIALS` |
| `--book-ids IDS` | — | Comma-separated internal `books.id` values to process |
| `--gutenberg-ids IDS` | — | Comma-separated Gutenberg IDs to process |
| `--force` | off | Re-process books that already have HTML content in `book_contents` |
| `--dry-run` | off | Discover and log without downloading or writing anything |
| `--reset-queue` | off | Delete all queue rows for this namespace before running |
| `--status` | off | Print per-status queue counts and exit without processing |
| `--no-verify` | off | Disable SSL certificate verification (workaround for macOS cert issues) |
| `--ca-bundle PATH` | — | Path to a PEM CA bundle file |
| `--ca-dir PATH` | — | Path to a directory of CA certificates |
| `--workers N` | 4 | Parallel download workers |
| `--chunk-size N` | 100 | Queue chunk size per executor batch |
| `--max-attempts N` | 3 | Skip books with this many consecutive failures |
| `--limit N` | — | Stop after discovering N books (useful for test runs) |
| `--progress-every N` | 50 | Print progress every N books |

#### Practical run combinations

**Step 1 — check prior state before any run**
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --status
```

**Step 2 — dry-run a small batch to verify GCS connectivity and scope**
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --dry-run --limit 50
```

**Step 3 — smoke-test end-to-end with 10 books**
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --limit 10 --workers 2 --progress-every 5
```

**Step 4 — full production run (credentials and bucket loaded from `.env`)**
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html \
  --workers 8 --chunk-size 200 --max-attempts 3 --progress-every 50
```

**Resume after interruption** (queue remembers state; failed rows are retried up to `--max-attempts`)
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --workers 8 --max-attempts 5
```

**Process specific books by internal ID** (e.g. after a targeted repair)
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --book-ids 48907,12345 --force
```

**Force full re-run from scratch** (clears queue and reprocesses all books)
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html \
  --force --reset-queue --workers 8 --chunk-size 200
```

**Retry skipped books** — books with `status='skipped'` (no image-capable download found on the first pass) are excluded from normal runs.  Reset them to `pending` then re-run:
```bash
sqlite3 src/AudioBooks/Catalog/DB/gutenbergindex.db \
  "UPDATE book_content_backfill_queue SET status='pending', attempts=0 WHERE queue_key='html:v1' AND status='skipped';"
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --workers 8
```

**macOS SSL fix** — use `--no-verify` when urllib raises certificate verification errors
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --no-verify --workers 4
```

**Explicit credentials / bucket override** (overrides `.env` values)
```bash
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html \
  --gcs-bucket my-bucket --gcs-credentials /path/to/key.json --workers 8
```

| Command | Effect |
|---------|--------|
| `--status` | Print per-status queue counts from last run and exit |
| `--dry-run --limit 50` | Discover scope and verify GCS connectivity; no writes |
| `--limit 10 --workers 2` | Smoke-test end-to-end on 10 books |
| `--workers 8 --chunk-size 200` | Full production run; resumes from last checkpoint |
| `--workers 8 --max-attempts 5` | Resume an interrupted run with more retries |
| `--book-ids 48907,12345 --force` | Reprocess specific books by internal ID |
| `--force --reset-queue --workers 8` | Full re-run from scratch; overwrites existing HTML |
| `--no-verify --workers 4` | SSL verification disabled (macOS cert fix) |
| `--gcs-bucket NAME --gcs-credentials PATH` | Override `.env` bucket/credentials explicitly |

#### Count image-backed books in GCS

Use `src/AudioBooks/Catalog/Gutenberg/count_gcs_image_books.py` to count how many Gutenberg IDs currently have uploaded image objects in GCS (`book-html/<gid>/images/...`) and map those IDs back to internal `books.id` rows in SQLite.

```bash
# Uses GCS_BUCKET and GOOGLE_APPLICATION_CREDENTIALS from .env
python -m AudioBooks.Catalog.Gutenberg.count_gcs_image_books

# Explicit bucket/credentials override
python -m AudioBooks.Catalog.Gutenberg.count_gcs_image_books \
  --bucket gutenberg-books \
  --gcs-credentials /path/to/service-account.json

# Also print Gutenberg IDs that map to multiple internal book IDs
python -m AudioBooks.Catalog.Gutenberg.count_gcs_image_books \
  --show-duplicate-mappings --duplicate-limit 20
```

Output fields:

- `gcs_gutenberg_ids_with_images`: distinct Gutenberg IDs found under `book-html/<gid>/images/`
- `db_internal_book_ids_mapped`: distinct internal `books.id` rows mapped from those Gutenberg IDs
- `db_gutenberg_ids_matched`: distinct mapped Gutenberg IDs in SQLite
- `gids_without_db_match`: GCS Gutenberg IDs that were not found in SQLite
- `gutenberg_ids_with_multiple_internal_books`: Gutenberg IDs mapped to more than one internal book row

#### Queue table

Run state is stored in `book_content_backfill_queue` under namespace `html:v1`. Every startup prints the prior queue counts so you can see how much work remains from an interrupted run.

| Column | Description |
|--------|-------------|
| `status` | `pending` → `done` / `skipped` / `failed` (failed rows are retried on the next run up to `--max-attempts`) |
| `source_url` | The `-h.zip` URL that was (or will be) downloaded |
| `source_type` | Always `h-zip` |
| `attempts` | Incremented on each failure |
| `last_error` | Last exception message for failed books |

The queue is seeded on every run. Already-done and skipped rows are preserved across runs; only `pending` and `failed` rows are picked up for processing.

---

### Gutenberg book description backfill

Use `src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py` to fill gaps in `book_desc` using Gutenberg RDF metadata and ebook page summaries. Run this **after** `backfill_book_desc.py` to supplement books the CMU dataset did not cover.

The script is resumable — each run is tracked in a `book_desc_backfill_queue` SQLite table. Stopping and restarting automatically continues from where it left off.

```bash
# Preview what would be updated
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py --dry-run

# Normal run — only processes books missing a summary or wikipedia_id
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py

# Parallel run with 8 workers (faster for Wikipedia API / live fetches)
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py --workers 8

# Fetch live RDF and ebook page summaries for books not in the local cache
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py --refresh-live --workers 8

# Overwrite existing summaries with Gutenberg-derived ones
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py --force-summary

# Target a specific book
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py --gutenberg-id 1342
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py --book-id 22025

# Restart from scratch (clear queue rows for the current namespace)
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py --reset-queue

# Verbose output showing per-book step traces
python src/AudioBooks/Catalog/Gutenberg/backfill_book_desc_gutenberg.py --verbose --limit 50
```

The script resolves summaries in priority order per book:

1. **Sibling summary** — another `book_desc` row for the same normalized title + author.
2. **RDF `marc520` field** — from the local RDF cache at `Catalog/DB/cache/epub/<id>/pg<id>.rdf`.
3. **Ebook HTML page** — parses the `summary-text-container` node from `gutenberg.org/ebooks/<id>` (only when `--refresh-live` is passed).

Wikipedia URLs found in RDF `dcterms:description` text and `pgterms:webpage` links are resolved to Wikipedia page IDs via the Wikipedia API and stored in `book_desc.wikipedia_id`.

By default only books with a missing summary **or** missing `wikipedia_id` are loaded — already-complete rows are skipped at the SQL level.

#### Resumable queue

Run state is stored in `book_desc_backfill_queue` (one row per book per namespace):

| Column | Description |
|--------|-------------|
| `status` | `pending` → `done` / `skipped` / `failed` |
| `attempts` | Incremented on each failure; capped by `--max-attempts` |
| `last_error` | Last exception message for failed books |
| `summary_source` | Which source filled the summary (`gutenberg-rdf`, `book-desc-sibling`, etc.) |

Namespaces are auto-derived from the run mode — no flag needed:

| Run mode | Namespace |
|----------|-----------|
| Default (missing only) | `missing:v1` |
| `--force-summary` or `--force-wikipedia-id` | `force:v1` |
| Specific `--book-id` / `--gutenberg-id` | `targeted:v1:<hash>` |

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | off | Preview updates without writing |
| `--verbose` | off | Print per-book step traces (`step=sibling`, `step=rdf`, `step=html-fetch`, `step=wikipedia-resolve`) |
| `--limit N` | — | Stop after N target books |
| `--book-id N` | — | Process only the given internal `books.id` (repeatable) |
| `--gutenberg-id N` | — | Process only books with this `gutenbergbookid` (repeatable) |
| `--force-summary` | off | Overwrite existing non-empty summaries |
| `--force-wikipedia-id` | off | Overwrite existing `wikipedia_id` values |
| `--refresh-live` | off | Fetch live RDF/page content and ebook HTML when local cache is missing |
| `--workers N` | 4 | Parallel threads for I/O-bound work (Wikipedia API, RDF reads, HTML fetches) |
| `--chunk-size N` | 64 | Books submitted to the thread pool per batch |
| `--max-attempts N` | 3 | Stop retrying a book after N consecutive failures |
| `--reset-queue` | off | Clear saved queue rows for the current namespace and start fresh |
| `--commit-every N` | 200 | Commit writes every N updated rows |
| `--progress-every N` | 100 | Print progress after N processed books |
| `--db-path PATH` | auto | Override the SQLite database path |

#### Practical run combinations

| Command | Effect |
|---------|--------|
| `backfill_book_desc_gutenberg.py` | Fill missing summaries/wikipedia_ids from local RDF cache; resumable |
| `backfill_book_desc_gutenberg.py --workers 8` | Same with 8 parallel workers |
| `backfill_book_desc_gutenberg.py --refresh-live --workers 8` | Also fetch live RDF and ebook HTML pages |
| `backfill_book_desc_gutenberg.py --reset-queue` | Restart the run from scratch |
| `backfill_book_desc_gutenberg.py --force-summary --refresh-live` | Overwrite all summaries using live Gutenberg sources |
| `backfill_book_desc_gutenberg.py --gutenberg-id 1342 --verbose` | Debug a single book with full step traces |

---

## GCS Upload Pipeline

After backfilling `book_contents` and `book_desc` in SQLite, these two scripts upload the data to Google Cloud Storage so the remote summarization harness (`test_summaries_blackwell.py`) can run without a local SQLite connection.

**Run order:** `book_contents_upload.py` first, then `book_desc_upload.py`.

### Environment setup

Both scripts read credentials and bucket name from `.env`:

```
GCS_BUCKET=your-bucket-name
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

### GCS layout

| Path | Content |
|------|---------|
| `gs://<bucket>/book-contents/<bookid>/clean_content.txt` | Plain-text clean content |
| `gs://<bucket>/book-contents/<bookid>/clean_content.html` | HTML clean content (when `content_type='html'`) |
| `gs://<bucket>/book-desc/<bookid>.json` | Per-book title, author, and summary |
| `gs://<bucket>/book-desc/gutenberg-id-map.json` | `{str(gutenberg_id): book_id}` lookup used by the summarizer |

### `book_contents_upload.py`

Uploads `clean_content` from `book_contents` to GCS. Run state is persisted in `book_contents_upload_queue` so interrupted runs resume automatically.

```bash
# Check prior run state
python src/AudioBooks/BookSummary/book_contents_upload.py --status

# Dry-run to preview scope
python src/AudioBooks/BookSummary/book_contents_upload.py --dry-run --limit 20

# Smoke-test: upload 10 books with 2 workers
python src/AudioBooks/BookSummary/book_contents_upload.py --limit 10 --workers 2

# Full upload
python src/AudioBooks/BookSummary/book_contents_upload.py --workers 8 --chunk-size 200

# Resume an interrupted run (failed rows retried up to --max-attempts)
python src/AudioBooks/BookSummary/book_contents_upload.py --workers 8 --max-attempts 5

# Re-upload specific books
python src/AudioBooks/BookSummary/book_contents_upload.py --book-ids 48907,12345 --force

# Full re-run from scratch
python src/AudioBooks/BookSummary/book_contents_upload.py --reset-queue --workers 8
```

`--reset-queue` already clears all `done` rows for this uploader, so running `--force` in the same command does not change scope.

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--db PATH` | auto | Override the SQLite database path |
| `--bucket NAME` | `GCS_BUCKET` from `.env` | GCS bucket name |
| `--book-ids IDS` | — | Comma-separated internal `books.id` values |
| `--limit N` | — | Stop after discovering N books |
| `--force` | off | Re-upload books already marked done in the queue |
| `--dry-run` | off | Preview without uploading or writing queue rows |
| `--reset-queue` | off | Delete all queue rows before running (this already causes a full rerun, so `--force` is not needed in the same run) |
| `--status` | off | Print queue state and exit |
| `--workers N` | 4 | Parallel upload workers |
| `--chunk-size N` | 100 | Books per executor batch |
| `--max-attempts N` | 3 | Skip books with this many consecutive failures |
| `--progress-every N` | 50 | Print progress every N books |

### `book_desc_upload.py`

Uploads `book_desc` rows as per-book JSON blobs and writes the `gutenberg-id-map.json` index to GCS. The id map is what `test_summaries_blackwell.py` uses to resolve a Gutenberg id to an internal `book_id`. Run state is persisted in `book_desc_upload_queue`.

```bash
# Check prior run state
python src/AudioBooks/BookSummary/book_desc_upload.py --status

# Dry-run to preview scope
python src/AudioBooks/BookSummary/book_desc_upload.py --dry-run --limit 20

# Full upload
python src/AudioBooks/BookSummary/book_desc_upload.py --workers 8 --chunk-size 200

# Resume after interruption
python src/AudioBooks/BookSummary/book_desc_upload.py --workers 8 --max-attempts 5

# Force re-upload specific books
python src/AudioBooks/BookSummary/book_desc_upload.py --book-ids 48907,12345 --force

# Full re-run from scratch
python src/AudioBooks/BookSummary/book_desc_upload.py --reset-queue --workers 8
```

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--db PATH` | auto | Override the SQLite database path |
| `--bucket NAME` | `GCS_BUCKET` from `.env` | GCS bucket name |
| `--book-ids IDS` | — | Comma-separated internal `books.id` values |
| `--limit N` | — | Stop after discovering N books |
| `--force` | off | Re-upload books already marked done in the queue |
| `--dry-run` | off | Preview without uploading or writing queue rows |
| `--reset-queue` | off | Delete all queue rows before running (this already causes a full rerun, so `--force` is not needed in the same run) |
| `--status` | off | Print queue state and exit |
| `--workers N` | 4 | Parallel upload workers |
| `--chunk-size N` | 100 | Books per executor batch |
| `--max-attempts N` | 3 | Skip books with this many consecutive failures |
| `--progress-every N` | 50 | Print progress every N books |

The `gutenberg-id-map.json` is always (re)uploaded at the end of every successful run, even partial ones, so it stays consistent with the book-desc blobs that were written.

### GCS JSON schema — `book-desc/<bookid>.json`

Each book-desc blob is a JSON object with the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `bookid` | int | Internal `books.id` |
| `source_title` | string | Book title from catalog |
| `source_author` | string | Author(s) from catalog |
| `summary` | string | Plot summary text |
| `subjects` | list[str] | Subject strings from `book_subjects` + `subjects` tables |
| `category` | string | One of `dramatic`, `biographical`, `analytical`, `practical` |

The `subjects` and `category` fields were added in queue version `desc:v2` and the category classification was improved in `desc:v3` (summary-text fallback). Books uploaded before these versions lack the fields. To upload or re-upload all books:

```bash
python src/AudioBooks/BookSummary/book_desc_upload.py --workers 8
```

Because the queue key is now `desc:v3`, the queue starts empty and all books are processed automatically — `--force` is not needed.

---

## AI Summarization Pipeline (BookSummary)

The `src/AudioBooks/BookSummary/` module generates AI-powered summaries and character/speaker profiles for each book using a HuggingFace Inference Endpoint. The pipeline runs from a Jupyter notebook (`Local_HF_Endpoint.ipynb`) and reads book text and metadata from GCS.

### Files

| File | Purpose |
|------|---------|
| `summarizer.py` | Core pipeline: chapter splitting, map-reduce summarization, character profile extraction |
| `Local_HF_Endpoint.ipynb` | Notebook driver: endpoint creation, per-book orchestration, result persistence |
| `book_contents_upload.py` | Uploads book text from SQLite → GCS (`book-contents/`) |
| `book_desc_upload.py` | Uploads book metadata from SQLite → GCS (`book-desc/`) |

### Book Category Classification

`src/AudioBooks/Catalog/book_category.py` provides a shared `classify_book(subjects)` function used by both the frontend catalog (`CatalogRepository`) and the summarizer. It maps Gutenberg subject strings to one of four categories modelled on the Audible/Amazon genre taxonomy.

#### The four categories

| Category | Description | Audible equivalents |
|----------|-------------|---------------------|
| `dramatic` | All forms of fiction and performed literature | Literature & Fiction, Mystery/Thriller, Sci-Fi & Fantasy, Romance, Horror, Children's |
| `biographical` | Real people, real events, personal narrative | Biographies & Memoirs, History, True Crime, Travel |
| `analytical` | Structured knowledge, argument, and theory | Science, Philosophy, Politics & Social Sciences, Law, Religion, Textbooks |
| `practical` | How-to, reference, self-improvement | Self Help, Health & Wellness, Cookbooks, Crafts, Parenting, Reference |

#### How categories are assigned

Category assignment happens in two stages and at two different points in the pipeline:

**Stage 1 — at upload time (`book_desc_upload.py`)**

When a book's metadata is uploaded to GCS, classification runs in two steps and the result is written into `book-desc/<bookid>.json` as the `"category"` field:

```
1. classify_book_strict(subjects)   ← LCSH subject keyword regex
       ↓ None (no keyword matched)?
2. classify_book_strict([summary])  ← same regex run on book_desc.summary text
       ↓ still None?
3. "dramatic"                       ← hard default
```

Most books have a `book_desc.summary` (sourced from CMU/Gutenberg backfills) that contains natural language genre signals such as "a biography of…", "a novel about…", or "a practical guide to…" which the same regex patterns catch reliably. The frontend catalog (`CatalogRepository.get_books()`) reads this field directly.

**Stage 2 — at summarization time (`summarizer.py`)**

`summarize_book()` runs three checks in order and stops at the first result:

```
1. book.category (pre-loaded from the GCS book-desc JSON)
       ↓ empty?
2. classify_book_strict(book.subjects)   ← regex keyword matching
       ↓ None (no keyword matched)?
3. classify_book_with_llm(...)           ← LLM classification using final summary
       ↓ parse failure?
4. "dramatic"                            ← hard default
```

- **Step 1** reuses the category already written at upload time — no work done.
- **Step 2** (`classify_book_strict`) is the same keyword regex logic as `classify_book` but returns `None` instead of the dramatic default, so the pipeline can tell the difference between a real match and a fallback.
- **Step 3** (`classify_book_with_llm`) only fires when subjects are empty or none of the keyword regexes matched. It sends the book's title, author, and **final summary** (already computed at this point) to the LLM with a one-word classification prompt. The response is parsed for the first matching category word; if the parse fails, the result is `dramatic`.
- The `max_new_tokens` for the LLM call is **10** — just enough for one word — so it adds negligible cost and latency.

**Where the result is used**

| Consumer | How it uses the category |
|----------|--------------------------|
| `CatalogRepository.get_books()` | Returned in the API response for frontend filtering |
| `summarize_book()` result dict | Stored as `"category"` in the per-book output |
| `extract_character_profiles()` | Selects the character profile field template (dramatic/biographical/analytical/practical) |

#### How classification works (regex)

`classify_book` joins all subject strings with ` | ` and runs four compiled regexes against the result in priority order:

```
dramatic → analytical → biographical → practical → dramatic (default)
```

If no regex matches the book defaults to `dramatic`, because most unlabelled Gutenberg books are fiction.

#### Subject keywords per category

**dramatic** — matches any subject containing:

| Group | Keywords |
|-------|---------|
| Core fiction | `fiction`, `novel`, `novella`, `drama`, `plays` |
| Genre fiction | `mystery`, `detective`, `romance`, `love story`, `thriller`, `suspense` |
| Speculative | `horror`, `gothic`, `supernatural`, `ghost stories`, `fantasy`, `fairy tales`, `folk tales`, `science fiction`, `sci-fi`, `dystopi`, `utopi` |
| Voice/form | `satire`, `comedy`, `humour`, `allegory`, `fable`, `parable`, `bildungsroman`, `picaresque`, `epistolary` |
| Format | `adventure stories`, `sea stories`, `war stories`, `western stories`, `short stories`, `anthology` |
| Qualified fiction | `historical fiction`, `crime fiction`, `domestic fiction`, `social fiction`, `children's fiction/literature` |
| Age category | `young adult` |
| Performance | `melodrama`, `tragedy`, `farce`, `manga`, `comic`, `graphic novel` |

**analytical** — matches any subject containing:

| Group | Keywords |
|-------|---------|
| Compound (checked first to beat simple "history") | `natural history`, `art history`, `church history`, `literary history`, `literary criticism`, `music theory`, `political economy`, `social science`, `computer science`, `political science`, `natural philosophy` |
| Hard sciences | `science`, `mathematics`, `math`, `physics`, `chemistry`, `astronomy`, `biology`, `botany`, `zoology`, `ecology`, `geology`, `paleontology` |
| Medicine | `medicine`, `anatomy`, `surgery`, `pharmacology`, `physiology` |
| Engineering & tech | `engineering`, `technology`, `architecture` |
| Humanities | `philosophy`, `ethics`, `logic`, `metaphysics`, `linguistics`, `grammar`, `rhetoric`, `archaeology`, `geography`, `sociology`, `anthropology` |
| Social sciences | `economics`, `economy`, `economic`, `politics`, `government`, `law`, `jurisprudence`, `psychology`, `psychiatry` |
| Religion | `theology`, `religion`, `spirituality` |
| Format | `textbook`, `treatise` |

**biographical** — matches any subject containing:

| Group | Keywords |
|-------|---------|
| Personal narrative | `biography`, `autobiography`, `memoir`, `diary`, `personal narrative` |
| Correspondence | `correspondence`, `letters`, `reminiscence`, `anecdote`, `recollection` |
| History | `history`, `historical`, `military` |
| Exploration | `expedition`, `exploration`, `explorer`, `travel`, `travel writing`, `travel accounts`, `travel narratives` |
| Crime | `true crime` |

**practical** — matches any subject containing:

| Group | Keywords |
|-------|---------|
| Self-improvement | `self-help`, `self improvement`, `personal development`, `motivational` |
| Food | `cooking`, `recipes`, `cookbook`, `food and wine` |
| Domestic | `gardening`, `horticulture`, `craft`, `sewing`, `knitting`, `needlework`, `farming`, `agriculture`, `household`, `housekeeping` |
| Wellbeing | `health`, `fitness`, `nutrition`, `diet`, `wellness`, `exercise`, `relationships` |
| Career/reference | `career`, `manual`, `handbook`, `reference`, `almanac`, `dictionary`, `encyclopedia`, `guide`, `test prep` |
| Family | `parenting`, `childcare`, `child rearing` |

#### Edge cases and priority rationale

- **"Natural history"** → `analytical` (not `biographical`) because the compound `natural history` is tested inside the analytical regex *before* the simple `history` keyword in the biographical regex.
- **"Historical fiction"** → `dramatic` because dramatic is checked first and the `historical fiction` phrase is an explicit term in the dramatic regex.
- **No subjects / empty list** → `dramatic` (default). Most unlabelled Gutenberg books are fiction.
- **Multiple matching categories** → whichever tier ranks first in the priority chain wins.

```python
from AudioBooks.Catalog.book_category import classify_book

classify_book(["Fiction", "Adventure stories"])      # → "dramatic"
classify_book(["Natural history", "Science"])        # → "analytical"
classify_book(["Historical fiction"])                # → "dramatic"  (not biographical)
classify_book(["Biography"])                         # → "biographical"
classify_book(["Cooking", "Recipes"])                # → "practical"
classify_book([])                                    # → "dramatic"  (default)
```

### HuggingFace Inference Endpoint

The notebook creates or reuses a HuggingFace dedicated Inference Endpoint (Qwen2.5-7B-Instruct on A100 GPU) and monkey-patches the summarizer's generate functions to route all LLM calls through it.

#### Endpoint creation flow

```python
from huggingface_hub import get_inference_endpoint, create_inference_endpoint

def load_or_create_endpoint(name, model, ...):
    try:
        endpoint = get_inference_endpoint(name)      # reuse existing
    except RepositoryNotFoundError:
        endpoint = create_inference_endpoint(        # create new
            name=name,
            repository=model,
            framework="pytorch",
            task="text-generation",
            accelerator="gpu",
            instance_type="nvidia-a100",
            ...
        )
    endpoint.wait()                                  # blocks until RUNNING
    return InferenceClient(base_url=endpoint.url, token=HF_TOKEN)
```

#### Monkey-patching the summarizer

Once the endpoint client is ready the notebook replaces the summarizer's local generate functions:

```python
import AudioBooks.BookSummary.summarizer as ai_summarizer

client = load_or_create_endpoint(...)

def _remote_generate_text(model, tokenizer, prompt, ...):
    response = client.text_generation(prompt, max_new_tokens=..., ...)
    return response

def _remote_generate_batch(model, tokenizer, prompts, ...):
    with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
        futures = [pool.submit(_remote_generate_text, ..., p, ...) for p in prompts]
        return [f.result() for f in futures]

ai_summarizer._generate_text = _remote_generate_text
ai_summarizer._generate_batch = _remote_generate_batch
```

All subsequent `summarizer.summarize_book()` calls transparently use the remote endpoint with concurrent batch calls.

### RunPod vLLM Endpoint

RunPod is a separate GPU hosting path from HuggingFace Inference Endpoints. After
you deploy a RunPod vLLM worker or pod, RunPod exposes an OpenAI-compatible
endpoint. The project script `src/AudioBooks/BookSummary/runpod_summarize.py` is the
client that calls that exposed endpoint and feeds responses into the same
`summarizer.py` pipeline.

Use this when the model is already deployed on RunPod:

```bash
export RUNPOD_API_KEY="..."
export RUNPOD_ENDPOINT_ID="..."
export HF_TOKEN="..."
export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/credentials.json"
export GCS_BUCKET="gutenberg-books"

python src/AudioBooks/BookSummary/runpod_summarize.py \
  --book-id 4037 \
  --validate
```

For a RunPod serverless vLLM endpoint, the script builds this base URL from
`RUNPOD_ENDPOINT_ID`:

```text
https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1
```

For a RunPod Pod or any custom vLLM deployment, pass the exposed `/v1` URL
directly:

```bash
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --base-url "https://<your-runpod-vllm-host>/v1" \
  --book-id 4037
```

The underlying client shape is:

```python
from openai import OpenAI

client = OpenAI(
    api_key=RUNPOD_API_KEY,
    base_url="https://api.runpod.ai/v2/ENDPOINT_ID/openai/v1",
)
```

Use `client.completions.create(...)` to preserve the current raw
`### Instruction` prompts. Only use `client.chat.completions.create(...)` if the
prompts are deliberately converted to chat messages and Qwen's chat template is
applied consistently.

The RunPod script preserves the same summarization behavior as the notebook:

- loads the Qwen tokenizer locally for prompt sizing/truncation
- calls vLLM through OpenAI-compatible `completions`
- uses greedy generation with `temperature=0`
- keeps `repetition_penalty=1.05`
- writes JSONL output to `src/AudioBooks/BookSummary/Artifacts/summary_results_runpod.jsonl`
- resumes interrupted books through `src/AudioBooks/BookSummary/Artifacts/checkpoints/runpod`

#### RunPod script options

| Option | Default | Description |
|--------|---------|-------------|
| `--model-id` | `Qwen/Qwen2.5-7B-Instruct` or `RUNPOD_MODEL_ID` | Model name sent to vLLM and used for local tokenizer loading |
| `--api-key` | `RUNPOD_API_KEY` | RunPod API key |
| `--endpoint-id` | `RUNPOD_ENDPOINT_ID` | Serverless endpoint id; used to build `https://api.runpod.ai/v2/<id>/openai/v1` |
| `--base-url` | `RUNPOD_VLLM_BASE_URL` | Explicit OpenAI-compatible `/v1` base URL; overrides endpoint-id URL construction |
| `--timeout` | `900.0` | OpenAI client request timeout, in seconds |
| `--request-retries` | `3` | Retry attempts per generation request |
| `--book-id` | — | Summarize one internal `books.id` |
| `--gutenberg-id` | — | Resolve one Gutenberg id through the GCS id map, then summarize that book |
| `--max-books` | — | Run the first N unprocessed books; ignored for single-book runs |
| `--bucket` | `GCS_BUCKET` or `gutenberg-books` | GCS bucket with `book-desc/` and `book-contents/` |
| `--gcs-credentials` | `GOOGLE_APPLICATION_CREDENTIALS` | Service-account JSON path |
| `--chunk-tokens` | `4096` | Max input tokens per chapter chunk before overlap/context |
| `--chunk-overlap` | `400` | Token overlap between adjacent chunks |
| `--reduce-input-tokens` | `8192` | Context window used for reduction/profile prompts |
| `--max-new-tokens` | `512` | Output budget for chunk summaries |
| `--reduce-max-new-tokens` | `768` | Output budget for reductions and story-so-far |
| `--profile-max-new-tokens` | `1024` | Output budget for character/narrator profiles |
| `--temperature` | `0.0` | Greedy deterministic generation; keep at `0.0` to match the notebook |
| `--repetition-penalty` | `1.05` | Penalty sent to vLLM; matches the notebook |
| `--batch-size` | `4` | Concurrent RunPod vLLM requests |
| `--max-chapters` | — | Smoke-test cap on chapters per book |
| `--max-chunks-per-chapter` | — | Smoke-test cap on chunks per chapter |
| `--validate` | off | Load local embedding/NLI models and score against `book-desc` summary |
| `--semantic-threshold` | `0.60` | Minimum semantic similarity for validation |
| `--lexical-floor` | `0.35` | Lexical overlap floor used in validation |
| `--nli-contradiction-threshold` | `0.50` | Max allowed NLI contradiction score |
| `--embedding-model-id` | `sentence-transformers/all-MiniLM-L6-v2` | Validation embedding model |
| `--nli-model-id` | `tasksource/deberta-small-long-nli` | Validation NLI model |
| `--output-path` | `src/AudioBooks/BookSummary/Artifacts/summary_results_runpod.jsonl` | JSONL output file (anchored to the script dir, not the CWD) |
| `--checkpoint-dir` | `src/AudioBooks/BookSummary/Artifacts/checkpoints/runpod` | Per-book checkpoint directory |

#### Practical command combinations

Single book smoke test without validation:

```bash
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --book-id 4037 \
  --max-chapters 1 \
  --max-chunks-per-chapter 1
```

Single full book with validation:

```bash
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --book-id 4037 \
  --validate
```

Run by Gutenberg id:

```bash
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --gutenberg-id 730 \
  --validate
```

Process the next 20 unprocessed books:

```bash
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --max-books 20
```

Use a custom RunPod Pod or self-hosted vLLM URL:

```bash
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --base-url "https://<your-runpod-vllm-host>/v1" \
  --book-id 4037
```

Increase concurrency on a strong A100 SXM endpoint:

```bash
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --max-books 20 \
  --batch-size 8
```

Keep HF-compatible deterministic generation while changing output files:

```bash
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --book-id 4037 \
  --temperature 0 \
  --repetition-penalty 1.05 \
  --output-path Artifacts/summary_results_runpod_a100.jsonl \
  --checkpoint-dir Artifacts/checkpoints/runpod_a100
```

Recommended stack:

```text
RunPod A100 SXM + vLLM + Hugging Face Qwen model + OpenAI-compatible client
```

This is the cleanest match for audiobook summarization because:

- **RunPod A100 SXM** gives enough VRAM and throughput for long-context chunk
  summarization, concurrent chapter chunk requests, and future larger models.
- **vLLM** is optimized for serving many independent generation requests, which
  matches the pipeline's `ThreadPoolExecutor` batch pattern.
- **Hugging Face Qwen model weights** keep behavior close to the existing
  HuggingFace Inference Endpoint notebook path. Avoid Ollama/GGUF quantized
  variants here if summary fidelity matters.
- **OpenAI-compatible client** keeps the project-side integration small: the
  RunPod script only swaps the generation call, while `summarizer.py` still owns
  chapter splitting, rolling context, final reduction, profiles, checkpoints,
  and validation.
- **Completions rather than chat** preserves the existing raw prompt format from
  `Local_HF_Endpoint.ipynb`, reducing behavior drift when moving from HF
  Inference Endpoints to RunPod.

To avoid accuracy or behavior drift between the HuggingFace Inference Endpoint
notebook and RunPod, keep the model and generation contract aligned:

```text
model: Qwen/Qwen2.5-7B-Instruct
dtype: bfloat16 or float16
quantization: none
prompt format: same raw completion prompt text
max tokens: same max_new_tokens / max_tokens values
sampling: temperature=0, greedy decoding
repetition penalty: 1.05
context length: same or larger than the notebook truncation window
endpoint API: OpenAI-compatible completions, not chat, unless prompts are converted deliberately
```

The `runpod_summarize.py` client already preserves the project-side parts of
that contract: it uses the same Qwen tokenizer for prompt truncation, the same
raw prompts, OpenAI-compatible `completions`, `temperature=0`, and
`repetition_penalty=1.05`. The RunPod deployment itself should use the
non-quantized Hugging Face Qwen weights with bf16/fp16. Avoid Ollama/GGUF
quantized variants for this path if summary fidelity is the priority.

### GCP Spot VM summarizer

`src/AudioBooks/BookSummary/spot_vm_summarize.py` is the CLI version of the
notebook flow for Google Cloud. It supports two backends:

- `--mode local`: load the model directly on the VM GPU with `transformers`
- `--mode hf-endpoint`: keep generation on a HuggingFace Inference Endpoint
  while the VM handles GCS I/O, chunking, reduction, checkpoints, and optional
  validation

Use this when you want the same summarization pipeline on a GCP Spot VM instead
of a notebook.

Basic environment:

```bash
export HF_TOKEN="..."
export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/credentials.json"
export GCS_BUCKET="gutenberg-books"
```

Local GPU mode on a Spot VM:

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode local \
  --book-id 4037 \
  --validate
```

HuggingFace Inference Endpoint mode from the Spot VM:

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode hf-endpoint \
  --book-id 4037 \
  --validate
```

#### Spot VM options

| Option | Default | Description |
|--------|---------|-------------|
| `--mode` | `local` | `local` runs the model on the VM GPU; `hf-endpoint` calls a HuggingFace Inference Endpoint |
| `--model-id` | `Qwen/Qwen2.5-7B-Instruct` | Model id for local load or HF endpoint creation/use |
| `--book-id` | — | Summarize one internal `books.id` |
| `--gutenberg-id` | — | Resolve one Gutenberg id from the GCS id map |
| `--max-books` | — | Process the first N unprocessed books |
| `--num-shards` | `1` | Total shard count for parallel replicas |
| `--shard-index` | `0` | Current shard index in `[0, num_shards)` |
| `--bucket` | `GCS_BUCKET` or `gutenberg-books` | GCS bucket containing uploaded data |
| `--gcs-credentials` | `GOOGLE_APPLICATION_CREDENTIALS` | Service-account JSON path |
| `--chunk-tokens` | `4096` | Max input tokens per chapter chunk |
| `--chunk-overlap` | `400` | Overlap between adjacent chunks |
| `--reduce-input-tokens` | `8192` | Reduction/profile context window |
| `--max-new-tokens` | `512` | Output budget for chunk summaries |
| `--reduce-max-new-tokens` | `768` | Output budget for reductions and story-so-far |
| `--profile-max-new-tokens` | `1024` | Output budget for character/narrator profiles |
| `--batch-size` | `4` | Local mode: GPU generation batch size. HF endpoint mode: concurrent requests |
| `--max-chapters` | — | Smoke-test cap on chapters |
| `--max-chunks-per-chapter` | — | Smoke-test cap on chunks per chapter |
| `--load-in-4bit` | off | Local mode only; use 4-bit loading to fit on smaller GPUs |
| `--endpoint-name` | `audiobook-summary-qwen25-7b` | HF endpoint name in `hf-endpoint` mode |
| `--endpoint-instance-type` | `nvidia-a100` | HF endpoint GPU type |
| `--endpoint-instance-size` | `x1` | HF endpoint instance size |
| `--endpoint-vendor` | `aws` | HF endpoint cloud vendor |
| `--endpoint-region` | `us-east-1` | HF endpoint region |
| `--no-create-endpoint` | off | Fail instead of creating a missing HF endpoint |
| `--validate` | off | Load local embedding/NLI models and score summaries |
| `--semantic-threshold` | `0.60` | Minimum semantic similarity for validation |
| `--lexical-floor` | `0.35` | Lexical overlap floor for validation |
| `--nli-contradiction-threshold` | `0.50` | Max allowed contradiction score |
| `--embedding-model-id` | `sentence-transformers/all-MiniLM-L6-v2` | Validation embedding model |
| `--nli-model-id` | `tasksource/deberta-small-long-nli` | Validation NLI model |
| `--output-path` | mode-dependent: `src/AudioBooks/BookSummary/Artifacts/summary_results_local.jsonl` (`--mode local`) or `…/summary_results.jsonl` (`--mode hf-endpoint`) | JSONL output file (anchored to the script dir, not the CWD). An explicit value overrides the per-mode default. |
| `--checkpoint-dir` | `src/AudioBooks/BookSummary/Artifacts/checkpoints` | Per-book checkpoint directory |

#### Practical command combinations

Single-book smoke test on the VM GPU:

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode local \
  --book-id 4037 \
  --max-chapters 1 \
  --max-chunks-per-chapter 1
```

Single full book on the VM GPU with validation:

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode local \
  --book-id 4037 \
  --validate
```

Run the next 20 unprocessed books locally in 4-bit mode:

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode local \
  --load-in-4bit \
  --max-books 20 \
  --validate
```

Use the Spot VM only as an orchestrator while generation stays on HF Endpoint:

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode hf-endpoint \
  --max-books 20 \
  --validate
```

Shard across multiple VM replicas or multiple GPUs:

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode local \
  --max-books 100 \
  --num-shards 2 \
  --shard-index 0
```

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode local \
  --max-books 100 \
  --num-shards 2 \
  --shard-index 1
```

Keep output/checkpoints separate for an experiment run:

```bash
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode local \
  --book-id 4037 \
  --output-path Artifacts/summary_results_local_a100.jsonl \
  --checkpoint-dir Artifacts/checkpoints/local_a100
```

#### GCP practical guidance

- Use `--mode local` on a GCP Spot VM when the GPU is already provisioned and
  you want the simplest path with no remote endpoint dependency.
- Use `--load-in-4bit` on smaller GPUs like L4 when VRAM is tight; avoid it on
  A100 if fidelity matters more than memory pressure.
- Use `--mode hf-endpoint` when you want resumable orchestration and validation
  on the VM, but you do not want the VM to host the model itself.
- Use `--num-shards` and `--shard-index` to split whole books across multiple
  workers; this avoids breaking the per-book rolling `story_so_far` logic.

### Overriding chunk and reduce token sizes

Two token budgets control how much text the model reads per pass, and therefore
how detailed the summaries are. Both default to the values used by
`Local_HF_Endpoint.ipynb`, and are kept **identical** across `summarizer.py`,
`runpod_summarize.py`, and `spot_vm_summarize.py` so all backends produce
comparable output:

| Flag | Default | What it controls |
|------|---------|------------------|
| `--chunk-tokens` | `4096` | Size of each chapter chunk fed to the map step. Larger chunks pack more source text into the same `--max-new-tokens` summary, so output becomes **denser/terser**. |
| `--reduce-input-tokens` | `8192` | Context window for the reduce step, story-so-far, and profiles. It is both the recursion trigger **and** the input-truncation limit in `reduce_texts`. A smaller value reduces sooner and truncates more, so output becomes **more concise** (and can drop later detail). |
| `--chunk-overlap` | `400` | Token overlap between adjacent chunks (continuity across chunk boundaries). |

The module defaults live in `src/AudioBooks/BookSummary/summarizer.py`
(`DEFAULT_CHUNK_TOKENS`, `DEFAULT_REDUCE_INPUT_TOKENS`); the two runner scripts
pin the same values via `NB_CHUNK_TOKENS` / `NB_REDUCE_INPUT_TOKENS`.

> Note: earlier the runner scripts defaulted to `6000 / 4096`, which produced
> noticeably terser `story_so_far` and chapter summaries than the notebook.
> They are now aligned to `4096 / 8192`.

Override per run on any entry point — for example, to fit a smaller context
window or to make summaries even more detailed:

```bash
# RunPod: smaller chunks, wider reduce window
python src/AudioBooks/BookSummary/runpod_summarize.py \
  --book-id 4037 \
  --chunk-tokens 4096 --reduce-input-tokens 8192

# Spot VM: larger chunks (terser) to cut the number of model calls
python src/AudioBooks/BookSummary/spot_vm_summarize.py \
  --mode local --book-id 4037 \
  --chunk-tokens 6000 --reduce-input-tokens 4096

# summarizer.py CLI uses the same flag names
python src/AudioBooks/BookSummary/summarizer.py \
  --book-id 4037 \
  --chunk-tokens 4096 --reduce-input-tokens 8192 --chunk-overlap 400
```

Keep `--reduce-input-tokens` at or below the served model's context length
(Qwen2.5-7B-Instruct is 32k), and leave room for `--reduce-max-new-tokens`
output on top of the input budget.

### Summarization Pipeline

`summarizer.py` implements a map-reduce strategy:

```
book text
  │
  ├─ split into chapters
  │
  └─ for each chapter:
       split into chunks (≤ chunk_max_tokens)
       for each chunk:
         build prompt with "story so far" context window
         call _generate_batch() concurrently
       reduce chunk summaries → chapter summary
       update rolling "story so far"
  │
  └─ reduce all chapter summaries → final summary
  │
  └─ extract character profiles (category-aware)
```

Key design points:
- **Rolling context**: each chunk prompt receives a truncated version of all preceding chapter summaries as "the story so far", giving the model continuity across the book.
- **Chapter-level checkpointing**: completed chapter results are written to a local `.ckpt` file. If the run is interrupted, it resumes from the last completed chapter. The checkpoint is deleted on full completion.
- **Concurrent HF calls**: `_generate_batch` uses `ThreadPoolExecutor(max_workers=BATCH_SIZE)` to call the endpoint for all chunks in a chapter simultaneously.
- **`BookRecord` dataclass**: holds `book_id`, `title`, `authors`, `text`, `subjects`, and `category`. Category is loaded from the GCS book-desc JSON or derived via `classify_book(subjects)` at runtime.

### Character / Speaker Profiles

After the final summary is produced, the pipeline extracts a profile for every named character or speaker mentioned in the book. Profiles are used for dramatic direction (voice acting, tone) and vary by category:

| Category | Profile fields |
|----------|---------------|
| `dramatic` | Role, Personality, Circumstances shaping mood/demeanor, Emotional expression (weeping/laughing/raging), Physical manner (hurried/shuffling/trembling) |
| `biographical` | Role, Character/personality, Life context, Emotional moments as described |
| `analytical` | Role/position, Background/expertise, Key contributions, Perspective/stance |
| `practical` | Author voice/persona, Expertise, Core methodology, Audience relationship |

Profiles are extracted by feeding all chapter summaries (or the final summary if the chapter summaries exceed the context window) to the model with a structured prompt, then parsing the response.

**Where profiles are saved:** character profiles are included in the per-book result dict returned by `summarize_book()` and persisted to the local JSONL artifact file (`src/AudioBooks/BookSummary/Artifacts/summary_results_local.jsonl`). They are not uploaded to GCS.

### Running the pipeline

Open `src/AudioBooks/BookSummary/Local_HF_Endpoint.ipynb` and run the cells in order:

1. **Config cell** — set `HF_TOKEN`, `GCS_BUCKET`, `ENDPOINT_NAME`, `MODEL_ID`, `BATCH_SIZE`, `PROFILE_MAX_NEW_TOKENS`.
2. **Endpoint cell** — calls `load_or_create_endpoint()`; blocks until the endpoint is `RUNNING`.
3. **Monkey-patch cell** — replaces `ai_summarizer._generate_text` and `ai_summarizer._generate_batch`.
4. **Summarize cell** — iterates over book IDs, calls `summarize_one_book_via_hf_endpoint()`, prints summaries and character profiles, appends results to the JSONL file.

### Updated GCS layout

| Path | Content |
|------|---------|
| `gs://<bucket>/book-contents/<bookid>/clean_content.txt` | Plain-text clean content |
| `gs://<bucket>/book-contents/<bookid>/clean_content.html` | HTML clean content (when `content_type='html'`) |
| `gs://<bucket>/book-desc/<bookid>.json` | Title, author, summary, **subjects**, **category** |
| `gs://<bucket>/book-desc/gutenberg-id-map.json` | `{str(gutenberg_id): book_id}` lookup |

---

## GCP Spot VM Inference with Terraform

Provision a low-cost GCP Spot VM to run large language models (like Gemma) using Terraform.

### How to Run

1. **Authenticate and Set Project**:
   ```bash
   gcloud auth application-default login
   gcloud auth application-default set-quota-project gen-lang-client-0910392250
   # or
   gcloud config set project gen-lang-client-0910392250
   ```

2. **Initialize Terraform**:
   ```bash
   cd src/AudioBooks/Terraform
   terraform init
   ```

3. **Deploy (Setup)**:
   Provision the spot instance.
   ```bash
   terraform apply -var="hf_token=your_hf_token_here" -auto-approve
   ```

4. **Connect to VM**:
   ```bash
   gcloud compute ssh gemma-cpu-spot-tf --zone=us-central1-a
   ```

5. **Run Inference**:
   Once inside the VM, create a file named `inference.py`:
   ```python
   import torch
   from transformers import AutoTokenizer, AutoModelForCausalLM

   model_id = "google/gemma-2-9b-it" # or "google/gemma-2-27b-it"

   print("Loading model to CPU...")
   tokenizer = AutoTokenizer.from_pretrained(model_id)
   model = AutoModelForCausalLM.from_pretrained(model_id, device_map="cpu", torch_dtype=torch.bfloat16)

   input_text = "What are the advantages of running Gemma on a Spot VM?"
   inputs = tokenizer(input_text, return_tensors="pt").to("cpu")

   print("Running inference...")
   outputs = model.generate(**inputs, max_new_tokens=50)
   print(tokenizer.decode(outputs[0]))
   ```
   Execute it:
   ```bash
   python3 inference.py
   ```

6. **Teardown**:
   Destroy the instance when finished to stop all active billing.
   ```bash
   terraform destroy -var="hf_token=your_hf_token_here" -auto-approve
   ```

### Troubleshooting

#### Local Google provider plugin failed to start
If `terraform validate` or `terraform plan` fails with a provider plugin error:
1.  **Verify `provider.tf`**: Ensure the file starts with the `terraform { required_providers { ... } }` block to explicitly define the Google provider source.
2. **Re-initialize with Upgrade**: Force Terraform to re-download and update the provider plugins:
    ```bash
    terraform init -upgrade
    ```

#### GPU Capacity Issues (Resource Availability)
If you get an error that `g2-standard-24` (or similar) is unavailable in your zone:
1.  **Find Alternative Zones**: Run these commands to see where the machine type and GPUs are supported:
    ```bash
    # List all zones supporting g2-standard-24
    gcloud compute machine-types list --filter="name=g2-standard-24" --format="table(zone, name)"

    # List all zones supporting NVIDIA L4 GPUs
    gcloud compute accelerator-types list --filter="name~nvidia-l4" --format="table(zone, name)"
    ```
2.  **Update Terraform**: Change the `zone` and `region` (if necessary) in `src/AudioBooks/Terraform/provider.tf` (or via `-var` flags) to one of the available zones found above.

If Terraform fails with `Quota 'NVIDIA_A100_GPUS' exceeded`, the VM is using an A2 machine type such as `a2-highgpu-1g`. Either request A100 quota for that region, or use the default G2/L4 Terraform configuration (`g2-standard-8`) to avoid the A100 quota path.

#### TODO:
Run backfill script to fix the missing images from the books that were skipped.
```commandline 
sqlite3 src/AudioBooks/Catalog/DB/gutenbergindex.db \
  "UPDATE book_content_backfill_queue SET status='pending', attempts=0 WHERE queue_key='html:v1' AND status='skipped';"
python -m AudioBooks.Catalog.Gutenberg.backfill_book_html --workers 8
```

Need to build voice map based on the profile data. For example based on the book the narrator reads, the voice profile should match that book category. 
Steps: (kokoro)
1. Text encoder - BERT
3. Encode the book as a director to cast the characters for novels or practical guies etc.
3. Need a voice map based on the book category and character profiles.
4. Build a prosody model to match the voice profile based on the book category.
Steps:
3. It includes:
* rhythm
* pauses
* emphasis
* pitch
* speaking style
* Verbs like running, walking, out of breath, laughing, crying, yelling, screaming, quarreling, arguing
* emotions - happy, sad, scared, anxious, fearful, excited, suspicious, skeptical, melancholy
* Tone of the voice - low, whisper, high, loud, soft
* direction of speech - fast, slow, slow-paced, pause (poignant), stutter

The ProsodyPredictor learns things like:
* speaking rate
* pauses
* rhythm
* pitch patterns
* emphasis
It does not necessarily define speaker identity.
Think of two people saying the same sentence:

Hello, how are you?

They may have:
* different voices (speaker identity)
* similar prosody (same rhythm and intonation)
or:
* the same voice
* different prosody (excited vs calm)
These are separate concepts.

Typical multi-speaker architecture
Usually you add a speaker embedding:
```
speaker_id
    ↓
speaker_embedding
    ↓
[Text Features + Speaker Features]
    ↓
Prosody / Decoder
```
For example:
```
self.speaker_embed = nn.Embedding(
    num_speakers=100,
    embedding_dim=128
)
```
Then:
```
speaker_vec = self.speaker_embed(speaker_id)


Voice map
A voice map is usually just a lookup table:

VOICE_MAP = {
    "alice": 0,
    "bob": 1,
    "charlie": 2,
}

User requests:

voice="alice"

You convert:

speaker_id = VOICE_MAP["alice"]
```
Then fetch:
```
speaker_embedding = self.speaker_embed(speaker_id)
```

Where would it go in Kokoro?
Looking at your snippet:
```
self.bert
self.bert_encoder
self.predictor
self.text_encoder

the speaker embedding would typically be injected:
Option A: Before prosody prediction

Text
 ↓
BERT
 ↓
TextEncoder
 ↓
+ Speaker Embedding
 ↓
ProsodyPredictor
```
This allows prosody to vary by speaker.
```
For example:

Speaker A → energetic
Speaker B → relaxed


Option B: Before waveform generation

Text Features
 ↓
ProsodyPredictor
 ↓
+ Speaker Embedding
 ↓
Decoder
```
This changes voice identity while leaving prosody mostly unchanged.
Many TTS systems do some combination of both.

If you're adding custom voices
You might create something like:
```
VOICE_MAP = {
    "af_bella": 0,
    "af_sarah": 1,
    "am_michael": 2,
}
```
and then:
```
speaker_id = VOICE_MAP[voice_name]
speaker_emb = self.speaker_embed(
    torch.tensor([speaker_id])
)
```
The embedding gets concatenated or added to the hidden representations:
```
hidden = hidden + speaker_emb.unsqueeze(1)

or

hidden = torch.cat(
    [hidden, speaker_emb.expand(...)],
    dim=-1
)
```

One important question: are you trying to:
1. Add a simple name→voice lookup for Kokoro's existing voices (e.g. af_bella, af_sky, etc.), or
2. Train entirely new speakers and modify the architecture to support multi-speaker embeddings?
Those require very different changes. The first is mostly a configuration problem; the second involves model architecture and training.


Train entirely new speakers and modify the architecture to support multi-speaker embeddings

If you're training entirely new speakers, then you're moving from a single-speaker (or fixed-voice) TTS model toward a multi-speaker TTS model. The key idea is that the model needs a way to separate:

What is being said?      ← text content
How is it being said?    ← prosody
Who is saying it?        ← speaker identity

```commandline
A more scalable design
Many modern TTS architectures use:

Text
 ↓
BERT
 ↓
Text Encoder
 ↓
Cross-Attention with Speaker Embedding
 ↓
Prosody Predictor
 ↓
Acoustic Decoder
 ↓
Vocoder
```


* I need this to be saved to later dramatize as a director the scenes to identify speakers and add emotions: crying, laughing, hysteria, ecstatic, etc., and verbs: running, sitting, walking, standing, out of breath, etc.
* Then I need to build a voice map and profiles of the books read by the narrator and attach that to the speaker


 Are there dates in the book? Is there a backdrop to the story we could glean to understand the book better - this could help with societal norms,  voice, tense, tone and emotions.
 When did the author write the book and release? Should this be used for context? Or is this fiction about some distant future? 

Or should I only use the profile the characters in the book and the era when the book was written to map to the speaker profile or avoid it?

Does the main character of the book match the narrator's voice for example Tom Sawyer is about a young boy who is a runaway slave. Also, location of th author matters.
