"""Tests for the mail-sync visibility layer. The sync ENGINES already existed; this
layer makes them legible (per-provider authorised + freshness) and offers a sync-now
that reuses the scheduler's own job. Tests lock in the status shape, the actionable
setup hints, that sync-now doesn't diverge from the scheduler path, and the endpoints
(incl. the nonce gate on sync)."""
import asyncio

from fastapi.testclient import TestClient

import mongoose.mail_status as M
from mongoose.app import app

client = TestClient(app)


def test_mail_status_shape():
    st = M.mail_status()
    assert "providers" in st and "message_count" in st and "any_active" in st
    names = [p["provider"] for p in st["providers"]]
    assert names == ["imap", "gmail", "microsoft"]


def test_mail_status_unconfigured_gives_setup_hints():
    """When nothing is set up, each provider should carry an actionable next step,
    that's the 'real sync path' help."""
    st = M.mail_status()
    for p in st["providers"]:
        if not p["configured"]:
            assert p["note"]  # a non-empty hint telling the user what to do
    gmail = [p for p in st["providers"] if p["provider"] == "gmail"][0]
    assert "authorize google" in gmail["note"].lower() or "client-secrets" in gmail["note"].lower()


def test_mail_status_reflects_oauth(monkeypatch):
    """A present Google token flips gmail to authorized."""
    import mongoose.oauth as O
    monkeypatch.setattr(O, "oauth_status", lambda: {"google": True, "microsoft": False})
    st = M.mail_status()
    gmail = [p for p in st["providers"] if p["provider"] == "gmail"][0]
    assert gmail["authorized"] is True
    assert st["any_active"] is True


def test_sync_now_reuses_scheduler_job(monkeypatch):
    """sync-now must call the SAME job the scheduler runs (no divergent sync path)."""
    import mongoose.services.scheduler as sched
    called = {}

    async def fake_job():
        called["yes"] = True
        return {"gmail": {"ok": True}}
    monkeypatch.setattr(sched, "_job_sync_email", fake_job)
    res = asyncio.run(M.sync_now())
    assert called.get("yes") is True
    assert res["ok"] is True
    assert "providers" in res


def test_sync_now_handles_failure(monkeypatch):
    import mongoose.services.scheduler as sched

    async def boom():
        raise RuntimeError("imap down")
    monkeypatch.setattr(sched, "_job_sync_email", boom)
    res = asyncio.run(M.sync_now())
    assert res["ok"] is False and "imap down" in res["error"]


def test_mail_endpoint():
    r = client.get("/cockpit/mail")
    assert r.status_code == 200
    assert len(r.json()["providers"]) == 3


def test_mail_sync_endpoint_requires_nonce():
    assert client.post("/cockpit/mail/sync").status_code == 403


def test_mail_sync_endpoint_with_nonce(monkeypatch):
    import mongoose.services.scheduler as sched

    async def fake_job():
        return {"gmail": {"ok": True}}
    monkeypatch.setattr(sched, "_job_sync_email", fake_job)
    n = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = client.post("/cockpit/mail/sync", headers={"X-Cockpit-Nonce": n})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_alpha_check_has_mail_line():
    from mongoose.release_check import release_check
    rep = asyncio.run(release_check())
    mail = [ln for _, lines in rep["sections"] for ln in lines if ln["label"] == "Mail sync"]
    assert mail  # mail sync is surfaced in the readiness report


def test_cockpit_overview_has_mail_card():
    body = client.get("/ui").text
    assert "Mail sync" in body and "syncMail" in body
