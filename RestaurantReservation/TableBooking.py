"""Enumeration representing the possible states of a table booking.

This enumeration defines the lifecycle states that a table booking can
transition through, from initial reservation to final disposition. Each
status value tracks a specific stage in the booking process.
"""
from collections import defaultdict
from datetime import datetime
from enum import Enum


class BookingStatus(Enum):
    RESERVED = 1
    OCCUPIED = 2
    CANCELLED = 3


"""
Table booking are used for tracking customers who dined and charge them.
These are persisted for tax purposes.
"""

class TableBooking:
    def __init__(self, table_id: str, customer_id: str, start_time: datetime, end_time: datetime, party_size: int, status: BookingStatus):
        self.table_id = table_id
        self.customer_id = customer_id
        self.start_time = start_time
        self.end_time = end_time
        self.party_size = party_size
        self.status = status

    def __repr__(self):
        return f"TableBooking(table_id={self.table_id}, customer_id={self.customer_id}, start_time={self.start_time}, end_time={self.end_time}, party_size={self.party_size}, status={self.status})"

    def __eq__(self, other):
        if not isinstance(other, TableBooking):
            return False
        return self.table_id == other.table_id and self.customer_id == other.customer_id and self.start_time == other.start_time

    def __hash__(self):
        return hash((self.table_id, self.customer_id, self.start_time))


class TableBookingManager:
    def __init__(self):
        self.table_bookings: dict[str, TableBooking] = {}

    def add_booking(self, table_id, customer_id, start_time, end_time, party_size, status):
        booking = TableBooking(table_id, customer_id, start_time, end_time, party_size, status)
        self.table_bookings[table_id] = booking
        return booking

    def remove_booking(self, table_id):
        self.table_bookings.pop(table_id, None)

    def get_booking(self, table_id):
        return self.table_bookings.get(table_id)

    def update_booking(self, table_id, booking):
        self.table_bookings[table_id] = booking
