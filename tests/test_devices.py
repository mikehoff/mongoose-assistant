"""Tests for the device pairing groundwork (Mac-side of "Android as a surface").

The security model is the point: pairing codes are minted only by an authenticated user
(the endpoint is nonce-gated), codes are single-use and short-lived, device tokens are
stored only as hashes, the request guard authenticates paired devices and rejects
revoked ones immediately. These tests lock that down."""
import asyncio
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

import mongoose.devices as D
import mongoose.app as A
from mongoose.app import app

client = TestClient(app)


def test_pairing_code_lifecycle():
    code = D.create_pairing_code("primary_phone")
    assert code["code"] and code["fingerprint"] and code["expires_in"] == 300
    claim = D.claim_pairing_code(code["code"], "Test Phone", "android")
    assert claim["ok"] is True
    assert claim["device_token"] and claim["device_id"]
    # default capabilities for a phone profile
    assert "approvals" in claim["capabilities"]


def test_pairing_code_is_single_use():
    code = D.create_pairing_code()
    D.claim_pairing_code(code["code"], "First", "android")
    again = D.claim_pairing_code(code["code"], "Second", "android")
    assert again["ok"] is False
    assert "already used" in again["error"]


def test_pairing_code_rejects_bogus_and_expired():
    assert D.claim_pairing_code("not-real", "X", "android")["ok"] is False
    # expired code
    code = D.create_pairing_code()
    from mongoose.db import connect
    with connect() as con:
        con.execute("UPDATE pairing_codes SET expires_at=? WHERE 1=1", (time.time() - 1,))
        con.commit()
    assert D.claim_pairing_code(code["code"], "X", "android")["ok"] is False


def test_token_stored_only_as_hash():
    """The raw device token must never be stored; only its hash."""
    code = D.create_pairing_code()
    claim = D.claim_pairing_code(code["code"], "HashTest", "android")
    tok = claim["device_token"]
    from mongoose.db import connect
    with connect() as con:
        rows = con.execute("SELECT token_hash FROM paired_devices").fetchall()
    assert all(r["token_hash"] != tok for r in rows)   # never the plaintext
    assert any(r["token_hash"] == D._hash(tok) for r in rows)  # the hash is there


def test_revoke_is_immediate():
    code = D.create_pairing_code()
    claim = D.claim_pairing_code(code["code"], "RevokeMe", "android")
    tok = claim["device_token"]
    assert D.device_for_token(tok) is not None
    D.revoke_device(claim["device_id"])
    assert D.device_for_token(tok) is None   # dead immediately


def test_pair_code_endpoint_requires_nonce():
    """Only an authenticated user can mint codes (an attacker on the LAN cannot)."""
    r = client.post("/cockpit/devices/pair-code", json={"profile": "primary_phone"})
    assert r.status_code == 403


def test_pair_code_endpoint_with_nonce_returns_qr():
    n = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = client.post("/cockpit/devices/pair-code",
                    headers={"X-Cockpit-Nonce": n}, json={"profile": "primary_phone"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] and body["pair_url"].endswith("code=" + body["code"])


def test_claim_endpoint_is_public_but_needs_valid_code():
    """/pair/claim is reachable pre-auth (device has no credential yet) but useless
    without a valid code."""
    r = client.post("/pair/claim", json={"code": "bogus", "device_name": "X"})
    assert r.status_code == 200          # public
    assert r.json()["ok"] is False       # but rejects bad code


def test_pair_page_is_public():
    r = client.get("/pair?code=abc")
    assert r.status_code == 200
    assert "Pair with Mongoose" in r.text


def test_devices_list_endpoint():
    r = client.get("/cockpit/devices")
    assert r.status_code == 200
    assert "devices" in r.json()


def test_revoke_endpoint_requires_nonce():
    r = client.post("/cockpit/devices/revoke", json={"device_id": "x"})
    assert r.status_code == 403


def test_guard_owner_full_guest_scoped_and_rejects_revoked(monkeypatch):
    """Owner device tokens (primary_phone) authenticate the FULL surface; guest tokens
    (trusted_guest) only /device/*; no token and revoked tokens are rejected. Device tokens do
    NOT require allow_remote, so we leave it unset to prove the decoupling."""
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "t")
    monkeypatch.delenv("MONGOOSE_ALLOW_REMOTE", raising=False)
    import mongoose.settings as st
    st._SETTINGS = None

    owner = D.claim_pairing_code(D.create_pairing_code("primary_phone")["code"], "OwnerPhone", "android")
    guest = D.claim_pairing_code(D.create_pairing_code("trusted_guest")["code"], "GuestPhone", "android")
    otok, gtok = owner["device_token"], guest["device_token"]

    def req(headers, path="/cockpit/models", method="GET"):
        return SimpleNamespace(url=SimpleNamespace(path=path, scheme="http"),
                               client=SimpleNamespace(host="192.168.1.77"),
                               method=method, headers=headers, cookies={})

    async def nxt(r):
        return SimpleNamespace(status_code=200)

    def status(headers, path):
        return asyncio.run(A._guard_sensitive_routes(req(headers, path=path), nxt)).status_code

    # owner: device and cockpit both pass, with allow_remote OFF
    assert status({"x-mongoose-device-token": otok}, "/device/whoami") == 200
    assert status({"x-mongoose-device-token": otok}, "/cockpit/models") == 200
    # guest: device passes, cockpit refused
    assert status({"x-mongoose-device-token": gtok}, "/device/whoami") == 200
    assert status({"x-mongoose-device-token": gtok}, "/cockpit/models") == 403
    # no token: refused
    assert status({}, "/device/whoami") == 403
    # revoked owner: refused immediately
    D.revoke_device(owner["device_id"])
    assert status({"x-mongoose-device-token": otok}, "/device/whoami") == 403


