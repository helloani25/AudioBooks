from dataclasses import dataclass

@dataclass
class Borrower:
    name: str
    age: int
    categories: list[str]
    is_blocked: bool
    fine: float

class BorrowerManager:

    borrowers: dict[int, Borrower]

    def __init__(self):
        self.borrowers = {}
        self.MAX_ACTIVE_LOANS = 3
        self.MAX_OVERDUE_LOANS = 1
        self.MAX_FINE = 50.00
        self.borrower_counter = 0

    def add_borrower(self, name: str, age: int, categories: list[str]):
        self.borrower_counter += 1
        self.borrowers[self.borrower_counter] = Borrower(name=name, age=age, categories=categories, is_blocked=False, fine=0.0)

    def get_borrower_by_id(self, borrower_id: int) -> Borrower:
        if borrower_id in self.borrowers:
            return self.borrowers[borrower_id]
        return None

    def remove_borrower(self, borrower_id: int) -> None:
        if borrower_id in self.borrowers:
            self.borrowers.pop(borrower_id, None)

    def update_borrower(self, borrower_id: int, name: str, age: int, categories: list[str]) -> None:
        if borrower_id in self.borrowers:
            self.borrowers[borrower_id].name = name
            self.borrowers[borrower_id].age = age
            self.borrowers[borrower_id].categories = categories

    def get_all_borrowers(self) -> list[Borrower]:
        return list(self.borrowers.values())

    def get_fine_by_borrower(self, borrower_id: int) -> float:
        if borrower_id in self.borrowers:
            return self.borrowers[borrower_id].fine
        return 0.0

    def update_fine_by_borrower(self, borrower_id: int, fine: float) -> None:
        if borrower_id in self.borrowers:
            self.borrowers[borrower_id].fine = fine

    def block_borrower(self, borrower_id: int) -> None:
        if borrower_id in self.borrowers:
            self.borrowers[borrower_id].is_blocked = True

    def borrower_exists(self, borrower_id):
        return borrower_id in self.borrowers

    def is_blocked(self, borrower_id):
        return self.borrowers[borrower_id].is_blocked if borrower_id in self.borrowers else False


