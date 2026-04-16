from enum import Enum
from dataclasses import dataclass
import logging


@dataclass
class Fees(Enum):
    two_hour_fee = 14
    three_hour_fee = 12
    five_hour_fee =  10
    twenty_hour_fee = 8

class FeeStrategy:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

    @staticmethod
    def determine_fees(ticket) -> float:
        duration = ticket.exited_time - ticket.entry_time
        duration_hours = duration.total_seconds() / 3600
        try:
            if duration_hours <= 2:
                return Fees.two_hour_fee.value
            elif duration_hours <= 3:
                return Fees.three_hour_fee.value
            elif duration_hours <= 5:
                return Fees.five_hour_fee.value
            elif duration_hours <= 24:
                return Fees.twenty_hour_fee.value
            else:
                raise ValueError(f"Time exceeded 24 hour limit. Total Duration: {duration_hours}")
        except Exception as e:
            FeeStrategy.logger.exception(e)
            return 0.0

