"""IDENT-1: onboarding captures the name the person wants to be known by.

The backend was always capable of this; what was missing was any UI that asked. These
tests pin the two capture paths the (new) UI uses, so the promise "each user confirms the
name they want to be known by" stays wired:

  - account creation carries display_name through to storage, and
  - the post-pairing /device/profile path records a device's chosen name.

If either regresses, the name step is silently dead again.
"""
from fastapi.testclient import TestClient


def _client():
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


def test_create_account_stores_display_name():
    from mongoose.services import auth_accounts as AA
    res = AA.create_account("alex", "longenoughpw", display_name="Alex H")
    assert res["ok"] is True
    assert res["display_name"] == "Alex H"
    # and it is actually persisted, not just echoed back
    names = {u.get("display_name") for u in AA.list_users()}
    assert "Alex H" in names


def test_create_account_defaults_name_to_username():
    from mongoose.services import auth_accounts as AA
    res = AA.create_account("solo", "longenoughpw")
    assert res["ok"] is True
    # the brain should always have a human name; absent a chosen one it falls back to username
    assert res["display_name"] == "solo"


def test_register_endpoint_accepts_display_name():
    c = _client()
    r = c.post("/account/register",
               json={"username": "sam", "password": "longenoughpw", "display_name": "Sam"})
    assert r.status_code == 200, r.text
    assert r.json().get("display_name") == "Sam"


def test_register_endpoint_without_name_still_works():
    c = _client()
    r = c.post("/account/register", json={"username": "nameless", "password": "longenoughpw"})
    assert r.status_code == 200, r.text
    # falls back to the username, never empty
    assert r.json().get("display_name") == "nameless"


def test_device_profile_records_chosen_name(monkeypatch):
    """A freshly paired device has no chosen name until /device/profile is called; after the
    call (the launchpad's first-run step), device_whoami reflects the name."""
    from mongoose import devices as D
    code = D.create_pairing_code(profile="trusted_guest")["code"]
    claimed = D.claim_pairing_code(code, "Sam iPhone", "phone")
    assert claimed["ok"] is True
    did = claimed["device_id"]
    assert not (D.device_whoami(did) or {}).get("display_name")  # no name yet

    # route the device-token resolver at our real, just-paired device
    monkeypatch.setattr(D, "device_for_token", lambda tok: D.device_whoami(did) or {"device_id": did})
    c = _client()
    r = c.post("/device/profile", json={"display_name": "Sam"},
               headers={"X-Mongoose-Device-Token": "real-device"})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is not False
    # persisted on the device record
    assert (D.device_whoami(did) or {}).get("display_name") == "Sam"


def test_device_profile_rejects_empty_name(monkeypatch):
    from mongoose import devices as D
    code = D.create_pairing_code(profile="trusted_guest")["code"]
    did = D.claim_pairing_code(code, "X", "phone")["device_id"]
    monkeypatch.setattr(D, "device_for_token", lambda tok: D.device_whoami(did) or {"device_id": did})
    c = _client()
    r = c.post("/device/profile", json={"display_name": "   "},
               headers={"X-Mongoose-Device-Token": "real-device"})
    assert r.status_code == 200
    assert r.json().get("ok") is False
