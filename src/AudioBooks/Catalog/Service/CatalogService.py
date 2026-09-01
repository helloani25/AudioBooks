import datetime
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Blueprint, request, jsonify, redirect

load_dotenv()

try:
    from google.cloud import storage as gcs_storage
    _HAS_GCS = True
except ImportError:
    _HAS_GCS = False

from AudioBooks.Catalog.Repository.CatalogRepository import CatalogRepository

catalog_bp = Blueprint('catalog', __name__)
catalog_repo = CatalogRepository()

_GCS_BUCKET = os.environ.get("GCS_BUCKET")
_GCS_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
_GCS_PREFIX = "book-html"
_SIGNED_URL_TTL = datetime.timedelta(hours=1)

# Lazily initialised GCS client shared across all requests in the process.
# Lazy init avoids a startup failure when GCS credentials are not yet configured.
_gcs_client = None


def _resolve_credentials_path(path_value: str | None) -> str | None:
    if not path_value:
        return None
    raw = Path(path_value).expanduser()
    candidates = [raw]
    if not raw.is_absolute():
        repo_root = Path(__file__).resolve().parents[3]
        audiobooks_root = Path(__file__).resolve().parents[2]
        candidates.append(audiobooks_root / raw)
        candidates.append(repo_root / raw)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(raw)


def _get_gcs_client():
    # Reuse a single GCS client for the lifetime of the process; creating one
    # per request would add ~100 ms of auth overhead to every image redirect.
    global _gcs_client
    if _gcs_client is not None:
        return _gcs_client
    if not _HAS_GCS:
        return None
    try:
        credentials_path = _resolve_credentials_path(_GCS_CREDENTIALS)
        if credentials_path:
            from google.oauth2 import service_account as _sa
            creds = _sa.Credentials.from_service_account_file(credentials_path)
            _gcs_client = gcs_storage.Client(credentials=creds)
        else:
            _gcs_client = gcs_storage.Client()
    except Exception:
        _gcs_client = None
    return _gcs_client


@catalog_bp.route('/api/books', methods=['GET'])
def get_books():
    # Paginated book list. Supports filtering by subject and keyword search.
    # When a search term produces zero results we return popular books as a
    # fallback so the home page never shows an empty grid.
    subject = request.args.get('subject')
    search = request.args.get('search')
    limit = request.args.get('limit', default=20, type=int)
    offset = request.args.get('offset', default=0, type=int)

    books = catalog_repo.get_books(subject=subject, search=search, limit=limit, offset=offset)
    total = catalog_repo.get_books_count(subject=subject, search=search)
    no_match_message = None
    fallback_books = []
    fallback_total = 0

    if search and total == 0:
        no_match_message = 'No match for the keyword'
        fallback_books = catalog_repo.get_books(limit=limit, offset=offset)
        fallback_total = catalog_repo.get_books_count()

    return jsonify({
        'books': books,
        'total': total,
        'limit': limit,
        'offset': offset,
        'no_match_message': no_match_message,
        'fallback_books': fallback_books,
        'fallback_total': fallback_total,
    })


@catalog_bp.route('/api/books/count', methods=['GET'])
def get_books_count():
    # Separate count endpoint used by the frontend to compute pagination controls
    # without re-fetching the full book list.
    subject = request.args.get('subject')
    search = request.args.get('search')
    total = catalog_repo.get_books_count(subject=subject, search=search)
    return jsonify({'total': total})


@catalog_bp.route('/api/books/<int:book_id>/description', methods=['GET'])
def get_book_description(book_id: int):
    # Returns summary, author, genres, and publication date from book_desc.
    # Falls back to minimal catalog metadata (title + author from books/titles
    # tables) when no enriched description row exists yet, so the book page
    # always has something to display even before backfills have run.
    description = catalog_repo.get_book_description(book_id)
    if description is None:
        return jsonify({'error': 'Book description not found'}), 404
    return jsonify(description)


