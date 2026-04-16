class Book:
    book_id: int
    title: str
    author_id: list[str]
    genre: str
    isbn: str
    images: list[str]
    description: str
    tags: list[str]
    publication_date: str
    publisher: str

    book_counter = 0

    def __init__(self, title, author, genre, isbn, images, description, tags, publication_date, publisher):
        self.book_id = Book.book_counter
        self.title = title
        self.author = author
        if isinstance(genre, str):
            self.genre = [genre]
        else:
            self.genre = genre
        self.isbn = isbn
        self.images = images
        self.description = description
        self.tags = tags
        self.publication_date = publication_date
        self.publisher = publisher
        Book.book_counter += 1
        
    def __repr__(self):
        return f"Book(book_id={self.book_id}, title='{self.title}', author='{self.author}', genre='{self.genre}', isbn='{self.isbn}', images={self.images}, description='{self.description}', tags={self.tags}, publication_date='{self.publication_date}', publisher='{self.publisher}')"


    def __eq__(self, other):  
        if not isinstance(other, Book):
            return False
        return self.book_id == other.book_id
    
    def __hash__(self):
        return hash((self.book_id * 37) % 31)

    def __str__(self):
        return f"Book: {self.title}"




