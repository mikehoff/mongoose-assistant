"""Self-service user accounts: username/password creation + persistent cross-device sessions."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def test_create_verify_and_hash_safety(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.auth_accounts as AA
    import sqlite3
    r = AA.create_account("Alice", "correcthorse")
    assert r["ok"] and r["username"] == "alice"          # normalised lowercase
    assert AA.create_account("alice", "another1")["ok"] is False   # duplicate rejected
    assert AA.verify_credentials("alice", "correcthorse")["ok"] is True
    assert AA.verify_credentials("alice", "wrong")["ok"] is False
    assert AA.verify_credentials("nobody", "x")["ok"] is False
    # password never stored in clear; stored as scrypt
    con = sqlite3.connect(str(tmp_path / "m.sqlite3"))
    h = con.execute("SELECT password_hash FROM user_accounts WHERE username='alice'").fetchone()[0]
    assert h.startswith("scrypt$") and "correcthorse" not in h


def test_username_and_password_validation(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.auth_accounts as AA
    assert AA.create_account("ab", "longenough1")["ok"] is False        # too short username
    assert AA.create_account("has space", "longenough1")["ok"] is False  # invalid char
    assert AA.create_account("okname", "short")["ok"] is False           # weak password
    assert AA.create_account("ok.name-2", "longenough1")["ok"] is True   # allowed chars


def test_session_binds_user_and_survives_independently(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("API_TOKEN", "tok")
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.services.auth_accounts as AA
    import mongoose.session_auth as SA
    uid = AA.create_account("carol", "passphrase1")["user_id"]
    tok = SA.issue_session(remember=True, user_id=uid)
    assert SA.session_user(tok) == uid          # identity recoverable from the session
    assert SA.verify_session(tok) is True
    # bearer (no user) still works and reports empty user
    bearer = SA.issue_session()
    assert SA.session_user(bearer) == ""
    # tampering breaks it
    assert SA.verify_session(tok[:-2] + "xx") is False


def test_endpoints_register_login_me_crossdevice(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    # device 1 (loopback testclient = trusted) registers
    c1 = TestClient(A.app)
    assert c1.post("/account/register", json={"username": "dave", "password": "raptorclaw7"}).status_code == 200
    # device 2 is a SEPARATE client (no shared cookie) , logs in with the same account
    c2 = TestClient(A.app)
    r = c2.post("/account/login", json={"username": "dave", "password": "raptorclaw7", "remember": True})
    assert r.status_code == 200 and r.cookies.get("mongoose_session")
    # /account/me on device 2 shows the username (persistent identity across devices)
    me = c2.get("/account/me").json()
    assert me["authenticated"] is True and me.get("username") == "dave"
    # device 3 with WRONG password is rejected
    c3 = TestClient(A.app)
    assert c3.post("/account/login", json={"username": "dave", "password": "nope"}).status_code == 401


def test_register_requires_authenticated_caller(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # simulate a NON-loopback, tokenless-or-unauthed client: lock_local makes even loopback require auth
    monkeypatch.setenv("MONGOOSE_LOCK_LOCAL", "true")
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.app as A
    c = TestClient(A.app)
    # no session, lock_local on -> register refused (either the global guard's 403 or the handler's 401)
    r = c.post("/account/register", json={"username": "mallory", "password": "sneakyone1"})
    assert r.status_code in (401, 403)


def test_change_password(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.auth_accounts as AA
    AA.create_account("erin", "oldpassword1")
    assert AA.change_password("erin", "wrong", "newpassword1")["ok"] is False
    assert AA.change_password("erin", "oldpassword1", "newpassword1")["ok"] is True
    assert AA.verify_credentials("erin", "newpassword1")["ok"] is True
    assert AA.verify_credentials("erin", "oldpassword1")["ok"] is False