@catalog_bp.route('/api/books/<int:book_id>/content', methods=['GET'])
def get_book_content(book_id: int):
    # Returns content_type alongside the text so the reader can decide whether to
    # split plain text into chapters (content_type='text') or render the HTML
    # edition directly (content_type='html'). The HTML edition is necessary for
    # illustrated books — ~75k Gutenberg titles include scanned photographs, maps,
    # and engravings that are only present in the -h.zip HTML archive, not in the
    # plain-text file that the HF dataset and backfill_missing_book_contents use.
    content = catalog_repo.get_book_content(book_id)
    if content is None:
        return jsonify({'error': 'Book content not found'}), 404
    return jsonify(content)


@catalog_bp.route('/api/books/<int:book_id>/audio', methods=['GET'])
def get_book_audio(book_id: int):
    # Returns the audio package URL, format, narrator metadata, and the ordered
    # list of chapter tracks. Populated by backfill_audio.py from Gutenberg
    # download links and LibriVox. Returns 404 when no audio exists so the
    # frontend knows to hide the Listen tab.
    audio = catalog_repo.get_book_audio(book_id)
    if audio is None:
        return jsonify({'error': 'Book audio not found'}), 404
    return jsonify(audio)


@catalog_bp.route('/api/books/<int:book_id>/cover-art', methods=['GET'])
def get_book_cover_art(book_id: int):
    # Returns all cover image URLs for the book, ordered by sort_order and size.
    # Populated by backfill_cover_art.py from Gutenberg RDF metadata. Returns
    # an empty covers list (not 404) when no art exists so the frontend can
    # fall back to the default SVG placeholder without an error branch.
    cover_art = catalog_repo.get_book_cover_art(book_id)
    return jsonify({
        'book_id': book_id,
        'covers': cover_art,
    })


@catalog_bp.route('/api/subjects', methods=['GET'])
def get_subjects():
    # Returns the top N subjects ranked by book count for the category dropdown.
    # Short-lived results are cached in Redis (or in-process) by the repository.
    limit = request.args.get('limit', default=50, type=int)
    subjects = catalog_repo.get_subjects(limit=limit)
    return jsonify(subjects)


@catalog_bp.route('/api/books/<int:book_id>/images/<path:filename>', methods=['GET'])
def get_book_image(book_id: int, filename: str):
    """Serve a book illustration via a short-lived signed GCS URL (302 redirect).

    Image source: Gutenberg's -h.zip archive (downloadlinks type 8) contains the
    original scanned illustrations from the book's first publication — engravings,
    photographs, maps, diagrams. These were digitised by Project Gutenberg volunteers
    and are in the public domain along with the text.

    We can't link to gutenberg.org/files/... directly (their deep-linking policy).
    Instead, backfill_book_html.py copies the images to gs://gutenberg-books/book-html/
    during the catalog backfill, and the clean HTML stored in book_contents has image
    paths rewritten to this endpoint. The 302 redirect keeps image bytes out of the
    Flask process — the browser fetches from GCS after the redirect.
    """
    gutenberg_id = catalog_repo.get_book_gutenberg_id(book_id)
    if gutenberg_id is None:
        return jsonify({'error': 'Book not found'}), 404

    client = _get_gcs_client()
    if client is None:
        # GCS unavailable — return a transparent 1×1 PNG so the page still renders.
        # The browser shows a broken-image placeholder; the text content is unaffected.
        return (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
            b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82',
            200,
            {'Content-Type': 'image/png', 'Cache-Control': 'no-store'},
        )

    blob_path = f"{_GCS_PREFIX}/{gutenberg_id}/images/{filename}"
    try:
        blob = client.bucket(_GCS_BUCKET).blob(blob_path)
        signed_url = blob.generate_signed_url(
            expiration=_SIGNED_URL_TTL,
            method="GET",
            version="v4",
        )
        return redirect(signed_url, code=302)
    except Exception:
        # Signing failed — return transparent placeholder so layout is preserved.
        return (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
            b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82',
            200,
            {'Content-Type': 'image/png', 'Cache-Control': 'no-store'},
        )
