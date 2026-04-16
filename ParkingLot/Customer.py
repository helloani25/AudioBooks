from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Customer:
    customer_id: int
    address_id: str
    name: str
    phone: str
    email: str
    driving_license: str
    license_expiry_date: str


class CustomerService:
    customers: List[Customer]

    def __init__(self):
       self.customers = []

    def add_customer(self, address_id: str, name: str, phone: str, email: str):
        customer = Customer(address_id, name, phone, email)
        self.customers.append(customer)

    def get_customer_by_id(self, customer_id: int) -> Customer:
        for customer in self.customers:
            if customer.customer_id == customer_id:
                return customer
        return None

    def get_customer_by_name(self, name: str) -> Customer:
        result = []
        for customer in self.customers:
            if customer.name == name:
                result.append(customer)
        return result

    def remove_customer(self, customer_id: int) -> Optional[Customer]:
        for customer in self.customers:
            if customer.customer_id == customer_id:
                self.customers.remove(customer)
                return customer
        return None

    def update_customer(self, customer_id: int, name: str, phone: str, email: str) -> Optional[Customer]:
        for customer in self.customers:
            if customer.customer_id == customer_id:
                customer.name = name
                customer.phone = phone
                customer.email = email
            return customer
        return None




