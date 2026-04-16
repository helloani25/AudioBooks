from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from collections import defaultdict
from dataclasses import dataclass
import threading
import time

from VirtualLibrary.Borrower import BorrowerManager
from VirtualLibrary.CatalogManagement import CatalogManagement
from VirtualLibrary.InventoryManagement import InventoryManagement, BookStatus


def interval(seconds):
    def decorator(func):
        def wrapper(*args, **kwargs):
            def run():
                while True:
                    func(*args, **kwargs)
                    time.sleep(seconds)
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            return thread
        return wrapper
    return decorator


class ReservationStatus(Enum):
    RESERVED = "RESERVED"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    CHECKED_OUT = "CHECKED_OUT"


@dataclass
class Reservation:
    reservation_id: int
    borrower_id: int
    book_id: int
    reservation_date: datetime
    checkout_date: datetime
    status: ReservationStatus

class ReservationManager:
    reservations: dict[int, list[Reservation]]
    borrower_manager: BorrowerManager
    inventory_management: InventoryManagement

    def __init__(self):
        self.reservations = defaultdict(list)
        self.borrower_manager = BorrowerManager()
        self.catalog_management = CatalogManagement()
        self.reservation_id_counter = 0
        self.inventory_management = InventoryManagement()
        self.update_pending_pickup_reservations()
        self.update_expired_reservations()

    def add_reservation(self, borrower_id: int, book_id: int, reservation_date: datetime, checkout_date: datetime):
        if not self.borrower_manager.borrower_exists(borrower_id):
            raise ValueError(f"Borrower with ID {borrower_id} not found")
        if self.catalog_management.get_book_by_id(book_id) is None:
            raise ValueError(f"Book with ID {book_id} not found")
        if self.borrower_manager.is_blocked(borrower_id):
            raise ValueError("Borrower is blocked")
        self.reservation_id_counter += 1
        self.reservations[borrower_id].append(Reservation(reservation_id=self.reservation_id_counter,
                                                          borrower_id = borrower_id,
                                                          book_id=book_id,
                                                          reservation_date = reservation_date,
                                                          checkout_date=checkout_date,
                                                          status= ReservationStatus.RESERVED))
        self.inventory_management.update_book_status(book_id, BookStatus.RESERVED)

    def get_reservation(self, borrower_id: int, reservation_id: int) -> Optional[Reservation]:
        if borrower_id not in self.reservations:
            raise ValueError(f"Borrower with ID {borrower_id} not found")
        for reservation in self.reservations[borrower_id]:
            if reservation.reservation_id == reservation_id:
                return reservation
        raise ValueError(f"Reservation with ID {reservation_id} not found")

    def get_all_reservations(self, borrower_id: int) -> list[Reservation]:
        return self.reservations.get(borrower_id)

    def update_reservation(self, borrower_id: int, reservation_id: int, status: ReservationStatus):
        for reservation in self.reservations[borrower_id]:
            if reservation.reservation_id == reservation_id:
                reservation.status = status
                if status == ReservationStatus.CHECKED_OUT:
                    self.inventory_management.update_book_status(reservation.book_id, BookStatus.CHECKED_OUT)
                return True
        return False

    @interval(3600)
    def update_pending_pickup_reservations(self):
        for borrower_id, reservations in self.reservations.items():
            for reservation in reservations:
                if reservation.checkout_date < datetime.now() and reservation.status == ReservationStatus.RESERVED:
                    reservation.status = ReservationStatus.READY_FOR_PICKUP

    @interval(3600)
    def update_expired_reservations(self):
        for borrower_id, reservations in self.reservations.items():
            for reservation in reservations:
                if (reservation.checkout_date < datetime.now() - timedelta(days=2) 
                    and reservation.status in [ReservationStatus.RESERVED, ReservationStatus.READY_FOR_PICKUP]):
                    reservation.status = ReservationStatus.EXPIRED
                    self.inventory_management.update_book_status(reservation.book_id, BookStatus.RESERVATION_EXPIRED)

    def cancel_reservation(self, borrower_id: int, reservation_id: int):
        if reservation_id is None:
            raise ValueError("Reservation ID cannot be None")
        if borrower_id not in self.reservations:
            raise ValueError(f"Borrower with ID {borrower_id} not found")
        for reservation in self.reservations[borrower_id]:
            if reservation.reservation_id == reservation_id:
                if reservation.status in [ReservationStatus.RESERVED, ReservationStatus.READY_FOR_PICKUP]:
                    reservation.status = ReservationStatus.CANCELLED
                    self.inventory_management.update_book_status(reservation.book_id, BookStatus.RESERVATION_CANCELED)
                    return True
                return False
        return False



