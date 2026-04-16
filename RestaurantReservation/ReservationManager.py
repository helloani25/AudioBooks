from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List

from RestaurantReservation.TableManager import TableManager, TableAssigmentStatus
from RestaurantReservation.TableBooking import TableBookingManager, BookingStatus


class ReservationStatus(Enum):
    RESERVED = "RESERVED"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Reservation:
    def __init__(self, reservation_id: int, table_id: str, customer_id: int, start_time: datetime, end_time: datetime,
                 party_size: int, status: ReservationStatus, zone: str = None):
        self.reservation_id = reservation_id
        self.table_id = table_id
        self.customer_id = customer_id
        self.start_time = start_time
        self.end_time = end_time
        self.party_size = party_size
        self.status = status
        self.zone = zone


class ReservationManager:
    def __init__(self, table_manager: TableManager = None, booking_manager: TableBookingManager = None):
        self.reservations: Dict[int, List[Reservation]] = {}
        self.tableManager = table_manager
        self.bookingManager = booking_manager
        self.reservation_counter: int = 0

    def get_free_intervals(self, capacity: int, start_time: datetime, end_time: datetime, zone: str = None) -> list[tuple[str, datetime, datetime]]:
        return self.tableManager.get_free_intervals(capacity, start_time, end_time, zone)

    def add_reservation(self, capacity: int, start_time: datetime, end_time: datetime, customer_id: int, zone: str = None) -> int:
        self._cleanup_expired_reservations()
        
        tables = self.tableManager.get_free_intervals(capacity, start_time, end_time, zone)
        if not tables:
            return -1
        
        # Assignment is ephemeral. We pick one for now.
        # Tables are already sorted by capacity if we used get_free_tables, 
        # but here it's just a list of tuples (table_id, start, end).
        assigned_table_id = tables[0][0]
        
        self.reservation_counter += 1
        reservation = Reservation(reservation_id=self.reservation_counter,
                                  table_id=assigned_table_id,
                                  customer_id=customer_id,
                                  start_time=start_time,
                                  end_time=end_time,
                                  party_size=capacity,
                                  status=ReservationStatus.RESERVED,
                                  zone=zone)
        
        # Register ephemeral assignment
        self.tableManager.assign_table(
                customer_id=customer_id,
                start_time=start_time,
                end_time=end_time)
        
        if customer_id not in self.reservations:
            self.reservations[customer_id] = []
        self.reservations[customer_id].append(reservation)
        
        # We also create a TableBooking for accounting purposes (RESERVED state)
        self.bookingManager.add_booking(assigned_table_id, str(customer_id), start_time, end_time, capacity, BookingStatus.RESERVED)
        
        return self.reservation_counter

    def get_reservation(self, reservation_id: int, customer_id: int) -> Optional[Reservation]:
        if customer_id not in self.reservations:
            return None
        for reservation in self.reservations[customer_id]:
            if reservation_id == reservation.reservation_id:
                return reservation
        return None

    def remove_reservation(self, reservation_id: int, customer_id: int) -> bool:
        if customer_id not in self.reservations:
            return False

        for reservation in self.reservations[customer_id]:
            if reservation_id == reservation.reservation_id:
                # Remove from table assignments
                self.tableManager.remove_table_assignment(reservation.table_id, reservation.start_time, reservation.end_time)
                # Mark booking as CANCELLED
                booking = self.bookingManager.get_booking(reservation.table_id)
                booking.status = BookingStatus.CANCELLED
                self.bookingManager.update_booking(reservation.table_id, booking)
                reservation.status = ReservationStatus.CANCELLED
                return True
        return False

    def update_reservation(self, customer_id: int, reservation_id: int, status: ReservationStatus) -> bool:
        self._cleanup_expired_reservations()
        reservation = self.get_reservation(reservation_id, customer_id)
        if reservation:
            reservation.status = status
            # If status becomes COMPLETED or OCCUPIED, update booking status
            if status == ReservationStatus.COMPLETED:
                # In a real system, we might have another status for completed
                pass
            if status == ReservationStatus.CANCELLED:
                booking = self.bookingManager.get_booking(reservation.table_id)
                booking.status = BookingStatus.CANCELLED
                self.bookingManager.update_booking(reservation.table_id,booking)
                # more transitions could be added here
            return True
        return False

    def reassign_table(self, reservation_id: int, customer_id: int) -> bool:
        """Example of changing ephemeral assignment before arrival"""
        reservation = self.get_reservation(reservation_id, customer_id)
        if not reservation or reservation.status != ReservationStatus.RESERVED:
            return False
            
        # Try to find another table in the same zone
        current_table_id = reservation.table_id
        new_table_id = self.tableManager.get_free_intervals(reservation.party_size, reservation.start_time, reservation.end_time, reservation.zone)[0][0]
        print(
            f"Reassigning table {current_table_id} to {new_table_id} for customer {customer_id} at {reservation.start_time}"
        )
        
        if new_table_id:
            reservation.table_id = new_table_id
            # Re-add assignment
            self.tableManager.remove_table_assignment(current_table_id, reservation.start_time, reservation.end_time)
            self.tableManager.add_table_assignment(new_table_id, customer_id, reservation.start_time, reservation.end_time, TableAssigmentStatus.ASSIGNED)
            # Update booking record too
            self.bookingManager.remove_booking(current_table_id)
            self.bookingManager.add_booking(new_table_id, str(customer_id), reservation.start_time, reservation.end_time, reservation.party_size, BookingStatus.RESERVED)
            return True

        return False
    
    def _cleanup_expired_reservations(self):
        current_time = datetime.now()
        for customer_id in list(self.reservations.keys()):
            reservations = self.reservations[customer_id]
            for reservation in reservations[:]:
                if reservation.end_time < current_time:
                    reservations.remove(reservation)
