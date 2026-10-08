"""Global anti-500 safety net: an unexpected error degrades to a structured 503, while
deliberate HTTPExceptions (auth/nonce) keep their exact status."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_unexpected_error_returns_graceful_503():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app

    @app.get("/__test_boom_500")
    def _boom():
        raise RuntimeError("simulated subsystem failure")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/__test_boom_500")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "temporarily_unavailable"
    assert "request_id" in body
    assert body["path"] == "/__test_boom_500"


def test_http_exception_status_preserved():
    _fresh()
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from mongoose.app import app

    @app.get("/__test_authfail_403")
    def _authfail():
        raise HTTPException(status_code=403, detail="nope")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/__test_authfail_403")
    assert r.status_code == 403  # NOT converted to 503


def test_real_cockpit_endpoints_dont_500_on_bare_install():
    """A bare install must not 500 any of the core cockpit read endpoints."""
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    h = {"X-Cockpit-Nonce": _COCKPIT_NONCE}
    with TestClient(app, raise_server_exceptions=False) as c:
        for path in ("/cockpit/wizard", "/cockpit/brain/wakeup", "/cockpit/messages",
                     "/health", "/setup/status"):
            r = c.get(path, headers=h)
            assert r.status_code < 500, f"{path} returned {r.status_code} on bare install"
