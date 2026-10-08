"""Owner pairing secret: the first device to claim the OWNER slot must present a secret shown only
on the Mac, so a guest/random network device can't grab it by scanning the install QR first."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_secret_lifecycle():
    _fresh()
    from mongoose.owner_pairing import (get_or_create_owner_secret, verify_owner_secret,
                                        consume_owner_secret, owner_secret_active)
    s = get_or_create_owner_secret()
    assert len(s) == 6
    assert get_or_create_owner_secret() == s          # stable
    assert verify_owner_secret(s) is True
    assert verify_owner_secret(s.lower()) is True      # case-insensitive
    assert verify_owner_secret("WRONGX") is False
    assert verify_owner_secret("") is False
    assert owner_secret_active() is True
    consume_owner_secret()
    assert owner_secret_active() is False
    assert verify_owner_secret(s) is False


def test_loopback_pairs_owner_without_secret():
    """The Mac itself (loopback) is trusted — physical access == the secret's trust level."""
    _fresh()
    from mongoose.install_progress import set_step
    set_step("ready", "done")
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/install/pair-code")
    assert r.status_code == 200
    assert r.json().get("code")


def test_remote_without_secret_refused(monkeypatch):
    """A non-loopback caller with no/var wrong secret must be refused the owner slot (401)."""
    _fresh()
    from mongoose.install_progress import set_step
    set_step("ready", "done")
    from mongoose.owner_pairing import get_or_create_owner_secret
    get_or_create_owner_secret()  # ensure a secret exists

    import mongoose.app as A
    # Force the endpoint to see a remote (non-loopback) client.
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", set())
    from fastapi.testclient import TestClient
    c = TestClient(A.app, raise_server_exceptions=False)
    r = c.post("/install/pair-code")
    assert r.status_code == 401
    assert "owner_secret" in r.json().get("detail", "")


def test_remote_with_correct_secret_pairs(monkeypatch):
    _fresh()
    from mongoose.install_progress import set_step
    set_step("ready", "done")
    from mongoose.owner_pairing import get_or_create_owner_secret
    secret = get_or_create_owner_secret()

    import mongoose.app as A
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", set())
    from fastapi.testclient import TestClient
    c = TestClient(A.app, raise_server_exceptions=False)
    r = c.post("/install/pair-code", headers={"X-Owner-Secret": secret})
    assert r.status_code == 200
    assert r.json().get("code")
