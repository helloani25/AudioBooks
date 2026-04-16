from dataclasses import dataclass
from enum import Enum

class VehicleSize(Enum):
    SMALL = 1
    MEDIUM = 2
    LARGE = 3

@dataclass
class Vehicle:
    vehicle_id: int
    license_plate: str
    size: VehicleSize
    make: str
    model: str
    year: int
    customerId: int
    ticketId: int

    def __init__(self, vehicle_id, license_plate, size, make, model, year, customer_id):
        self.vehicle_id = vehicle_id
        self.license_plate = license_plate
        self.size = size
        self.make = make
        self.model = model
        self.year = year
        self.customerId = customer_id
        self.ticketId = 0





