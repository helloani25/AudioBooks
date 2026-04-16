from datetime import datetime, timedelta

from ParkingLot.ParkingSpot import ParkingSpot, SpotStatus
from ParkingLot.ParkingTicket import TicketDispenser
from ParkingLot.ReservationManager import ReservationManager, Parkingstatus
from ParkingLot.Vehicle import VehicleSize, Vehicle


class ParkingLotManager:
    reservation_manager: ReservationManager
    ticket_dispenser: TicketDispenser
    def __init__(self, capacity: dict[VehicleSize, int]):
        self.spots: list[ParkingSpot] = []
        self.ticket_dispenser = TicketDispenser()
        self.reservation_manager = ReservationManager()

        counter = 0
        for size, count in capacity.items():
            for _ in range(count):
                counter += 1
                self.spots.append(ParkingSpot(counter, size))

    def enter_parking_spot(self, vehicle: Vehicle):
        self._cleanup_expired_reservations()
        reservation = self.reservation_manager.get_reservation(vehicle.vehicle_id)
        
        # 1. Try to use a reserved spot if it exists and hasn't expired
        if reservation:
            if datetime.now() < reservation.expirationDate:
                for spot in self.spots:
                    if spot.status == SpotStatus.HELD and spot.reserved_vehicle == vehicle:
                        spot.assign_spot(vehicle)
                        vehicle.ticketId = self.ticket_dispenser.add_ticket(vehicle)
                        self.reservation_manager.remove_reservation(vehicle.vehicle_id)
                        return True
            else:
                # Reservation exists but is expired. 
                # _cleanup_expired_reservations should have handled it, but double check.
                pass

        # 2. Try to find an available spot (not held or occupied)
        for spot in self.spots:
            if spot.size == vehicle.size and spot.status == SpotStatus.AVAILABLE:
                spot.assign_spot(vehicle)
                vehicle.ticketId = self.ticket_dispenser.add_ticket(vehicle)
                return True

        return False

    def _cleanup_expired_reservations(self):
        # Identify all expired reservations
        expired_vehicle_ids = []
        now = datetime.now()
        for vehicle_id, reservation in self.reservation_manager.reservations_dict.items():
            if now >= reservation.expirationDate:
                expired_vehicle_ids.append(vehicle_id)
        
        # Clear spots held for expired reservations and remove from ReservationManager
        for vehicle_id in expired_vehicle_ids:
            for spot in self.spots:
                if spot.status == SpotStatus.HELD and spot.reserved_vehicle and spot.reserved_vehicle.vehicle_id == vehicle_id:
                    spot.clear_spot()
            self.reservation_manager.remove_reservation(vehicle_id)

    def reserve_parking_spot(self, vehicle: Vehicle):
        self._cleanup_expired_reservations()
        for spot in self.spots:
            if spot.size == vehicle.size and spot.status == SpotStatus.AVAILABLE:
                self.reservation_manager.add_reservation(
                    customer_id=vehicle.customerId,
                    parking_time=datetime.now(),
                    max_hour_limit=2,
                    vehicle_id=vehicle.vehicle_id,
                    status=Parkingstatus.RESERVED,
                    reservation_date=datetime.now(),
                    expiration_date=datetime.now() + timedelta(minutes=30)
                )
                spot.reserve_spot(vehicle)
                return True
        return False

    def exit_parking_spot(self, vehicle: Vehicle):
        for spot in self.spots:
            if spot.vehicle == vehicle:
                self.ticket_dispenser.close_ticket(vehicle.vehicle_id)
                spot.clear_spot()
                return True
        return False





