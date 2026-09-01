from AudioBooks.Catalog.Repository.CatalogRepository import CatalogRepository


def test_pagination():
    repo = CatalogRepository()
    limit = 5

    # Page 1
    books1 = repo.get_books(limit=limit, offset=0)
    # Page 2
    books2 = repo.get_books(limit=limit, offset=limit)

    assert len(books1) == limit
    assert len(books2) == limit

    # Check that they are different
    ids1 = {book['id'] for book in books1}
    ids2 = {book['id'] for book in books2}
    assert ids1.isdisjoint(ids2)


if __name__ == '__main__':
    test_pagination()
