from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import NamedTuple


class Table(NamedTuple):
    table_id: str
    capacity: int
    zone: str

class TableAssigmentStatus(Enum):
    ASSIGNED = 1
    FREE = 2

'''
Table assignments are ephemeral and only valid for a limited time. They
are used to track the status of table reservations and ensure that free intervals
can be determined.
'''


class TableAssignment:
    def __init__(self, table_id: str, customer_id: int, start_time: datetime, end_time: datetime,
                 status: TableAssigmentStatus):
        self.table_id = table_id
        self.customer_id = customer_id
        self.start_time = start_time
        self.end_time = end_time
        self.status = status


class TableManager:
    tables: dict[str, list[Table]]
    table_assignments: list
    table_bookings: list

    def __init__(self):
        self.tables = defaultdict(list)
        self.table_assignments = []
        self.table_bookings = []
        for i in range(5):
            self.tables["indoor"].append(Table(table_id=f"table_{i + 1}", capacity=4, zone="indoor"))
        for i in range(6, 11):
            self.tables["outdoor"].append(Table(table_id=f"table_{i + 1}", capacity=2, zone="outdoor"))
        for i in range(11, 16):
            self.tables["indoor"].append(Table(table_id=f"table_{i + 1}", capacity=2, zone="indoor"))
        for i in range(16, 21):
            self.tables["banquet"].append(Table(table_id=f"table_{i + 1}", capacity=6, zone="banquet"))

    def assign_table(self, customer_id: int, start_time: datetime, end_time: datetime, capacity: int, zone: str = None):
        available_tables = self.get_free_intervals(capacity, start_time, end_time, zone)
        if not available_tables:
            return None
        assigned_table = available_tables.pop(0)
        self.table_assignments.append(
            TableAssignment(table_id=assigned_table[0], customer_id=customer_id, start_time=start_time,
                            end_time=end_time, status=TableAssigmentStatus.ASSIGNED))
        return assigned_table[0]

    def add_table_assignment(self, table_id: str, customer_id: int, start_time: datetime, end_time: datetime, status: TableAssigmentStatus) -> None:
        self.table_assignments.append(TableAssignment(table_id, customer_id, start_time, end_time, status))

    def remove_table_assignment(self, table_id: str, start_time: datetime, end_time: datetime) -> bool:
        for table_assignment in self.table_assignments:
            if (table_assignment.table_id == table_id 
                    and table_assignment.start_time == start_time 
                    and table_assignment.end_time == end_time):
                self.table_assignments.remove(table_assignment)
                return True
        return False

    def get_free_intervals(self, capacity: int, start_time: datetime, end_time: datetime, zone: str = None) -> list[
        tuple[str, datetime, datetime]]:
        free_intervals = []
        zones_to_check = [zone] if zone else self.tables.keys()
        
        for z in zones_to_check:
            for table in self.tables[z]:
                if table.capacity >= capacity:
                    is_assigned = False
                    for assignment in self.table_assignments:
                        if assignment.table_id == table.table_id:
                            if not (end_time <= assignment.start_time or start_time >= assignment.end_time):
                                is_assigned = True
                                break
                    if not is_assigned:
                        free_intervals.append((table.table_id, start_time, end_time))
        return free_intervals
