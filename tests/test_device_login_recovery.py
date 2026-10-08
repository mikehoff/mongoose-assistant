"""Credential recovery: a device whose token was cleared can sign back in with username + password
and get a FRESH device token, with the profile decided by the account (owner restores owner access,
everyone else gets guest). The owner can set a sign-in password proven by their paired device."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "dl.sqlite3"),
                 "MONGOOSE_DATA_DIR": str(tmp_path / "d"),
                 "MONGOOSE_ALLOW_REMOTE": "true", "MONGOOSE_HOST": "0.0.0.0",
                 "MONGOOSE_API_TOKEN": "t", "MONGOOSE_PUBLIC_URL": "https://example.duckdns.org"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app)


def _owner_device(A_devices):
    code = A_devices.create_pairing_code(profile="primary_phone")["code"]
    return A_devices.claim_pairing_code(code, "Alex", "phone", display_name="Alex")["device_token"]


def test_owner_can_set_password_then_login_restores_owner_access(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    from mongoose import devices
    otok = _owner_device(devices)
    H = {"X-Mongoose-Device-Token": otok}
    # Mint the owner identity, then set a known sign-in password (no old password needed).
    assert c.post("/account/owner-identity", headers=H).status_code == 200
    r = c.post("/account/set-password", headers=H, json={"password": "recover-me-123"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["username"] == "alex"

    # Simulate a wiped device: sign in fresh with username + password, NO device token header.
    lr = c.post("/device/login", json={"username": "alex", "password": "recover-me-123"})
    assert lr.status_code == 200, lr.text
    d = lr.json()
    assert d["ok"] and d["profile"] == "primary_phone" and d["device_token"]
    # The new token really is an owner device.
    from mongoose.devices import device_for_token
    assert device_for_token(d["device_token"])["profile"] == "primary_phone"


def test_guest_login_only_restores_guest_and_bad_password_rejected(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    from mongoose.services.auth_accounts import create_account
    create_account("gina", "guest-pw-123", display_name="Gina")  # a plain (non-owner) account
    # correct guest login -> trusted_guest, never owner
    d = c.post("/device/login", json={"username": "gina", "password": "guest-pw-123"}).json()
    assert d["ok"] and d["profile"] == "trusted_guest"
    from mongoose.devices import device_for_token
    assert device_for_token(d["device_token"])["profile"] == "trusted_guest"
    # wrong password is refused
    assert c.post("/device/login", json={"username": "gina", "password": "nope"}).status_code == 401


def test_set_password_requires_owner_device(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label="g")
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": "Sam", "public_name": "Sam", "fun_fact": "x"})
    r = c.post("/account/set-password", headers={"X-Mongoose-Device-Token": cl["device_token"]},
               json={"password": "whatever-123"})
    assert r.status_code == 403
