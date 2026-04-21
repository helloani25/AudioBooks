import argparse
import os
import ssl
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from os import path, listdir


try:
    from lxml import etree, html as lxml_html
except ImportError:
    etree = None
    lxml_html = None

import gutenbergpy.textget
from gutenbergpy.gutenbergcache import GutenbergCache
from gutenbergpy.parse.rdfparser import RdfParser
from gutenbergpy.parse.rdfparseresults import RDFParseResults
from gutenbergpy.parse.cachefields import Fields
from gutenbergpy.utils import Utils
from gutenbergpy.gutenbergcachesettings import GutenbergCacheSettings
from gutenbergpy.parse.book import Book


try:
    import chardet
except ImportError:
    chardet = None

DOWNLOAD_TYPES = [
    "text/plain",
    "text/plain; charset=utf-8",
    "text/plain; charset=us-ascii",
]

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR.parent / "DB"
DB_PATH = DB_DIR / "gutenbergindex.db"
GutenbergCacheSettings.set(
    CacheFilename=str(DB_PATH),
    CacheUnpackDir=str(DB_DIR / "cache" / "epub"),
    CacheArchiveName=str(DB_DIR / "rdf-files.tar.bz2"),
    TextFilesCacheFolder=str(DB_DIR / "texts"),
)

def _connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn

def _fetch_gutenberg_cache():
    if not GutenbergCache.exists():
        _monkeypatch_rdf_parser()
        # 1. Create/Refresh the cache (This downloads the massive RDF master file)
        # This will take a few minutes but gives you EVERY piece of metadata.
        # The cache is created via the GutenbergCache class, not the SQLiteCache instance.
        GutenbergCache.create()

        # 2. Define the fields you want to retrieve
        # LoCC (Library of Congress Class) is the gold standard for genre/subject depth.
        # Example: 'PR' is English Literature, 'PS' is American Literature.
    cache = GutenbergCache.get_cache()

    # 3. Query for books. Omit `subjects` to fetch across all subjects.
    results = cache.query(downloadtype=DOWNLOAD_TYPES)

    # Limit to first 10
    for book_id in results[:10]:
        # Use the ID to get the text or more meta-info
        text = gutenbergpy.textget.get_text_by_id(book_id)

        # SQLiteCache doesn't have a get_metadata method, so we'll use a native query to get the title
        title_query = f"SELECT titles.name FROM titles JOIN books ON titles.bookid = books.id WHERE books.gutenbergbookid = {book_id}"
        title_res = list(cache.native_query(title_query))
        title = title_res[0][0] if title_res else "Unknown"

        print(f"Retrieved Book ID: {book_id} Title: {title}")


def _monkeypatch_rdf_parser():
    """
    Monkeypatch RdfParser.do to skip directories that don't follow the pg{id}.rdf convention.
    Specifically fixes 'cache/epub/test/pgtest.rdf' error.
    """
    if etree is None:
        raise RuntimeError(
            "lxml is required for Project Gutenberg cache creation. "
            "The module can now be imported without it, but this code path still "
            "needs lxml in the active Python environment."
        )

    original_do = RdfParser.do

    @staticmethod
    def patched_do():
        result = RDFParseResults()
        result.field_sets = Fields.FIELD_COUNT * [None]
        from gutenbergpy.parse.parseitemtitles import ParseItemTitles
        from gutenbergpy.parse.parseitem import ParseItem
        from gutenbergpy.parse.parseitemfile import ParseItemFiles

        result.field_sets[Fields.TITLE]     = ParseItemTitles(xpath=['//dcterms:title/text()','//dcterms:alternative/text()'])
        result.field_sets[Fields.SUBJECT]   = ParseItem(xpath =['//dcterms:subject/rdf:Description/rdf:value/text()'])
        result.field_sets[Fields.TYPE]      = ParseItem(xpath =['//dcterms:type/rdf:Description/rdf:value/text()'])
        result.field_sets[Fields.LANGUAGE]  = ParseItem(xpath =['//dcterms:language/rdf:Description/rdf:value/text()'])
        result.field_sets[Fields.AUTHOR]    = ParseItem(xpath =['//dcterms:creator/pgterms:agent/pgterms:alias/text()','//dcterms:creator/pgterms:agent/pgterms:name/text()'])
        result.field_sets[Fields.BOOKSHELF] = ParseItem(xpath =['//pgterms:bookshelf/rdf:Description/rdf:value/text()'])
        result.field_sets[Fields.FILES]     = ParseItemFiles(xpath =['//dcterms:hasFormat'])
        result.field_sets[Fields.PUBLISHER] = ParseItem(xpath =['//dcterms:publisher/text()'])
        result.field_sets[Fields.RIGHTS]    = ParseItem( xpath =['//dcterms:rights/text()'])

        dirs = [d for d in listdir(GutenbergCacheSettings.CACHE_RDF_UNPACK_DIRECTORY) if not d.startswith("DELETE")]
        total = len(dirs)

        for idx, directory in enumerate(dirs):
            processing_str = "Processing progress: %d / %d" % (idx, total)
            Utils.update_progress_bar(processing_str, idx, total)

            file_path = path.join(GutenbergCacheSettings.CACHE_RDF_UNPACK_DIRECTORY, directory, 'pg%s.rdf' % (directory))

            # THE FIX: Check if file exists
            if not path.isfile(file_path):
                print(f"\nSkipping non-conforming directory: {directory} (expected {file_path})")
                continue

            try:
                doc = etree.parse(file_path, etree.ETCompatXMLParser())
            except Exception as e:
                print(f"\nFailed to parse {file_path}: {e}")
                continue

            res = Fields.FIELD_COUNT * [-1]
            for idx_field, pt in enumerate(result.field_sets):
                if not pt.needs_book_id():
                    res[idx_field] = pt.do(doc)
                else:
                    res[idx_field] = pt.do(doc, idx + 1)

            try:
                gutenberg_book_id = int(directory)
            except ValueError:
                print(f"\nSkipping non-numeric directory: {directory}")
                continue

            date_issued_x   = doc.xpath('//dcterms:issued/text()', namespaces=GutenbergCacheSettings.NS)
            num_downloads_x = doc.xpath('//pgterms:downloads/text()', namespaces=GutenbergCacheSettings.NS)

            date_issued       = '1000-10-10' if not date_issued_x or date_issued_x[0] =='None' else str(date_issued_x[0])
            num_downloads     =  -1 if not num_downloads_x else int(num_downloads_x[0])
            publisher_id      =  -1 if not res[Fields.PUBLISHER] else res[Fields.PUBLISHER][0]
            rights_id         =  -1 if not res[Fields.RIGHTS]    else res[Fields.RIGHTS][0]
            language_id       =  -1 if not res[Fields.LANGUAGE] else res[Fields.LANGUAGE][0]
            bookshelf_id      =  -1 if not res[Fields.BOOKSHELF] else res[Fields.BOOKSHELF][0]
            type_id           =  -1 if not  res[Fields.TYPE]    else  res[Fields.TYPE][0]

            newbook = Book(publisher_id, rights_id, language_id, bookshelf_id,
                           gutenberg_book_id, date_issued, num_downloads, res[Fields.TITLE],
                           res[Fields.SUBJECT], type_id, res[Fields.AUTHOR], res[Fields.FILES])

            result.books.append(newbook)

        return result

    RdfParser.do = patched_do


