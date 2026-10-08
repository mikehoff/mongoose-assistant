"""Push status is a tri-state so the UI never conflates 'settings saved', 'a test actually got
delivered', and 'this device is subscribed'. These tests pin the two backend signals the UI reads:
the state summary shape, and that a test only counts as succeeded when it reached a device. DB
isolation is automatic via conftest."""
from __future__ import annotations


def test_push_state_summary_shape():
    from mongoose.services import web_push as WP
    s = WP.push_state_summary()
    for key in ("settings_saved", "subscriptions", "last_test_ok_at", "last_test_at"):
        assert key in s, key
    assert s["subscriptions"] == 0
    assert s["last_test_ok_at"] is None and s["last_test_at"] is None


def test_test_that_reached_no_device_is_not_success():
    from mongoose.services import web_push as WP
    WP.record_test_result(0)
    s = WP.push_state_summary()
    assert s["last_test_at"] is not None      # a test ran
    assert s["last_test_ok_at"] is None        # but nothing received it


def test_test_that_reached_a_device_is_success():
    from mongoose.services import web_push as WP
    WP.record_test_result(3)
    s = WP.push_state_summary()
    assert s["last_test_ok_at"] is not None
    assert s["last_test_at"] is not None


def test_status_endpoint_exposes_tristate():
    from fastapi.testclient import TestClient
    import mongoose.app as A
    client = TestClient(A.app)
    r = client.get("/cockpit/push/status", headers={"X-Cockpit-Nonce": A._COCKPIT_NONCE})
    assert r.status_code == 200
    body = r.json()
    for key in ("settings_saved", "last_test_ok_at", "last_test_at", "subscriptions"):
        assert key in body, key
