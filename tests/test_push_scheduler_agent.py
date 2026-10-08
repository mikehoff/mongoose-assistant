"""Tests for v0.13.0: outbound push-to-phone, the background scheduler, and the
device-files push ingest (Android agent path)."""
import asyncio

import pytest

from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- outbound push to phone (ntfy) -------------------------------------------
def test_send_to_phone_posts_to_ntfy(db, monkeypatch):
    import mongoose.services.notifications as N
    monkeypatch.setenv("NTFY_URL", "http://localhost:8090")
    monkeypatch.setenv("NTFY_TOPIC", "mongoose-test")

    captured = {}

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, content=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["content"] = content
            return FakeResp()

    monkeypatch.setattr(N.httpx, "AsyncClient", FakeClient)
    r = asyncio.run(N.send_to_phone("Reminder", "Hearing 9am", priority="high",
                                    tags=["calendar", "warning"], click="https://x.com"))
    assert r["sent"] is True
    assert captured["url"].endswith("/mongoose-test")
    assert captured["headers"]["Title"] == "Reminder"
    assert captured["headers"]["Priority"] == "high"
    assert captured["headers"]["Tags"] == "calendar,warning"
    assert captured["headers"]["Click"] == "https://x.com"


def test_send_to_phone_unconfigured_is_clear(db, monkeypatch):
    import mongoose.services.notifications as N
    monkeypatch.setenv("NTFY_URL", "")
    monkeypatch.setenv("NTFY_TOPIC", "")
    r = asyncio.run(N.send_to_phone("x"))
    assert r["sent"] is False and r["reason"] == "ntfy_not_configured"


def test_push_endpoint(db, monkeypatch):
    import mongoose.services.notifications as N

    async def fake_send(title, body="", **kw):
        return {"sent": True, "echo": title}
    monkeypatch.setattr(N, "send_to_phone", fake_send)
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.post("/notifications/push", json={"title": "hi", "body": "there"})
    assert r.status_code == 200 and r.json()["sent"] is True


# --- background scheduler ----------------------------------------------------
def test_interval_due_then_not(db):
    from mongoose.services import scheduler as S
    S._ensure_table()
    assert S._interval_due("sync_email", 30) is True   # never run -> due
    S._record_run("sync_email", "ok")
    assert S._interval_due("sync_email", 30) is False   # just ran -> not due


def test_daily_due_logic(db):
    from mongoose.services import scheduler as S
    S._ensure_table()
    # target 00:00 has always passed today; never run -> due
    assert S._daily_due("daily_brief", 0, 0) is True
    S._record_run("daily_brief", "ok")
    # ran today -> not due again today
    assert S._daily_due("daily_brief", 0, 0) is False


def test_run_due_jobs_isolates_failures(db, monkeypatch):
    from mongoose.services import scheduler as S

    async def good():
        return {"ok": 1}

    async def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(S, "_build_registry", lambda: {
        "good_job": (("interval", 1), good),
        "bad_job": (("interval", 1), boom),
    })
    ran = asyncio.run(S.run_due_jobs())
    assert ran["good_job"]["status"] == "ok"
    assert ran["bad_job"]["status"] == "error"   # one failing job doesn't sink the rest


def test_job_status_reports_schedule(db):
    from mongoose.services import scheduler as S
    rows = {r["job"]: r for r in S.job_status()}
    assert "daily_brief" in rows and "daily at" in rows["daily_brief"]["schedule"]
    assert "every" in rows["sync_email"]["schedule"]


def test_reindex_job_skips_without_roots(db, monkeypatch):
    from mongoose.services import scheduler as S
    monkeypatch.delenv("MONGOOSE_INDEX_ROOTS", raising=False)
    result = asyncio.run(S._job_reindex_files())
    assert result.get("skipped") == "no_index_roots"


# --- device-files push ingest (Android agent path) ---------------------------
def test_ingest_device_files_into_shared_index(db):
    from mongoose.services.file_index import ingest_device_files, search_files
    batch = [
        {"path": "/sdcard/Documents/notes.md", "filename": "notes.md",
         "text": "service charge dispute notes", "mtime": 1718400000.0, "device_id": "phone"},
        {"path": "/sdcard/Download/scan.pdf", "filename": "scan.pdf",
         "text": "", "mtime": 1718400001.0, "device_id": "phone"},
    ]
    r = ingest_device_files(batch, device="phone")
    assert r["indexed"] == 2
    # text content searchable, tagged phone
    hits = search_files("service charge")
    assert any(h["device"] == "phone" and h["title"] == "notes.md" for h in hits)
    # binary findable by name
    assert any(h["title"] == "scan.pdf" for h in search_files("scan"))


def test_device_files_ingest_idempotent(db):
    from mongoose.services.file_index import ingest_device_files
    from mongoose.db import connect
    batch = [{"path": "/sdcard/a.md", "filename": "a.md", "text": "hello", "mtime": 1.0}]
    ingest_device_files(batch, device="phone")
    ingest_device_files(batch, device="phone")
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM files WHERE path='/sdcard/a.md'").fetchone()[0]
    assert n == 1


def test_device_files_endpoint(db):
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.post("/device-files/ingest", json={
        "device": "phone",
        "files": [{"path": "/sdcard/x.md", "filename": "x.md", "text": "findme", "mtime": 1.0}],
    })
    assert r.status_code == 200 and r.json()["indexed"] == 1


def test_send_to_phone_and_device_files_in_mcp(db):
    from mongoose.mcp_server import _tools
    t = _tools()
    assert "send_to_phone" in t
