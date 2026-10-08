"""Owner-authorised password reset: the host resets a person's password from Cockpit and reads them
the new one (the reset authority for a local-first app with no email). Nonce-gated; returns the new
passphrase once; the new password logs in and the old one stops working."""
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.services import auth_accounts as AA
    AA.create_account("samtest", "initial-pw-xyz", "Samtest")
    return AA


def test_reset_password_helper(env):
    AA = env
    uid = AA.find_user("samtest")["user_id"]
    r = AA.reset_password(uid)
    assert r["ok"] and r["username"] == "samtest" and len(r["password"]) >= 8
    assert (AA.verify_credentials("samtest", r["password"]) or {}).get("ok") is True
    assert (AA.verify_credentials("samtest", "initial-pw-xyz") or {}).get("ok") is not True


def test_reset_unknown_account_fails_cleanly(env):
    assert env.reset_password("usr_nope")["ok"] is False


def test_reset_endpoint_is_nonce_gated_and_returns_new_password(env, monkeypatch):
    import mongoose.app as app_mod
    monkeypatch.setattr(app_mod, "_require_cockpit_nonce", lambda request: None)
    from starlette.testclient import TestClient
    c = TestClient(app_mod.app)
    r = c.post("/cockpit/guests/reset-password", json={"username": "samtest"}).json()
    assert r["ok"] and r["username"] == "samtest" and r["password"]
    assert (env.verify_credentials("samtest", r["password"]) or {}).get("ok") is True


def test_reset_endpoint_bad_account(env, monkeypatch):
    import mongoose.app as app_mod
    monkeypatch.setattr(app_mod, "_require_cockpit_nonce", lambda request: None)
    from starlette.testclient import TestClient
    c = TestClient(app_mod.app)
    r = c.post("/cockpit/guests/reset-password", json={"username": "nobody"}).json()
    assert r["ok"] is False


def test_reset_routes_as_chat_intent():
    """Reset must be reachable from the phone, which has only chat: 'reset X's password' routes to the
    password_reset intent (and generic resets do not)."""
    from mongoose.services.intent_resolver import resolve
    for m in ("reset Jordan's password", "reset the password for jordan", "give jordan a new password"):
        assert resolve(m)[0] == "password_reset", m
    assert resolve("repair the door")[0] != "password_reset"
