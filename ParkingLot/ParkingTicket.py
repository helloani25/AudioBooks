import datetime
from dataclasses import dataclass
import logging
from typing import Dict
from ParkingLot.FeeStrategy import FeeStrategy

from ParkingLot.ParkingSpot import SpotStatus
from datetime import datetime, timezone
from ParkingLot.Vehicle import Vehicle


@dataclass
class ParkingTicket:
    vehicle_id: int
    status: SpotStatus
    entry_time: datetime
    exited_time: datetime
    customer_id: int
    ticket_id: int
    charge: float


class TicketDispenser:
    ticket_counter: int = 0
    ticket_dict: Dict[int, ParkingTicket]
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    def __init__(self):
        self.ticket_dict = {}

    def add_ticket(self, vehicle: Vehicle) -> int | None:
        self.ticket_counter += 1
        # Simplified: always issue a ticket for now, or check reservations properly
        ticket = ParkingTicket(
            vehicle_id=vehicle.vehicle_id,
            status=SpotStatus.OCCUPIED,
            entry_time=datetime.now(timezone.utc),
            exited_time=datetime.now(timezone.utc),
            customer_id=vehicle.customerId,
            ticket_id=self.ticket_counter,
            charge=0.0
        )
        self.ticket_dict[vehicle.vehicle_id] = ticket
        return ticket.ticket_id

    def remove_ticket(self, vehicle_id: int) -> None:
        self.ticket_dict.pop(vehicle_id, None)

    def close_ticket(self, vehicle_id: int) -> None:
        try:
            if vehicle_id in self.ticket_dict:
                ticket = self.ticket_dict[vehicle_id]
                ticket.exited_time = datetime.now(timezone.utc)
                ticket.charge = FeeStrategy.determine_fees(ticket)
        except Exception as e:
            self.logger.exception(e)









