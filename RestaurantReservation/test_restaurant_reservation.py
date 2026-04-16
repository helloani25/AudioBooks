from datetime import datetime, timedelta
from RestaurantReservation.ReservationManager import ReservationManager
from RestaurantReservation.TableBooking import BookingStatus

def test_restaurant_reservation():
    manager = ReservationManager()
    
    start_time = datetime.now() + timedelta(hours=1)
    end_time = start_time + timedelta(hours=2)
    customer_id = 101
    
    # 1. Test adding a reservation in 'outdoor' zone
    res_id = manager.add_reservation(capacity=2, start_time=start_time, end_time=end_time, customer_id=customer_id, zone="outdoor")
    assert res_id != -1, "Should be able to add a reservation for 2 in outdoor zone"
    
    res = manager.get_reservation(res_id, customer_id)
    assert res is not None
    assert res.zone == "outdoor"
    assert res.party_size == 2
    
    # 2. Check accounting (TableBookings)
    assert len(manager.bookingManager.table_bookings) == 1
    booking = manager.bookingManager.table_bookings[0]
    assert booking.customer_id == str(customer_id)
    assert booking.status == BookingStatus.RESERVED
    
    # 3. Test reassignment (Ephemeral assignment)
    old_table_id = res.table_id
    success = manager.reassign_table(res_id, customer_id)
    
    # Since we have multiple tables in 'outdoor' (table_7 to table_11), it should be possible to reassign
    assert success, "Reassignment should be successful if alternative tables exist"
    
    new_res = manager.get_reservation(res_id, customer_id)
    assert new_res.table_id != old_table_id, "Table ID should have changed after reassignment"
    
    # Verify booking update
    assert manager.bookingManager.table_bookings[0].table_id == new_res.table_id, "Booking record should be updated with new table ID"

    # 4. Test capacity constraint
    # Try to book a table for 10 people in outdoor (max capacity is 2)
    res_id_huge = manager.add_reservation(capacity=10, start_time=start_time, end_time=end_time, customer_id=102, zone="outdoor")
    assert res_id_huge == -1, "Should fail to book for 10 people in outdoor zone"

    # 5. Test zone constraint
    res_id_banquet = manager.add_reservation(capacity=4, start_time=start_time, end_time=end_time, customer_id=103, zone="banquet")
    assert res_id_banquet != -1
    res_b = manager.get_reservation(res_id_banquet, 103)
    assert res_b.zone == "banquet"

    # 6. Test cancellation
    success_cancel = manager.remove_reservation(res_id, customer_id)
    assert success_cancel is True
    assert manager.get_reservation(res_id, customer_id) is None
    
    # Verify booking status changed to CANCELLED
    found_cancelled = False
    for b in manager.bookingManager.table_bookings:
        if b.customer_id == str(customer_id) and b.status == BookingStatus.CANCELLED:
            found_cancelled = True
            break
    assert found_cancelled, "Booking should be marked as CANCELLED after reservation removal"

    print("All tests passed successfully!")

if __name__ == "__main__":
    test_restaurant_reservation()
