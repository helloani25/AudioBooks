from datetime import datetime, timedelta
from ParkingLot.ParkingLotManager import ParkingLotManager
from ParkingLot.Vehicle import Vehicle, VehicleSize
from ParkingLot.ParkingSpot import SpotStatus

def test_parking_lot_flow():
    print("Starting Parking Lot Flow Test...")
    capacity = {VehicleSize.SMALL: 2, VehicleSize.MEDIUM: 2}
    manager = ParkingLotManager(capacity)

    # Test 1: Normal Entry
    vehicle1 = Vehicle(1, "ABC-123", VehicleSize.SMALL, "Toyota", "Corolla", 2020, 101)
    print(f"Vehicle {vehicle1.vehicle_id} entering...")
    assert manager.enter_parking_spot(vehicle1) is True
    assert any(spot.vehicle == vehicle1 and spot.status == SpotStatus.OCCUPIED for spot in manager.spots)
    print("Normal entry successful.")

    # Test 2: Normal Exit
    print(f"Vehicle {vehicle1.vehicle_id} exiting...")
    assert manager.exit_parking_spot(vehicle1) is True
    assert all(spot.vehicle != vehicle1 for spot in manager.spots)
    print("Normal exit successful.")

    # Test 3: Reservation
    vehicle2 = Vehicle(2, "XYZ-789", VehicleSize.MEDIUM, "Honda", "Civic", 2021, 102)
    print(f"Vehicle {vehicle2.vehicle_id} reserving spot...")
    assert manager.reserve_parking_spot(vehicle2) is True
    held_spot = next((spot for spot in manager.spots if spot.status == SpotStatus.HELD and spot.reserved_vehicle == vehicle2), None)
    assert held_spot is not None
    print("Reservation successful (spot HELD).")

    # Test 4: Other vehicle cannot take HELD spot
    vehicle3 = Vehicle(3, "DEF-456", VehicleSize.MEDIUM, "Ford", "Focus", 2019, 103)
    # Only 2 medium spots. One is HELD for vehicle2. One is AVAILABLE.
    # First one should succeed because there's one more medium spot.
    assert manager.enter_parking_spot(vehicle3) is True
    
    # Now all medium spots are either OCCUPIED or HELD.
    vehicle4 = Vehicle(4, "GHI-789", VehicleSize.MEDIUM, "Nissan", "Sentra", 2018, 104)
    assert manager.enter_parking_spot(vehicle4) is False
    print("Protection of HELD spot verified.")

    # Test 5: Reserved vehicle enters using HELD spot
    print(f"Reserved vehicle {vehicle2.vehicle_id} entering...")
    assert manager.enter_parking_spot(vehicle2) is True
    assert held_spot.status == SpotStatus.OCCUPIED
    assert held_spot.vehicle == vehicle2
    print("Reserved entry successful.")

    # Test 6: Reservation Expiration
    vehicle5 = Vehicle(5, "EXP-999", VehicleSize.SMALL, "Tesla", "Model 3", 2022, 105)
    print(f"Vehicle {vehicle5.vehicle_id} reserving spot for expiration test...")
    manager.reserve_parking_spot(vehicle5)
    
    # Manually expire the reservation in the manager
    res = manager.reservation_manager.get_reservation(vehicle5.vehicle_id)
    res.expirationDate = datetime.now() - timedelta(minutes=1)
    
    # Try to enter - cleanup should happen, and it might find a spot if available, 
    # but the HELD status should be cleared first.
    print("Triggering cleanup via entry...")
    # There was 1 small spot left (capacity 2, 1 used then exited).
    manager.enter_parking_spot(vehicle5)
    
    # Check that reservation is gone
    assert manager.reservation_manager.get_reservation(vehicle5.vehicle_id) is None
    print("Reservation expiration cleanup verified.")

    print("All tests passed!")

if __name__ == "__main__":
    test_parking_lot_flow()
