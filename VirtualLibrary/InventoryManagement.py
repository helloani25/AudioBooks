from collections import defaultdict
from enum import Enum
from typing import NamedTuple

from VirtualLibrary.CatalogManagement import CatalogManagement


class BookStatus(Enum):
    AVAILABLE = "AVAILABLE"
    RETURNED = "RETURNED"
    RESERVED = "RESERVED"
    LOST = "LOST"
    MISSING = "MISSING"
    HOLD = "HOLD"
    CHECKED_OUT = "CHECKED_OUT"
    UNKNOWN = "UNKNOWN"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    RESERVATION_CANCELED = "RESERVATION_CANCELED"

class BookCount(NamedTuple):
    Book_id: int
    Book_status: BookStatus

class InventoryManagement:
    book_collections: dict[BookCount, int]


    def __init__(self):
        self.book_collections = defaultdict(int)
        self.catalog_manager = CatalogManagement()
        for book_id in self.catalog_manager.books_by_id:
            self.book_collections[BookCount(book_id, BookStatus.AVAILABLE)] = 20
            self.book_collections[BookCount(book_id, BookStatus.RESERVED)] = 0
            self.book_collections[BookCount(book_id, BookStatus.LOST)] = 0
            self.book_collections[BookCount(book_id, BookStatus.MISSING)] = 0
            self.book_collections[BookCount(book_id, BookStatus.HOLD)] = 0
            self.book_collections[BookCount(book_id, BookStatus.CHECKED_OUT)] = 0
            self.book_collections[BookCount(book_id, BookStatus.UNKNOWN)] = 0


    def add_book(self, book_id: int, book_count: int)-> bool:
        if book_id in self.book_collections:
            return False
        self.book_collections[BookCount(book_id, BookStatus.AVAILABLE)] = book_count
        return True
    

    def remove_book(self, book_id: int)-> bool:
        if book_id in self.book_collections:
            self.book_collections.pop(BookCount(book_id, BookStatus.AVAILABLE), None)
            return True
        return False

    def update_book_status(self, book_id: int, new_status: BookStatus) -> bool:
        # If old_status is not provided, we might not know what to decrement
        # But for simplicity, if it's RESERVED -> CHECKED_OUT, we know to decrement RESERVED
        # If it's AVAILABLE -> RESERVED, we know to decrement AVAILABLE
        
        if new_status == BookStatus.RESERVED:
            self.book_collections[BookCount(book_id, BookStatus.AVAILABLE)] -= 1
            self.book_collections[BookCount(book_id, BookStatus.RESERVED)] += 1
        elif new_status == BookStatus.CHECKED_OUT:
            # Can be from AVAILABLE or RESERVED
            if self.book_collections[BookCount(book_id, BookStatus.RESERVED)] > 0:
                self.book_collections[BookCount(book_id, BookStatus.RESERVED)] -= 1
            else:
                self.book_collections[BookCount(book_id, BookStatus.AVAILABLE)] -= 1
            self.book_collections[BookCount(book_id, BookStatus.CHECKED_OUT)] += 1
        elif new_status == BookStatus.RETURNED:
            # Returning a book makes it AVAILABLE
            if self.book_collections[BookCount(book_id, BookStatus.CHECKED_OUT)] > 0:
                self.book_collections[BookCount(book_id, BookStatus.CHECKED_OUT)] -= 1
            self.book_collections[BookCount(book_id, BookStatus.AVAILABLE)] += 1
        elif new_status in [BookStatus.RESERVATION_EXPIRED, BookStatus.RESERVATION_CANCELED]:
            self.book_collections[BookCount(book_id, BookStatus.RESERVED)] -= 1
            self.book_collections[BookCount(book_id, BookStatus.AVAILABLE)] += 1
        return True

    def update_book_count(self, book_id: int, new_status: BookStatus) -> bool:
        return self.update_book_status(book_id, new_status)

    def get_availability_count(self, book_id: int) -> int:
        return self.book_collections.get(BookCount(book_id, BookStatus.AVAILABLE), 0)

    def get_reserved_count(self, book_id: int) -> int:
        return self.book_collections.get(BookCount(book_id, BookStatus.RESERVED), 0)

    def get_lost_count(self, book_id: int) -> int:
        return self.book_collections.get(BookCount(book_id, BookStatus.LOST), 0)

    def get_missing_count(self, book_id: int) -> int:
        return self.book_collections.get(BookCount(book_id, BookStatus.MISSING), 0)

    def get_book_status(self, book_id: int) -> list[BookStatus]:
        results = []
        for (bid, status), count in self.book_collections.items():
            if bid == book_id and count > 0:
                results.append(status)
        return results

    def get_hold_count(self, book_id: int) -> int:
        return self.book_collections.get(BookCount(book_id, BookStatus.HOLD), 0)


