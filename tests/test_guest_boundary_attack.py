"""Live attack matrix: a remote trusted_guest must never reach owner-only routes.

This is the security regression guard for the guest/owner boundary. It mints real owner and guest
device tokens via the pairing flow, forces a REMOTE (non-loopback) client so the token gate actually
engages (loopback is trusted as the Mac console and would mask the boundary), then asserts a guest is
denied on owner-only routes and allowed on guest-safe ones.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import mongoose.app as A
from mongoose.app import app
from mongoose import devices as D


@pytest.fixture
def remote_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "g.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "testtoken")
    # Drop 'testclient' from the loopback set so TestClient is treated as a REMOTE caller and the
    # token gate engages. Without this, every request looks like the trusted local Mac console.
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", frozenset({"127.0.0.1", "::1", "localhost"}))
    return TestClient(app)


def _mint(profile):
    code = D.create_pairing_code(profile=profile)["code"]
    return D.claim_pairing_code(code, f"{profile}-dev", "android")["device_token"]


OWNER_ONLY = [
    ("/cockpit/nonce", "GET"),
    ("/system/status", "GET"),
    ("/cockpit/devices", "GET"),
    ("/build/status", "GET"),
    ("/rtm/reserve/position", "GET"),
]

GUEST_SAFE = [
    ("/device/whoami", "GET"),
    ("/device/summary", "GET"),
]


def test_remote_guest_denied_on_owner_routes(remote_client):
    guest = _mint("trusted_guest")
    for path, method in OWNER_ONLY:
        r = remote_client.request(method, path, headers={"x-mongoose-device-token": guest})
        assert r.status_code == 403, f"guest reached owner route {path} (status {r.status_code}) — BOUNDARY LEAK"


def test_remote_guest_allowed_on_safe_routes(remote_client):
    guest = _mint("trusted_guest")
    for path, method in GUEST_SAFE:
        r = remote_client.request(method, path, headers={"x-mongoose-device-token": guest})
        assert r.status_code != 403, f"guest wrongly denied safe route {path}"


def test_remote_owner_allowed_where_guest_denied(remote_client):
    owner = _mint("primary_phone")
    # owner should pass the gate on at least the read routes (200), proving the deny is guest-specific
    for path in ["/system/status", "/cockpit/nonce", "/cockpit/devices"]:
        r = remote_client.request("GET", path, headers={"x-mongoose-device-token": owner})
        assert r.status_code == 200, f"owner blocked on {path} (status {r.status_code})"


def test_guest_denial_does_not_leak_capability(remote_client):
    """A guest's 403 must not enumerate what is behind the wall."""
    guest = _mint("trusted_guest")
    r = remote_client.request("GET", "/system/status", headers={"x-mongoose-device-token": guest})
    assert r.status_code == 403
    body = (r.json() or {})
    # the error message should be the generic forbidden text, not a description of system internals
    msg = str(body.get("message", "")).lower()
    assert "scheduler" not in msg and "build" not in msg and "memory" not in msg
