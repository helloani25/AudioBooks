from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional


class Parkingstatus(Enum):
    PENDING = 0
    HOLD = 1
    RESERVED = 2
    CANCELED = 3
    EXPIRED = 4
    EXITED = 5

@dataclass
class Reservation:
    customer_id: int
    parking_time: datetime
    max_hour_limit: int
    vehicle_id: int
    status: Parkingstatus
    reservationDate: datetime
    expirationDate: Optional[datetime]
    cancelledDate: Optional[datetime]

class ReservationManager:
    reservations_dict: Dict[int, Reservation]
    def __init__(self):
        self.reservations_dict = {}

    def add_reservation(self, customer_id: int, parking_time: datetime, max_hour_limit: int, vehicle_id: int, status: Parkingstatus, reservation_date: datetime, expiration_date: datetime):
        self.reservations_dict[vehicle_id] = Reservation(customer_id=customer_id, parking_time=parking_time, max_hour_limit=max_hour_limit, vehicle_id=vehicle_id, status=status, reservationDate=reservation_date, expirationDate=None, cancelledDate= None)

    def remove_reservation(self, vehicle_id: int):
        try:
            if vehicle_id not in self.reservations_dict:
                raise KeyError(vehicle_id)
            self.reservations_dict.pop(vehicle_id, None)
        except KeyError:
            print(f"VehicleID {vehicle_id} does not exist")

    def get_reservation(self, vehicle_id: int) -> Reservation:
        return self.reservations_dict.get(vehicle_id)

    def update_reservation(self, vehicle_id: int, reservation: Reservation):
        try:
            if vehicle_id not in self.reservations_dict:
                raise KeyError(vehicle_id)
            self.reservations_dict[vehicle_id] = reservation
        except KeyError:
            print(f"VehicleID {vehicle_id} does not exist")



