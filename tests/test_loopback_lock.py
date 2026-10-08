"""Cockpit loopback lock: by default the Mac (loopback) is trusted with no login (frictionless
console). Setting MONGOOSE_LOCK_LOCAL=true requires a valid session even on loopback, so the cockpit
genuinely locks and times out — addressing 'no login, never times out, everyone gets my whole life'."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("MONGOOSE_LOCK_LOCAL", "MONGOOSE_ALLOW_REMOTE", "MONGOOSE_API_TOKEN"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_loopback_open_by_default():
    """Default: loopback reaches a protected cockpit route with no session."""
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    # a protected (non-public) GET — backup-status is nonce-gated but reachable on loopback
    r = c.get("/cockpit/backup-status")
    assert r.status_code != 403  # loopback trusted


def test_lock_local_blocks_loopback_without_session():
    """With lock_local on, loopback without a session is refused on a protected route."""
    _fresh(MONGOOSE_LOCK_LOCAL="true")
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/cockpit/backup-status")
    assert r.status_code == 403


def test_lock_local_allows_with_valid_session():
    """With lock_local on, a valid session cookie unlocks loopback access."""
    _fresh(MONGOOSE_LOCK_LOCAL="true")
    from mongoose.session_auth import issue_session, SESSION_COOKIE
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set(SESSION_COOKIE, issue_session())
    r = c.get("/cockpit/backup-status")
    assert r.status_code != 403


def test_login_page_public_even_when_locked():
    """The login page must stay reachable when locked, or you couldn't log in."""
    _fresh(MONGOOSE_LOCK_LOCAL="true")
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/login")
    assert r.status_code == 200
