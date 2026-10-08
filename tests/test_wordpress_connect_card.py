"""The Cockpit WordPress connect endpoint replaces the 'hand-edit .env' dead-end. These tests cover
the validation branches (which return BEFORE writing anything), so they never touch real .env or real
credentials. The nonce guard is mocked off here; it is proven separately by the shared cockpit-nonce
machinery every /build action uses."""
from starlette.testclient import TestClient

import mongoose.app as app_mod


def _client(monkeypatch):
    monkeypatch.setattr(app_mod, "_require_cockpit_nonce", lambda request: None)
    return TestClient(app_mod.app)


def test_route_exists():
    assert "/build/wordpress/connect" in [getattr(r, "path", "") for r in app_mod.app.routes]


def test_missing_fields_are_rejected_before_any_write(monkeypatch):
    called = {"write": False}
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "write_env", lambda *a, **k: called.__setitem__("write", True))
    c = _client(monkeypatch)
    r = c.post("/build/wordpress/connect", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "site URL" in body["reason"]
    assert called["write"] is False  # nothing persisted on a validation failure


def test_bad_url_scheme_is_rejected_before_any_write(monkeypatch):
    called = {"write": False}
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "write_env", lambda *a, **k: called.__setitem__("write", True))
    c = _client(monkeypatch)
    r = c.post("/build/wordpress/connect",
               json={"url": "example-court.org", "username": "admin", "app_password": "a b c"})
    assert r.json()["ok"] is False and "https://" in r.json()["reason"]
    assert called["write"] is False


def test_valid_input_writes_and_probes(monkeypatch):
    """A well-formed submission persists via write_env, applies to the process, and returns the live
    probe result. write_env and the probe are stubbed so no real .env or network is touched."""
    writes = {}
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "write_env", lambda updates, *a, **k: writes.update(updates))
    import mongoose.connectors.wordpress as wp
    monkeypatch.setattr(wp, "wp_status", lambda: {"ok": True, "configured": True, "user": "Alex", "url": "https://example-court.org"})
    c = _client(monkeypatch)
    r = c.post("/build/wordpress/connect",
               json={"url": "https://example-court.org/", "username": "alex", "app_password": "abcd efgh ijkl"})
    body = r.json()
    assert body["ok"] is True and body["connection"]["user"] == "Alex"
    assert writes["WP_URL"] == "https://example-court.org"  # trailing slash stripped
    assert writes["WP_USERNAME"] == "alex" and writes["WP_APP_PASSWORD"] == "abcd efgh ijkl"
