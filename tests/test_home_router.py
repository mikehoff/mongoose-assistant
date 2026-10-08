from mongoose.services.home_router import HomeCommand, route_key


def test_route_key_is_exact_device_room_action():
    command = HomeCommand(room="bathroom", device_id="light.bathroom", entity_id="light.bathroom", action="turn_on")
    assert route_key(command) == "bathroom:light.bathroom:turn_on"


def test_preference_read_write_thresholds_consistent():
    # The read threshold in _get_preference must not exceed the write threshold
    # in _update_preference, or learned routes in the gap are stored-but-ignored.
    import inspect
    from mongoose.services import home_router as hr
    read_src = inspect.getsource(hr._get_preference)
    write_src = inspect.getsource(hr._update_preference)
    assert "0.75" in read_src, "read threshold drifted from 0.75"
    assert "0.75" in write_src, "write threshold drifted from 0.75"
