class Customer:
    def __init__(self, customer_id: int, name: str):
        self.customer_id = customer_id
        self.name = name

    def __repr__(self):
        return f"Customer(customer_id={self.customer_id}, name='{self.name}')"

    def __eq__(self, other):
        return self.customer_id == other.customer_id

    def add_customer(self, customer_id: int, name: str):
        self.customer_id = customer_id
        self.name = name

    def update_customer(self, customer_id: int, name: str):
        if self.customer_id is None:
            raise ValueError("Customer is not initialized")
        self.customer_id = customer_id
        self.name = name