"""A guest gets the same savable identity handoff owners get: claiming an invite returns a
suggested username + memorable passphrase, and the guest's own device token is allowed to
create that account (so they can be poked and recognised). A tokenless remote client is still
refused, so this did not open account creation to the world."""
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _setup(monkeypatch, tmp_path):
    # Hermetic: only the DB path and allow-remote are set (both auto-reverted by monkeypatch).
    # The invite is created with an explicit base_url instead of a global MONGOOSE_PUBLIC_URL,
    # so this file leaks no readiness-affecting env into sibling tests.
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "gi.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    from mongoose.db import init_db
    init_db()
    import mongoose.app as A
    return TestClient(A.app), A


def _invite():
    from mongoose.guest_invites import create_guest_invite
    inv = create_guest_invite(owner_label="Lee", suggested_name="Lee",
                              base_url="https://yourname.duckdns.org:8765")
    assert inv.get("ok"), inv
    return inv.get("token") or inv["invite_url"].rsplit("/", 1)[-1]


def _claim(c, tok):
    return c.post(f"/guest/invite/{tok}/claim", json={
        "mongoose_name": "Lee", "public_name": "Lee", "fun_fact": "I like kayaks"}).json()


def test_guest_claim_returns_savable_credentials(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    d = _claim(c, _invite())
    assert d["ok"], d
    assert d.get("suggested_username"), "a login should be offered at claim"
    assert d.get("suggested_password")
    assert "-" in d["suggested_password"], "memorable passphrase shape (word-word-word-N)"


def test_guest_device_can_register_and_login(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    d = _claim(c, _invite())
    u, p, dtoken = d["suggested_username"], d["suggested_password"], d["device_token"]
    r = c.post("/account/register", headers={"X-Mongoose-Device-Token": dtoken},
               json={"username": u, "password": p, "display_name": "Lee"})
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json().get("ok") is True
    lr = c.post("/account/login", json={"username": u, "password": p})
    assert lr.status_code == 200 and lr.json().get("ok") is True


def test_guest_may_reach_own_account_routes_but_not_owner_surface():
    # The remote guard lets a trusted-guest token reach its OWN identity routes (so the credential
    # handoff works over the tunnel) but nothing owner-scoped. Locks the allowlist that made a
    # guest's "Save and continue" 403 over the tunnel while working on loopback.
    import mongoose.app as A
    for r in ("/account/register", "/account/login", "/account/me", "/account/logout"):
        assert r in A._GUEST_ACCOUNT_ROUTES
    assert "/cockpit/models" not in A._GUEST_ACCOUNT_ROUTES
    assert "/device/build" not in A._GUEST_ACCOUNT_ROUTES
    assert not A._guest_allowed_device_route("/account/register")  # account routes are separate


def test_caller_auth_accepts_guest_token_but_not_tokenless_remote(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    d = _claim(c, _invite())
    dtoken = d["device_token"]
    # A REMOTE request (not loopback) carrying the guest device token is authenticated.
    ok = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"),
                         headers={"x-mongoose-device-token": dtoken}, cookies={})
    assert A._caller_is_authenticated(ok) is True
    # A remote request with NO token is still refused (we did not open the door to the world).
    no = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"), headers={}, cookies={})
    assert A._caller_is_authenticated(no) is False
