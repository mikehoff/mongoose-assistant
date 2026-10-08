"""Tests for the LAN enable/repair + logging + off-thread reconciliation batch."""
import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("MONGOOSE_HOST", "MONGOOSE_ALLOW_REMOTE", "MONGOOSE_API_TOKEN"):
        os.environ.pop(k, None)
    import mongoose.setup_wizard as SW
    from pathlib import Path
    SW.ENV_PATH = Path(d) / ".env"
    from mongoose.db import init_db
    init_db()


def test_ensure_phone_access_defaults_writes_and_is_idempotent():
    _fresh()
    from mongoose.enable_phone import ensure_phone_access_defaults
    r1 = ensure_phone_access_defaults()
    assert r1["ok"] and r1["host"] == "0.0.0.0"
    assert r1["changed"] is True
    r2 = ensure_phone_access_defaults()
    assert r2["changed"] is False  # nothing left to change


def test_mail_status_exposes_sync_progress():
    _fresh()
    from mongoose.mail_status import mail_status, _set_progress
    _set_progress({"running": True, "stage": "syncing", "message": "x"})
    st = mail_status()
    assert "sync_progress" in st
    assert st["sync_progress"]["stage"] == "syncing"


def test_sync_now_runs_off_thread_and_reports_progress():
    _fresh()
    from mongoose.mail_status import sync_now, mail_status
    res = asyncio.run(sync_now())
    assert "ok" in res
    # progress should have advanced to a terminal stage
    prog = mail_status().get("sync_progress") or {}
    assert prog.get("stage") in ("complete", "failed")


def test_enable_phone_already_enabled_without_force_suggests_repair(monkeypatch):
    monkeypatch.setattr("mongoose.app._lan_socket_reachable", lambda settings, timeout=0.75: True)
    os.environ["MONGOOSE_HOST"] = "0.0.0.0"
    os.environ["MONGOOSE_ALLOW_REMOTE"] = "true"
    os.environ["MONGOOSE_API_TOKEN"] = "t" * 12
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    import mongoose.setup_wizard as SW
    from pathlib import Path
    SW.ENV_PATH = Path(d) / ".env"
    from mongoose.db import init_db
    init_db()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    c = TestClient(A.app)
    r = c.post("/cockpit/enable-phone", headers={"X-Cockpit-Nonce": A._COCKPIT_NONCE}, json={})
    body = r.json()
    assert r.status_code == 200
    assert body.get("already") is True
    assert "epair" in (body.get("detail", "") or "")
    for k in ("MONGOOSE_HOST", "MONGOOSE_ALLOW_REMOTE", "MONGOOSE_API_TOKEN"):
        os.environ.pop(k, None)


def test_request_observability_logs_start_and_done():
    """The diagnostic middleware logs request.start + request.done for non-noisy routes (so a freeze
    shows the last started-but-not-finished request)."""
    import io
    import logging
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    import mongoose.setup_wizard as SW
    from pathlib import Path
    SW.ENV_PATH = Path(d) / ".env"
    from mongoose.db import init_db
    init_db()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("mongoose.request")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    import mongoose.app as A
    from fastapi.testclient import TestClient
    c = TestClient(A.app)
    c.get("/doctor")
    out = buf.getvalue()
    assert "request.start" in out and "/doctor" in out
    assert "request.done" in out
    # noisy routes stay quiet
    buf.truncate(0)
    buf.seek(0)
    c.get("/health")
    assert "path=/health " not in buf.getvalue()


def test_safe_query_redacts_secrets():
    from mongoose.app import _safe_query_for_log
    out = _safe_query_for_log("token=supersecret&q=hello")
    assert "supersecret" not in out
    assert "redacted" in out.lower()
    assert "hello" in out  # non-secret preserved
