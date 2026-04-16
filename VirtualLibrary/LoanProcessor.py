from collections import defaultdict
from datetime import datetime

from VirtualLibrary.Borrower import BorrowerManager
from VirtualLibrary.CatalogManagement import CatalogManagement
from dataclasses import dataclass

from VirtualLibrary.InventoryManagement import InventoryManagement, BookStatus
from VirtualLibrary.ReservationManager import interval


class LoanStatus:
    ACTIVE = "ACTIVE"
    OVERDUE = "OVERDUE"
    RETURNED = "RETURNED"

@dataclass
class Loan:
    book_id: int
    borrower_id: int
    loan_date: datetime
    return_date: datetime
    fine: float
    status: str


class LoanProcessor:
    loans_data: dict[int, Loan] = {}
    loans_by_borrower: dict[int, list[int]] = {}
    MAX_ACTIVE_LOANS: int
    MAX_OVERDUE_LOANS: int
    MAX_FINE: float

    loan_counter = 0

    def __init__(self):
        self.MAX_ACTIVE_LOANS = 3
        self.MAX_OVERDUE_LOANS = 1
        self.MAX_FINE = 50.00
        self.borrower_manager = BorrowerManager()
        self.catalog_manager = CatalogManagement()
        self.loans_data = {}
        self.loans_by_borrower = defaultdict(list)
        self.inventory_management = InventoryManagement()

    def add_loan(self, book_id: int, borrower_id: int, loan_date: datetime, return_date: datetime):
        borrower = self.borrower_manager.get_borrower_by_id(borrower_id)
        if not borrower:
            raise ValueError(f"Borrower with ID {borrower_id} not found")
        
        book = self.catalog_manager.get_book_by_id(book_id)
        if not book:
            raise ValueError(f"Book with ID {book_id} not found")
        
        if borrower.is_blocked:
            raise ValueError(f"Borrower with ID {borrower_id} is blocked")
            
        if borrower.fine >= self.MAX_FINE:
            raise ValueError(f"Borrower with ID {borrower_id} has outstanding fines of {borrower.fine}")

        if loan_date > return_date:
            raise ValueError("Return date must be after loan date")
            
        active_loans = [l_id for l_id in self.loans_by_borrower[borrower_id] 
                        if self.loans_data[l_id].status in [LoanStatus.ACTIVE, LoanStatus.OVERDUE]]
        
        if len(active_loans) >= self.MAX_ACTIVE_LOANS:
            raise ValueError("Borrower has reached the maximum number of active loans")
            
        overdue_loans = [l_id for l_id in active_loans if self.loans_data[l_id].status == LoanStatus.OVERDUE]
        if len(overdue_loans) >= self.MAX_OVERDUE_LOANS:
            raise ValueError("Borrower has too many overdue loans")

        if self.inventory_management.get_availability_count(book_id) <= 0:
            raise ValueError(f"Book with ID {book_id} is not available for loan")

        self.loan_counter += 1
        self.loans_data[self.loan_counter] = Loan(book_id=book_id, borrower_id= borrower_id, loan_date=loan_date, return_date=return_date, fine= 0.0, status= LoanStatus.ACTIVE)
        self.loans_by_borrower[borrower_id].append(self.loan_counter)
        
        # Decrement availability count
        self.inventory_management.update_book_count(book_id, BookStatus.CHECKED_OUT)


    def update_loan_status(self, loan_id: int, new_status: LoanStatus):
        if loan_id not in self.loans_data:
            raise ValueError(f"Loan with ID {loan_id} not found")
        self.loans_data[loan_id].status = new_status
        
        if new_status == LoanStatus.RETURNED:
            self.inventory_management.update_book_status(self.loans_data[loan_id].book_id, BookStatus.RETURNED)
        elif new_status == LoanStatus.OVERDUE:
            # Book remains CHECKED_OUT in inventory
            pass
        elif new_status == LoanStatus.ACTIVE:
            # If it was already CHECKED_OUT (which ACTIVE implies), no change needed
            pass

    @interval(7200)
    def update_overdue_loans(self):
        for loan_id, loan in self.loans_data.items():
            if loan.status == LoanStatus.ACTIVE and loan.return_date < datetime.now():
                self.update_loan_status(loan_id, LoanStatus.OVERDUE)
                self.borrower_manager.update_fine_by_borrower(loan.borrower_id, self.MAX_FINE)

    @interval(7200)
    def block_overdue_loans_by_borrower(self, borrower_id: int):
        if borrower_id not in self.loans_by_borrower:
            raise ValueError(f"Borrower with ID {borrower_id} not found")
        borrower_overdue_loans = 0
        for loan_id in self.loans_by_borrower[borrower_id]:
            if self.loans_data[loan_id].status == LoanStatus.OVERDUE:
                borrower_overdue_loans += 1
        if borrower_overdue_loans >= self.MAX_OVERDUE_LOANS:
                self.borrower_manager.block_borrower(borrower_id)
                self.borrower_manager.update_fine_by_borrower(borrower_id, self.MAX_FINE * self.MAX_OVERDUE_LOANS)





