import argparse
import json
import sqlite3
from pathlib import Path


def _default_paths():
    repo_dir = Path(__file__).resolve().parents[1]
    db_path = repo_dir / "DB" / "gutenbergindex.db"
    out_path = repo_dir.parent / "Presentation" / "library" / "public" / "gutenberg-top.json"
    return db_path, out_path


def _extract_year(date_str: str | None) -> int | None:
    if not date_str:
        return None
    year_str = str(date_str).strip()[:4]
    if not year_str.isdigit():
        return None
    year = int(year_str)
    return None if year <= 1000 else year


def main():
    default_db, default_out = _default_paths()

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(default_db), help="Path to gutenbergindex.db")
    parser.add_argument("--out", default=str(default_out), help="Output JSON path")
    parser.add_argument("--limit", type=int, default=100, help="Number of rows to export")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            b.id,
            MIN(t.name) AS title,
            GROUP_CONCAT(DISTINCT a.name) AS authors,
            b.dateissued,
            l.name AS language,
            b.numdownloads,
            GROUP_CONCAT(DISTINCT s.name) AS subjects
        FROM books b
        LEFT JOIN titles t ON t.bookid = b.id
        LEFT JOIN book_authors ba ON ba.bookid = b.id
        LEFT JOIN authors a ON a.id = ba.authorid
        LEFT JOIN languages l ON l.id = b.languageid
        LEFT JOIN book_subjects bs ON bs.bookid = b.id
        LEFT JOIN subjects s ON s.id = bs.subjectid
        WHERE b.numdownloads IS NOT NULL AND b.numdownloads >= 0
        GROUP BY b.id
        ORDER BY b.numdownloads DESC
        LIMIT ?
        """,
        (args.limit,),
    )
    rows = cur.fetchall()
    conn.close()

    items = []
    for _, title, authors, dateissued, language, downloads, subjects in rows:
        items.append(
            {
                "title": title or "Untitled",
                "authors": authors or "Unknown",
                "year": _extract_year(dateissued),
                "language": language or "Unknown",
                "downloads": downloads or 0,
                "subjects": [s.strip() for s in subjects.split(",")] if subjects else [],
            }
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(items, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
