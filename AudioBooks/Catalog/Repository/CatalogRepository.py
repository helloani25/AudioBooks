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