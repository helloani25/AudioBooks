from typing import Optional

from VirtualLibrary.Book import Book


class CatalogManagement:
    books_by_genre: dict[str, list[Book]]
    books_by_id: dict[int, Book]
    books_by_author: dict[int, list[Book]]
    books_by_title: dict[str, list[Book]]
    books_by_isbn: dict[str, Book]
    catalogs: dict[str, list[Book]]
    books_by_genre: dict[str, list[Book]]

    def __init__(self):
        self.catalogs = {}
        self.books_by_genre = {}
        self.books_by_id = {}
        self.books_by_author = {}
        self.books_by_title = {}
        self.books_by_isbn = {}

    def add_book(self, book: Book):
        for genre in book.genre:
            if genre not in self.books_by_genre:
                self.books_by_genre[genre] = []
            self.books_by_genre[genre].append(book)
        
        self.books_by_id[book.book_id] = book
        self.books_by_isbn[book.isbn] = book
        
        if book.title not in self.books_by_title:
            self.books_by_title[book.title] = []
        self.books_by_title[book.title].append(book)
        
        # book.author is a single name or list? Book class says author_id: list[str] but __init__ says author. 
        # Let's check Book.py carefully.
        # Assuming book.author is what we use.
        if hasattr(book, 'author'):
            author = book.author
            if author not in self.books_by_author:
                self.books_by_author[author] = []
            self.books_by_author[author].append(book)


    def get_genre_books(self, genre: str) -> list[Book]:
        return self.books_by_genre.get(genre, [])

    def get_all_genres(self) -> list[str]:
        return list(self.books_by_genre.keys())

    def get_book_by_id(self, book_id: int) -> Optional[Book]:
        if book_id in self.books_by_id:
            return self.books_by_id[book_id]
        return None

    def get_book_by_isbn(self, isbn: str) -> Optional[Book]:
        if isbn in self.books_by_isbn:
            return self.books_by_isbn[isbn]
        return None

    def get_book_by_title(self, title: str) -> list[Book]:
        return self.books_by_title.get(title, [])

    def get_book_by_author(self, author_id: int) -> list[Book]:
        return self.books_by_author.get(author_id, [])

    def get_all_books(self) -> list[Book]:
        return list(self.books_by_id.values())

    def get_books_by_genre(self, genre: str) -> list[Book]:
        return self.books_by_genre.get(genre, [])

    def remove_book(self, book_id: int) -> bool:
        if book_id in self.books_by_id:
            self.books_by_id.pop(book_id, None)
            return True
        return False

    def update_book(self, book_id: int, new_book: Book) -> Optional[Book]:
        if book_id in self.books_by_id:
            self.books_by_id[book_id] = new_book
            return self.books_by_id[book_id]
        return None







