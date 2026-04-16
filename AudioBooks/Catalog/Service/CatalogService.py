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

@catalog_bp.route('/api/subjects', methods=['GET'])
def get_subjects():
    limit = request.args.get('limit', default=50, type=int)
    subjects = catalog_repo.get_subjects(limit=limit)
    return jsonify(subjects)