def _resolve_ca_paths(cli_cafile, cli_capath, verify=True):
    if not verify:
        return None, None
    cafile = (
        cli_cafile
        or os.getenv("GUTTENBERG_CA_BUNDLE")
        or os.getenv("SSL_CERT_FILE")
        or os.getenv("REQUESTS_CA_BUNDLE")
        or os.getenv("CURL_CA_BUNDLE")
    )

    # Use specified cafile only if it exists
    if cafile and not os.path.isfile(cafile):
        cafile = None

    # Try certifi as fallback if no CA bundle specified or found
    if not cafile:
        try:
            import certifi
            cafile = certifi.where()
        except ImportError:
            pass

    capath = cli_capath or os.getenv("SSL_CERT_DIR")

    if cafile and not os.path.isfile(cafile):
        raise FileNotFoundError(f"CA bundle not found: {cafile}")
    if capath and not os.path.isdir(capath):
        raise FileNotFoundError(f"CA directory not found: {capath}")
    return cafile, capath


def _install_https_opener(cafile, capath, verify=True):
    if not verify:
        context = ssl._create_unverified_context()
    else:
        context = ssl.create_default_context()
        if cafile or capath:
            context.load_verify_locations(cafile=cafile, capath=capath)

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    urllib.request.install_opener(opener)
    # Also override the global default context if possible, 
    # though install_opener should handle most urllib calls.
    ssl._create_default_https_context = lambda: context


def _build_arg_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ca-bundle", dest="cafile", help="Path to a PEM CA bundle file.")
    parser.add_argument("--ca-dir", dest="capath", help="Path to a directory of CA certificates.")
    parser.add_argument("--no-verify", action="store_false", dest="verify", default=True,
                        help="Disable SSL certificate verification.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--refresh-downloadlinks",
        action="store_true",
        help="Fetch Project Gutenberg /files/<id>/ index pages and backfill missing downloadlinks rows.",
    )
    mode.add_argument(
        "--repair-downloadlinks",
        action="store_true",
        help="Rebuild downloadlinks rows for the selected books from the live Project Gutenberg index.",
    )
    parser.add_argument(
        "--gutenberg-id",
        dest="gutenberg_ids",
        action="append",
        type=int,
        help="Limit refresh/repair to one or more Gutenberg ids.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent download-index fetch workers for refresh/repair.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit how many books are scanned when using refresh/repair.",
    )
    return parser


def main():
    parser = _build_arg_parser()
    args = parser.parse_args()
    cafile, capath = _resolve_ca_paths(args.cafile, args.capath, args.verify)
    _install_https_opener(cafile, capath, args.verify)
    _fetch_gutenberg_cache()

if __name__ == "__main__":
    try:
        main()
    except (ssl.SSLCertVerificationError, urllib.error.URLError) as exc:
        if isinstance(exc, ssl.SSLCertVerificationError) or (isinstance(exc, urllib.error.URLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc)):
             raise RuntimeError(
                "TLS verification failed. Provide a trusted CA bundle path via "
                "GUTTENBERG_CA_BUNDLE, SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE, "
                "or pass --ca-bundle/--ca-dir (and optionally SSL_CERT_DIR). "
                "Alternatively, use --no-verify to disable verification (use with caution)."
            ) from exc
        raise
