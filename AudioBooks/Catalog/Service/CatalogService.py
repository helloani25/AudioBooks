from flask import Blueprint, request, jsonify
from AudioBooks.Catalog.Repository.CatalogRepository import CatalogRepository

catalog_bp = Blueprint('catalog', __name__)
catalog_repo = CatalogRepository()

@catalog_bp.route('/api/books', methods=['GET'])
def get_books():
    subject = request.args.get('subject')
    search = request.args.get('search')
    limit = request.args.get('limit', default=20, type=int)
    offset = request.args.get('offset', default=0, type=int)
    
    books = catalog_repo.get_books(subject=subject, search=search, limit=limit, offset=offset)
    total = catalog_repo.get_books_count(subject=subject, search=search)
    
    return jsonify({
        'books': books,
        'total': total,
        'limit': limit,
        'offset': offset
    })

@catalog_bp.route('/api/books/count', methods=['GET'])
def get_books_count():
    subject = request.args.get('subject')
    search = request.args.get('search')
    total = catalog_repo.get_books_count(subject=subject, search=search)
    return jsonify({'total': total})

@catalog_bp.route('/api/books/<int:book_id>/description', methods=['GET'])
def get_book_description(book_id: int):
    description = catalog_repo.get_book_description(book_id)
    if description is None:
        return jsonify({'error': 'Book description not found'}), 404
    return jsonify(description)

@catalog_bp.route('/api/books/<int:book_id>/content', methods=['GET'])
def get_book_content(book_id: int):
    content = catalog_repo.get_book_content(book_id)
    if content is None:
        return jsonify({'error': 'Book content not found'}), 404
    return jsonify(content)

@catalog_bp.route('/api/books/<int:book_id>/audio', methods=['GET'])
def get_book_audio(book_id: int):
    audio = catalog_repo.get_book_audio(book_id)
    if audio is None:
        return jsonify({'error': 'Book audio not found'}), 404
    return jsonify(audio)

@catalog_bp.route('/api/books/<int:book_id>/cover-art', methods=['GET'])
def get_book_cover_art(book_id: int):
    cover_art = catalog_repo.get_book_cover_art(book_id)
    return jsonify({
        'book_id': book_id,
        'covers': cover_art,
    })

@catalog_bp.route('/api/subjects', methods=['GET'])
def get_subjects():
    limit = request.args.get('limit', default=50, type=int)
    subjects = catalog_repo.get_subjects(limit=limit)
    return jsonify(subjects)
