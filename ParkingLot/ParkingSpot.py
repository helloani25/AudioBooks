from dataclasses import dataclass
from enum import Enum

from ParkingLot.Vehicle import VehicleSize, Vehicle

class SpotStatus(Enum):
    AVAILABLE = 0
    HELD = 1
    OCCUPIED = 2

@dataclass
class ParkingSpot:
    spot_id: int
    status: SpotStatus
    vehicle: Vehicle = None
    reserved_vehicle: Vehicle = None
    size: VehicleSize = None

    def __init__(self, spot_id, size: VehicleSize):
        self.spot_id = spot_id
        self.status = SpotStatus.AVAILABLE
        self.size = size
        self.vehicle = None
        self.reserved_vehicle = None

    def assign_spot(self, vehicle: Vehicle):
        self.status = SpotStatus.OCCUPIED
        self.vehicle = vehicle
        self.reserved_vehicle = None

    def reserve_spot(self, vehicle: Vehicle):
        self.status = SpotStatus.HELD
        self.reserved_vehicle = vehicle

    def clear_spot(self):
        self.status = SpotStatus.AVAILABLE
        self.vehicle = None
        self.reserved_vehicle = None

