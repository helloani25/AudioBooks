from collections import defaultdict

from VirtualLibrary.CatalogManagement import CatalogManagement
from dataclasses import dataclass


@dataclass
class Review:
    review_id: int
    review: str
    rating: float
    book_id: int


class BookReviewManager:
    book_reviews_dict: dict[int, list[Review]]
    catalog_manager: CatalogManagement

    def __init__(self, catalog_manager: CatalogManagement = None):
        self.book_reviews_dict = defaultdict(list)
        self.catalog_manager = catalog_manager if catalog_manager else CatalogManagement()

    def add_review(self, book_id: int, rating: float, review: str):
        try:
            book = self.catalog_manager.get_book_by_id(book_id)
            if not book:
                raise ValueError(f"Book with ID {book_id} not found")
            num_reviews = len(self.book_reviews_dict[book_id]) + 1
            new_review = Review(review_id=num_reviews,
                                review=review,
                                rating=rating,
                                book_id=book_id)
            self.book_reviews_dict[book_id].append(new_review)
        except ValueError as e:
            print(f"Error adding review for Book Id {book_id}: {e}")

    def get_reviews(self, book_id: int) -> list[Review]:
        return self.book_reviews_dict.get(book_id, [])

    def get_average_rating(self, book_id: int) -> float:
        reviews = self.get_reviews(book_id)
        if not reviews:
            return 0.0
        return sum(review.rating for review in reviews) / len(reviews)

    def remove_review(self, book_id: int, review_id: int) -> bool:
        if book_id in self.book_reviews_dict:
            for review in self.book_reviews_dict[book_id]:
                if review.review_id == review_id:
                    self.book_reviews_dict[book_id].remove(review)
                    return True
        return False

    def remove_all_reviews(self, book_id: int) -> bool:
        if book_id in self.book_reviews_dict:
            del self.book_reviews_dict[book_id]
            return True
        return False
