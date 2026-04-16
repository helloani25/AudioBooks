class Author:
    author_id: int
    name: str
    bio: str
    books: list[str]
    genre: list[str]
    tags: list[str]
    profile_picture: str

    def __init__(self, name: str, bio: str, language: list[str], books: list[str], genre: list[str], tags: list[str]):
        self.name = name
        self.bio = bio
        self.language = language
        self.books = books
        self.genre = genre
        self.tags = tags
        self.profile_picture = None

    def __repr__(self):
        return f"Author(name='{self.name}', bio='{self.bio}', language={self.language}, books={self.books}, genre={self.genre}, tags={self.tags})"

    def __eq__(self, other):
        if not isinstance(other, Author):
            return False
        return self.name == other.name and self.author_id == other.author_id

    def __hash__(self):
        return hash(self.name, self.author_id)

    def __str__(self):
        return f"Author: {self.name}"

    def add_book(self, book_id: int):
        self.books.append(book_id)

    def remove_book(self, book_id: int):
        self.books.remove(book_id)

    def get_books(self):
        return self.books

    def add_profile_picture(self, profile_picture: str):
        self.profile_picture = profile_picture

