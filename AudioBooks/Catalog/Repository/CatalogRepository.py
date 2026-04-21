import sqlite3
import redis
import json
import os
from pathlib import Path

class CatalogRepository:
    def __init__(self):

        current_dir = Path(__file__).resolve().parent
        db_path = str(current_dir.parent / "DB" / "gutenbergindex.db")
        self.db_path = db_path
        
        # Redis Caching Configuration
        redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
        self.redis = redis.from_url(redis_url)
        self.use_redis = False
        try:
            self.redis.ping()
            self.use_redis = True
            print(f"Connected to Redis at {redis_url} for Catalog caching.")
        except Exception as e:
            print(f"WARNING: Redis not available for Catalog caching. Error: {e}")
        
        self._books_cache = {}
        self._subjects_cache = {}
        self._ensure_audio_tables()

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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_book_audio_chapters_book_id ON book_audio_chapters(book_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_book_desc_wikipedia_id ON book_desc(wikipedia_id)")
        conn.commit()
        conn.close()

    def get_books_count(self, subject: str = None, search: str = None) -> int:
        cache_key = f"catalog:count:{subject or ''}:{search or ''}"
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
            conditions.append("""
                (b.id IN (SELECT bookid FROM titles WHERE name LIKE ?) 
                 OR b.id IN (SELECT bookid FROM book_authors ba3 JOIN authors a3 ON a3.id = ba3.authorid WHERE a3.name LIKE ?))
            """)
            params.append(f"%{search}%")
            params.append(f"%{search}%")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        cur.execute(query, params)
        count = cur.fetchone()[0]
        conn.close()
        
        if self.use_redis:
            self.redis.setex(cache_key, 3600, count) # Cache for 1 hour
        else:
            self._books_cache[cache_key] = count
        return count

    def get_books(self, subject: str = None, search: str = None, limit: int = 100, offset: int = 0):
        cache_key = f"catalog:books:{subject or ''}:{search or ''}:{limit}:{offset}"
        if self.use_redis:
            cached_val = self.redis.get(cache_key)
            if cached_val is not None:
                return json.loads(cached_val)
        elif cache_key in self._books_cache:
            return self._books_cache[cache_key]

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Build subqueries for authors and subjects using '|' separator to handle commas in names
        query = """
            SELECT 
                b.id,
                (SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1) AS title,
                (SELECT GROUP_CONCAT(name, '|') FROM (SELECT DISTINCT a2.name as name FROM authors a2 JOIN book_authors ba2 ON ba2.authorid = a2.id WHERE ba2.bookid = b.id)) AS authors,
                b.dateissued,
                l.name AS language,
                b.numdownloads,
                (SELECT GROUP_CONCAT(name, '|') FROM (SELECT DISTINCT s2.name as name FROM subjects s2 JOIN book_subjects bs2 ON bs2.subjectid = s2.id WHERE bs2.bookid = b.id)) AS subjects
            FROM books b
            LEFT JOIN languages l ON l.id = b.languageid
        """

        conditions = []
        params = []

        if subject:
            conditions.append("b.id IN (SELECT bookid FROM book_subjects bs2 JOIN subjects s2 ON s2.id = bs2.subjectid WHERE s2.name = ?)")
            params.append(subject)

        if search:
            # When searching, we need subqueries to avoid complex joins and cartesian product issues
            conditions.append("""
                (b.id IN (SELECT bookid FROM titles WHERE name LIKE ?) 
                 OR b.id IN (SELECT bookid FROM book_authors ba3 JOIN authors a3 ON a3.id = ba3.authorid WHERE a3.name LIKE ?))
            """)
            params.append(f"%{search}%")
            params.append(f"%{search}%")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY b.numdownloads DESC LIMIT ? OFFSET ?"
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
        # Filtered set cover results are cached with a specific key
        cache_key = f"catalog:subjects:set_cover_v2"
        if self.use_redis:
            cached_val = self.redis.get(cache_key)
            if cached_val is not None:
                result = json.loads(cached_val)
                return result[:limit]
        elif cache_key in self._subjects_cache:
            return self._subjects_cache[cache_key][:limit]

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        # 1. Fetch all book-subject associations and subject names
        cur.execute("SELECT bs.bookid, bs.subjectid, s.name FROM book_subjects bs JOIN subjects s ON s.id = bs.subjectid")
        subject_to_books = {}
        all_books_with_subjects = set()
        for bookid, sid, name in cur.fetchall():
            if not self._is_valid_subject(name):
                continue
            all_books_with_subjects.add(bookid)
            if sid not in subject_to_books:
                subject_to_books[sid] = set()
            subject_to_books[sid].add(bookid)
            
        # 2. Greedy set cover
        covered_books = set()
        selected_subject_ids = []
        remaining_subjects = {sid: books for sid, books in subject_to_books.items()}
        
        while covered_books < all_books_with_subjects:
            best_sid = -1
            max_new_coverage = 0
            for sid, books in remaining_subjects.items():
                new_coverage = len(books - covered_books)
                if new_coverage > max_new_coverage:
                    max_new_coverage = new_coverage
                    best_sid = sid
            
            if best_sid == -1: break
            
            selected_subject_ids.append(best_sid)
            covered_books.update(remaining_subjects[best_sid])
            del remaining_subjects[best_sid]
            
        # 3. Get metadata for selected subjects
        if not selected_subject_ids:
            return []
            
        placeholders = ','.join(['?'] * len(selected_subject_ids))
        cur.execute(f"SELECT id, name FROM subjects WHERE id IN ({placeholders})", selected_subject_ids)
        id_to_name = {row[0]: row[1] for row in cur.fetchall()}
        
        # Also get counts for these subjects
        cur.execute(f"SELECT subjectid, COUNT(bookid) FROM book_subjects WHERE subjectid IN ({placeholders}) GROUP BY subjectid", selected_subject_ids)
        id_to_count = {row[0]: row[1] for row in cur.fetchall()}
        conn.close()
        
        # Order by the greedy selection order (importance for coverage)
        result = [
            {"name": id_to_name[sid], "count": id_to_count.get(sid, 0)}
            for sid in selected_subject_ids if sid in id_to_name
        ]
        
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
            "SELECT book_id, package_url, audio_format, track_count, is_chaptered, download_date FROM book_audio WHERE book_id = ?",
            (book_id,),
        )
        summary = cur.fetchone()
        if not summary:
            conn.close()
            return None

        cur.execute(
            """
            SELECT book_id, track_order, chapter_title, track_url, audio_format, download_date
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

    def get_book_desc(self, book_id: int):
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
        return dict(row) if row else None
