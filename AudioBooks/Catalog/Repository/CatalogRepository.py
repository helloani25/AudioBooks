import html
import sqlite3
import json
import os
import re
from pathlib import Path

try:
    import redis
except ImportError:  # pragma: no cover - optional dependency
    redis = None

_SEARCH_STOP_WORDS = frozenset([
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or",
    "by", "from", "with", "as", "is", "it", "its", "be", "are", "was",
])

_DIGIT_TO_WORD = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}

class CatalogRepository:
    def __init__(self):

        current_dir = Path(__file__).resolve().parent
        db_path = str(current_dir.parent / "DB" / "gutenbergindex.db")
        self.db_path = db_path
        
        # Redis Caching Configuration
        redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
        self.redis = None
        self.use_redis = False
        if redis is not None:
            try:
                self.redis = redis.from_url(redis_url)
                self.redis.ping()
                self.use_redis = True
                print(f"Connected to Redis at {redis_url} for Catalog caching.")
            except Exception as e:
                self.redis = None
                print(f"WARNING: Redis not available for Catalog caching. Error: {e}")
        else:
            print("WARNING: Redis package not installed; using in-memory catalog cache.")
        
        self._books_cache = {}
        self._subjects_cache = {}
        self._ensure_audio_tables()

    def _tokenize_search(self, query: str) -> list[str]:
        """Split a search query into normalized tokens with digit→word expansion and stop-word removal."""
        raw = re.split(r'\W+', query.lower().strip())
        seen: set[str] = set()
        tokens: list[str] = []
        for tok in raw:
            if not tok or len(tok) < 2:
                continue
            tok = _DIGIT_TO_WORD.get(tok, tok)
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
        filtered = [t for t in tokens if t not in _SEARCH_STOP_WORDS]
        return filtered if filtered else tokens

    def _extract_year(self, date_str: str | None) -> int | None:
        if not date_str:
            return None
        year_str = str(date_str).strip()[:4]
        if not year_str.isdigit():
            return None
        year = int(year_str)
        return None if year <= 1000 else year

    def _is_valid_subject(self, s: str) -> bool:
        if not s:
            return False
        s = s.strip()
        # Filter out 2-letter words and non-word codes like E011
        # Keep if it contains space, dash, or comma (likely human-readable LCSH)
        if " " in s or "-" in s or "," in s:
            return True
        # Keep if it has more than 2 characters and contains no digits (likely common word)
        return len(s) > 2 and not any(char.isdigit() for char in s)

    def _normalize_match_text(self, value: str | None) -> str:
        if not value:
            return ""
        normalized = str(value).encode("ascii", errors="ignore").decode("ascii")
        normalized = re.sub(r"[^a-z0-9]+", " ", normalized.lower())
        return " ".join(normalized.split())

    def _token_set(self, value: str | None) -> set[str]:
        return {token for token in self._normalize_match_text(value).split() if len(token) > 2}

    def _text_matches(self, expected: str | None, candidate: str | None, *, threshold: float = 0.8) -> bool:
        expected_norm = self._normalize_match_text(expected)
        candidate_norm = self._normalize_match_text(candidate)
        if not expected_norm or not candidate_norm:
            return False
        if expected_norm == candidate_norm:
            return True
        if expected_norm in candidate_norm or candidate_norm in expected_norm:
            return True

        expected_tokens = self._token_set(expected)
        candidate_tokens = self._token_set(candidate)
        if not expected_tokens or not candidate_tokens:
            return False
        # Use min so that "Mark Twain" matching against "Twain, Mark|Clemens, Samuel..."
        # scores 2/2 = 1.0 rather than 2/5 = 0.4 (catalog author lists are always larger).
        overlap = len(expected_tokens & candidate_tokens) / min(len(expected_tokens), len(candidate_tokens))
        return overlap >= threshold

    def _load_catalog_book_metadata(self, book_id: int) -> dict | None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                b.id AS bookid,
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
                        ORDER BY a.name
                    )
                ), '') AS authors
            FROM books b
            WHERE b.id = ?
            """,
            (book_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "bookid": int(row["bookid"]),
            "gutenbergbookid": int(row["gutenbergbookid"]) if row["gutenbergbookid"] is not None else None,
            "dateissued": row["dateissued"],
            "title": row["title"] or "Untitled",
            "authors": row["authors"] or "",
        }

    def _catalog_description_payload(self, catalog: dict) -> dict:
        return {
            "bookid": catalog["bookid"],
            "wikipedia_id": None,
            "freebase_id": None,
            "source_title": catalog["title"] or "Untitled",
            "source_author": (catalog["authors"] or None).replace("|", ", ") if catalog.get("authors") else None,
            "publication_date": catalog.get("dateissued"),
            "genres_text": None,
            "genres_json": None,
            "summary": "Description not available for this title.",
            "source": "catalog",
            "download_date": None,
        }

    def _ensure_audio_tables(self) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("CREATE INDEX IF NOT EXISTS books_gutenbergbookid_idx ON books(gutenbergbookid)")
        cur.execute("CREATE INDEX IF NOT EXISTS downloadlinks_bookid_idx ON downloadlinks(bookid)")
        cur.execute("CREATE INDEX IF NOT EXISTS downloadlinks_downloadtypeid_idx ON downloadlinks(downloadtypeid)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS book_audio (
                book_id INTEGER PRIMARY KEY,
                package_url TEXT NOT NULL,
                audio_format TEXT NOT NULL,
                track_count INTEGER NOT NULL DEFAULT 0,
                is_chaptered INTEGER NOT NULL DEFAULT 0,
                download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS book_audio_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                track_order INTEGER NOT NULL,
                chapter_title TEXT,
                track_url TEXT NOT NULL,
                audio_format TEXT NOT NULL,
                download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(book_id, track_order, track_url)
            )
            """
        )
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS book_cover_art (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bookid INTEGER NOT NULL,
                size_label TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                image_url TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                byte_size INTEGER,
                rdf_url TEXT,
                source TEXT NOT NULL DEFAULT 'gutenberg-rdf',
                download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(bookid, size_label, image_url)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_book_audio_chapters_book_id ON book_audio_chapters(book_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_book_desc_wikipedia_id ON book_desc(wikipedia_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_book_cover_art_bookid ON book_cover_art(bookid)")
        conn.commit()
        conn.close()

    def get_books_count(self, subject: str = None, search: str = None) -> int:
        cache_key = f"catalog:count:v2:{subject or ''}:{search or ''}"
        if self.use_redis:
            cached_val = self.redis.get(cache_key)
            if cached_val is not None:
                return int(cached_val)
        elif cache_key in self._books_cache:
            return self._books_cache[cache_key]

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        query = "SELECT COUNT(*) FROM books b"
        conditions = []
        params = []

        if subject:
            conditions.append("b.id IN (SELECT bookid FROM book_subjects bs2 JOIN subjects s2 ON s2.id = bs2.subjectid WHERE s2.name = ?)")
            params.append(subject)

        if search:
            for token in self._tokenize_search(search):
                like = f"%{token}%"
                conditions.append("""
                    (b.id IN (SELECT bookid FROM titles WHERE LOWER(name) LIKE ?)
                     OR b.id IN (SELECT bookid FROM book_authors ba3 JOIN authors a3 ON a3.id = ba3.authorid WHERE LOWER(a3.name) LIKE ?))
                """)
                params.extend([like, like])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        cur.execute(query, params)
        count = cur.fetchone()[0]
        conn.close()

        if self.use_redis:
            self.redis.setex(cache_key, 3600, count)
        else:
            self._books_cache[cache_key] = count
        return count

    def get_books(self, subject: str = None, search: str = None, limit: int = 100, offset: int = 0):
        cache_key = f"catalog:books:v2:{subject or ''}:{search or ''}:{limit}:{offset}"
        if self.use_redis:
            cached_val = self.redis.get(cache_key)
            if cached_val is not None:
                return json.loads(cached_val)
        elif cache_key in self._books_cache:
            return self._books_cache[cache_key]

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        tokens = self._tokenize_search(search) if search else []

        # Build a per-token title-match score expression for ranking.
        score_cases = " + ".join(
            f"CASE WHEN LOWER((SELECT name FROM titles WHERE bookid = b.id LIMIT 1)) LIKE ? THEN 1 ELSE 0 END"
            for _ in tokens
        ) or "0"
        score_params = [f"%{t}%" for t in tokens]

        query = f"""
            SELECT
                b.id,
                (SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1) AS title,
                (SELECT GROUP_CONCAT(name, '|') FROM (SELECT DISTINCT a2.name as name FROM authors a2 JOIN book_authors ba2 ON ba2.authorid = a2.id WHERE ba2.bookid = b.id)) AS authors,
                b.dateissued,
                l.name AS language,
                b.numdownloads,
                (SELECT GROUP_CONCAT(name, '|') FROM (SELECT DISTINCT s2.name as name FROM subjects s2 JOIN book_subjects bs2 ON bs2.subjectid = s2.id WHERE bs2.bookid = b.id)) AS subjects,
                ({score_cases}) AS search_score
            FROM books b
            LEFT JOIN languages l ON l.id = b.languageid
        """

        conditions = []
        params = list(score_params)

        if subject:
            conditions.append("b.id IN (SELECT bookid FROM book_subjects bs2 JOIN subjects s2 ON s2.id = bs2.subjectid WHERE s2.name = ?)")
            params.append(subject)

        for token in tokens:
            like = f"%{token}%"
            conditions.append("""
                (b.id IN (SELECT bookid FROM titles WHERE LOWER(name) LIKE ?)
                 OR b.id IN (SELECT bookid FROM book_authors ba3 JOIN authors a3 ON a3.id = ba3.authorid WHERE LOWER(a3.name) LIKE ?))
            """)
            params.extend([like, like])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        order_by = "search_score DESC, b.numdownloads DESC" if tokens else "b.numdownloads DESC"
        query += f" ORDER BY {order_by} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        items = []
        for row in rows:
            subjects = []
            if row["subjects"]:
                # Filter out subjects that are 2 characters or shorter or are non-word codes (e.g., E011, PR)
                subjects = [s.strip() for s in row["subjects"].split("|") if self._is_valid_subject(s)]
            
            items.append({
                "id": row["id"],
                "title": row["title"] or "Untitled",
                "authors": (row["authors"] or "Unknown").replace('|', ', '),
                "year": self._extract_year(row["dateissued"]),
                "language": row["language"] or "Unknown",
                "downloads": row["numdownloads"] or 0,
                "subjects": subjects,
            })
        
        if self.use_redis:
            self.redis.setex(cache_key, 3600, json.dumps(items))
        else:
            self._books_cache[cache_key] = items
        return items

    def get_subjects(self, limit: int = 50):
        # Cache the ranked subject list separately from the book list caches.
        cache_key = f"catalog:subjects:top_counts_v1:{limit}"
        if self.use_redis:
            cached_val = self.redis.get(cache_key)
            if cached_val is not None:
                result = json.loads(cached_val)
                return result[:limit]
        elif cache_key in self._subjects_cache:
            return self._subjects_cache[cache_key][:limit]

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                s.name,
                COUNT(DISTINCT bs.bookid) AS book_count
            FROM book_subjects bs
            JOIN subjects s ON s.id = bs.subjectid
            WHERE s.name IS NOT NULL
            GROUP BY s.id, s.name
            ORDER BY book_count DESC, s.name ASC
            """
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return []

        result = [
            {"name": name, "count": int(book_count)}
            for name, book_count in rows
            if self._is_valid_subject(name)
        ]
        result = result[:limit]
        
        if self.use_redis:
            self.redis.setex(cache_key, 3600, json.dumps(result))
        else:
            self._subjects_cache[cache_key] = result

        return result[:limit]

    def save_book_content(self, book_id: int, raw_content: bytes, clean_content: bytes):
        """Save raw and clean book content to the database."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # Convert bytes to string if needed
        raw_text = raw_content.decode('utf-8', errors='ignore') if isinstance(raw_content, bytes) else raw_content
        clean_text = clean_content.decode('utf-8', errors='ignore') if isinstance(clean_content, bytes) else clean_content

        # Use INSERT OR REPLACE to handle duplicates
        cur.execute("""
            INSERT OR REPLACE INTO book_contents (bookid, raw_content, clean_content, download_date)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (book_id, raw_text, clean_text))

        conn.commit()
        conn.close()

    def get_book_content(self, book_id: int):
        """Retrieve book content from the database."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT bookid, raw_content, clean_content, download_date FROM book_contents WHERE bookid = ?", (book_id,))
        row = cur.fetchone()
        conn.close()

        if row:
            return {
                "bookid": row["bookid"],
                "raw_content": row["raw_content"],
                "clean_content": row["clean_content"],
                "download_date": row["download_date"]
            }
        return None

    def save_book_audio(
        self,
        book_id: int,
        package_url: str,
        audio_format: str,
        track_count: int,
        is_chaptered: bool = False,
    ) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO book_audio (
                book_id,
                package_url,
                audio_format,
                track_count,
                is_chaptered,
                download_date
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (book_id, package_url, audio_format, track_count, 1 if is_chaptered else 0),
        )
        conn.commit()
        conn.close()

    def save_book_audio_chapters(self, book_id: int, chapters: list[dict]) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("DELETE FROM book_audio_chapters WHERE book_id = ?", (book_id,))
        for chapter in chapters:
            cur.execute(
                """
                INSERT OR REPLACE INTO book_audio_chapters (
                    book_id,
                    track_order,
                    chapter_title,
                    track_url,
                    audio_format,
                    download_date
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    book_id,
                    chapter["track_order"],
                    chapter.get("chapter_title"),
                    chapter["track_url"],
                    chapter["audio_format"],
                ),
            )
        conn.commit()
        conn.close()

    def get_book_audio(self, book_id: int):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT book_id, package_url, audio_format, track_count, is_chaptered,
                   narrator, narrator_source, is_synthesized, download_date
            FROM book_audio WHERE book_id = ?
            """,
            (book_id,),
        )
        summary = cur.fetchone()
        if not summary:
            conn.close()
            return None

        cur.execute(
            """
            SELECT book_id, track_order, chapter_title, track_url, audio_format, duration, download_date
            FROM book_audio_chapters
            WHERE book_id = ?
            ORDER BY track_order, id
            """,
            (book_id,),
        )
        chapters = [dict(row) for row in cur.fetchall()]
        conn.close()
        return {
            "book_id": summary["book_id"],
            "package_url": summary["package_url"],
            "audio_format": summary["audio_format"],
            "track_count": summary["track_count"],
            "is_chaptered": bool(summary["is_chaptered"]),
            "narrator": summary["narrator"],
            "narrator_source": summary["narrator_source"],
            "is_synthesized": bool(summary["is_synthesized"]),
            "download_date": summary["download_date"],
            "chapters": chapters,
        }

    def save_book_desc(
        self,
        book_id: int,
        wikipedia_id: int | None,
        freebase_id: str | None,
        source_title: str,
        source_author: str | None,
        publication_date: str | None,
        genres_text: str | None,
        genres_json: str | None,
        summary: str,
        source: str = "cmu-book-summaries",
    ) -> None:
        conn = sqlite3.connect(self.db_path)
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                book_id,
                wikipedia_id,
                freebase_id,
                source_title,
                source_author,
                publication_date,
                genres_text,
                genres_json,
                summary,
                source,
            ),
        )
        conn.commit()
        conn.close()

    def get_book_description(self, book_id: int):
        catalog = self._load_catalog_book_metadata(book_id)
        if catalog is None:
            return None

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
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
            FROM book_desc
            WHERE bookid = ?
            """,
            (book_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            row_dict = dict(row)
            if row_dict.get("source_author"):
                row_dict["source_author"] = str(row_dict["source_author"]).replace("|", ", ")
            if row_dict.get("summary"):
                row_dict["summary"] = html.unescape(row_dict["summary"])
            if self._text_matches(catalog["title"], row_dict.get("source_title")) and (
                not row_dict.get("source_author")
                or self._text_matches(catalog["authors"], row_dict.get("source_author"), threshold=0.5)
            ):
                return row_dict

        return self._catalog_description_payload(catalog)

    def get_book_desc(self, book_id: int):
        return self.get_book_description(book_id)

    def get_book_cover_art(self, book_id: int):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                bca.bookid,
                bca.size_label,
                bca.sort_order,
                bca.image_url,
                bca.mime_type,
                bca.byte_size,
                bca.rdf_url,
                bca.source,
                bca.download_date
            FROM book_cover_art bca
            JOIN books b ON b.gutenbergbookid = bca.bookid
            WHERE b.id = ?
            ORDER BY bca.sort_order, COALESCE(bca.byte_size, 0) DESC, bca.size_label, bca.image_url
            """,
            (book_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
        return rows