def test_cockpit_has_devices_tab():
    body = client.get("/ui").text
    assert "devices:loadDevices" in body
    assert "Add a device" in body


def test_tablet_profile_has_different_caps():
    code = D.create_pairing_code("home_tablet")
    claim = D.claim_pairing_code(code["code"], "Tablet", "android")
    assert "cockpit" in claim["capabilities"]
    assert "notifications" not in claim["capabilities"]  # tablet doesn't capture by default


def test_capability_status_encodes_purpose():
    """The registry answers 'what can THIS device do', with per-capability state:
    enabled / available (not yet on) / off_by_design."""
    code = D.create_pairing_code("primary_phone")
    claim = D.claim_pairing_code(code["code"], "PurposePhone", "android")
    dev = [d for d in D.list_devices() if d["device_id"] == claim["device_id"]][0]
    by_cap = {cs["capability"]: cs for cs in dev["capability_status"]}
    assert by_cap["approvals"]["state"] == "enabled"        # expected -> on at pairing
    assert by_cap["notifications"]["state"] == "available"  # not yet enabled
    assert dev["next_step"]                                  # a concrete next step exists


def test_tablet_notifications_off_by_design():
    code = D.create_pairing_code("home_tablet")
    claim = D.claim_pairing_code(code["code"], "DesignTablet", "android")
    dev = [d for d in D.list_devices() if d["device_id"] == claim["device_id"]][0]
    by_cap = {cs["capability"]: cs for cs in dev["capability_status"]}
    assert by_cap["notifications"]["state"] == "off_by_design"


def test_device_reports_capabilities_and_off_by_design_ignored():
    code = D.create_pairing_code("home_tablet")
    claim = D.claim_pairing_code(code["code"], "ReportTablet", "android")
    # tablet tries to enable a capability that's off_by_design for its role
    r = D.set_device_capabilities(claim["device_id"], ["cockpit", "notifications"])
    assert r["ok"] is True
    assert "notifications" not in r["enabled"]   # ignored, not allowed for this role
    assert "cockpit" in r["enabled"]


def test_pairing_leaves_receipts():
    code = D.create_pairing_code("primary_phone")
    claim = D.claim_pairing_code(code["code"], "ReceiptPhone", "android")
    D.revoke_device(claim["device_id"])
    events = [r["event"] for r in D.device_receipts()]
    assert "paired" in events and "revoked" in events


def test_freshness_reports_staleness():
    code = D.create_pairing_code("primary_phone")
    claim = D.claim_pairing_code(code["code"], "StalePhone", "android")
    from mongoose.db import connect
    with connect() as con:
        con.execute("UPDATE paired_devices SET last_seen=? WHERE device_id=?",
                    ("2020-01-01T00:00:00+00:00", claim["device_id"]))
        con.commit()
    dev = [d for d in D.list_devices() if d["device_id"] == claim["device_id"]][0]
    assert dev["freshness"]["state"] == "stale"
    assert "not seen" in dev["freshness"]["detail"]


def test_device_capabilities_endpoint_requires_device_token():
    assert client.post("/device/capabilities", json={"enabled": ["voice"]}).status_code == 403
