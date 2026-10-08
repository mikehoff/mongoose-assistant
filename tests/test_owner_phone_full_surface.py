"""The owner's paired phone (profile primary_phone) gets the FULL cockpit surface even with
allow_remote OFF; a trusted_guest device stays scoped to /device/*. We simulate a non-loopback
caller by removing 'testclient' from the loopback allowlist so the request guard actually runs
(TestClient is otherwise treated as loopback-trusted and bypasses the guard entirely)."""
from fastapi.testclient import TestClient


def _remote_client(monkeypatch):
    import mongoose.app as A
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", {"127.0.0.1", "::1", "localhost"})
    return TestClient(A.app, raise_server_exceptions=False)


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MONGOOSE_ALLOW_REMOTE", raising=False)
    monkeypatch.delenv("MONGOOSE_API_TOKEN", raising=False)
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def _owner_token():
    from mongoose import devices as D
    code = D.create_pairing_code(profile="primary_phone")["code"]
    return D.claim_pairing_code(code, "Owner Phone", "phone")["device_token"]


def _guest_token():
    from mongoose import devices as D
    code = D.create_pairing_code(profile="trusted_guest")["code"]
    return D.claim_pairing_code(code, "Guest Phone", "phone")["device_token"]


def test_unauthenticated_remote_is_forbidden_on_cockpit(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    c = _remote_client(monkeypatch)
    assert c.get("/cockpit/nonce").status_code == 403


def test_owner_phone_reaches_full_cockpit_without_allow_remote(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = _owner_token()
    c = _remote_client(monkeypatch)
    r = c.get("/cockpit/nonce", headers={"X-Mongoose-Device-Token": tok})
    assert r.status_code == 200
    assert "cockpit_nonce" in r.json()


def test_guest_phone_blocked_from_cockpit(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = _guest_token()
    c = _remote_client(monkeypatch)
    assert c.get("/cockpit/nonce", headers={"X-Mongoose-Device-Token": tok}).status_code == 403


def test_guest_phone_still_allowed_on_device_surface(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = _guest_token()
    c = _remote_client(monkeypatch)
    # device surface reaches its handler (not a 403 from the guard)
    assert c.get("/device/summary", headers={"X-Mongoose-Device-Token": tok}).status_code != 403


def test_phone_cockpit_route_serves_html(tmp_path, monkeypatch):
    # The phone-cockpit page itself is public (serves HTML); its /cockpit/* data calls are what the
    # guard gates. So it must load even before the device token is read client-side.
    _fresh(tmp_path, monkeypatch)
    from mongoose.app import app
    c = TestClient(app)
    r = c.get("/device/cockpit")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    assert "Full cockpit" in body or "full cockpit" in body
    assert "/cockpit/approvals" in body and "/cockpit/devices" in body
    assert "primary_phone" in body  # owner gating present
