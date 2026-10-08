"""Tests for liveness/rot detection , the antidote to silent decay. It must flag stale sync
with a specific fix, report unused subsystems as not-configured (never 'broken'), and stay
honest about freshness from real timestamps."""
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

from mongoose.app import app
from mongoose.db import connect, init_db
import mongoose.liveness as L

client = TestClient(app)


def test_fresh_email_is_ok(monkeypatch):
    init_db()
    monkeypatch.setattr(L, "_email_configured", lambda: True)
    now = datetime.now(timezone.utc)
    with connect() as con:
        con.execute("INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
                    "VALUES('t','a','s','b',?,'normal',?)", (now.isoformat(), now.isoformat()))
        con.commit()
    rep = L.liveness()
    email = [c for c in rep["checks"] if c["subsystem"] == "email"][0]
    assert email["state"] == "ok"


def test_stale_email_is_flagged_with_fix(monkeypatch):
    init_db()
    monkeypatch.setattr(L, "_email_configured", lambda: True)
    old = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    with connect() as con:
        con.execute("INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
                    "VALUES('t','a','s','b',?,'normal',?)", (old, old))
        con.commit()
    rep = L.liveness()
    email = [c for c in rep["checks"] if c["subsystem"] == "email"][0]
    assert email["state"] == "stale"
    assert "fix" in email and email["fix"]            # a stale subsystem must name the fix
    assert rep["healthy"] is False


def test_unconfigured_subsystem_is_not_broken(monkeypatch):
    init_db()
    monkeypatch.setattr(L, "_email_configured", lambda: False)
    monkeypatch.setattr(L, "_calendar_configured", lambda: False)
    rep = L.liveness()
    email = [c for c in rep["checks"] if c["subsystem"] == "email"][0]
    assert email["state"] == "not_configured"        # absent != stale


def test_liveness_endpoint():
    r = client.get("/cockpit/liveness")
    assert r.status_code == 200
    body = r.json()
    assert "healthy" in body and "checks" in body and "headline" in body


def test_liveness_in_system_tab():
    body = client.get("/ui").text
    assert "System health" in body and "cockpit/liveness" in body
