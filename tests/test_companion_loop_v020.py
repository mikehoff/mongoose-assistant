"""v0.20 companion-loop tests: paired devices are scoped surfaces, not master API
callers; the mobile launchpad has useful real data; prepare-me cards are grounded."""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import mongoose.app as A
from mongoose.app import app
from mongoose.db import connect, init_db
import mongoose.devices as D

client = TestClient(app)


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def _paired(profile="primary_phone"):
    code = D.create_pairing_code(profile)
    claim = D.claim_pairing_code(code["code"], "Moto Phone", "android")
    assert claim["ok"] is True
    return claim


def _remote_req(headers, path, method="GET"):
    return SimpleNamespace(url=SimpleNamespace(path=path, scheme="http"),
                           client=SimpleNamespace(host="192.168.1.44"),
                           method=method, headers=headers, cookies={})


async def _next(r):
    return SimpleNamespace(status_code=200)


def test_device_token_owner_full_guest_scoped(db, monkeypatch):
    # The owner's own paired phone (primary_phone) IS the owner: full Cockpit surface. A
    # trusted_guest device stays scoped to /device/* only. Device tokens no longer require
    # allow_remote (a per-device, revocable token is its own scoped grant); the master API token
    # still does. We assert the guard's behaviour for a simulated remote (LAN) caller.
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "master")
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    import mongoose.settings as st
    st._SETTINGS = None
    owner = _paired("primary_phone")["device_token"]
    guest = _paired("trusted_guest")["device_token"]

    def status(tok, path, method="GET"):
        return asyncio.run(A._guard_sensitive_routes(
            _remote_req({"x-mongoose-device-token": tok}, path, method=method), _next)).status_code

    # owner phone: device AND broad cockpit/setup all pass the guard
    assert status(owner, "/device/whoami") == 200
    assert status(owner, "/cockpit/models") == 200
    assert status(owner, "/setup/save", method="POST") == 200

    # guest phone: device routes pass, broad cockpit/setup refused
    assert status(guest, "/device/whoami") == 200
    assert status(guest, "/cockpit/models") == 403
    assert status(guest, "/setup/save", method="POST") == 403

    # master token still authenticates broad routes
    master = asyncio.run(A._guard_sensitive_routes(
        _remote_req({"x-mongoose-token": "master"}, "/cockpit/models"), _next))
    assert master.status_code == 200


def test_device_heartbeat_and_whoami(db):
    claim = _paired()
    headers = {"X-Mongoose-Device-Token": claim["device_token"]}
    r = client.get("/device/whoami", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["device"]["device_id"] == claim["device_id"]
    assert body["device"]["next_step"]
    assert "device_token" not in str(body)

    hb = client.post("/device/heartbeat", headers=headers)
    assert hb.status_code == 200
    assert hb.json()["ok"] is True


def test_device_event_envelope_allowlist_and_size(db):
    claim = _paired()
    headers = {"X-Mongoose-Device-Token": claim["device_token"]}
    ok = client.post("/device/event", headers=headers,
                     json={"type": "notifications_permission_missing", "payload": {"app": "WhatsApp"}})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    bad = client.post("/device/event", headers=headers, json={"type": "run_shell", "payload": {}})
    assert bad.json()["ok"] is False
    huge = client.post("/device/event", headers=headers,
                       json={"type": "device_checkin", "payload": {"x": "a" * 9000}})
    assert huge.json()["ok"] is False


def test_pair_claim_rate_limited(db):
    A._PAIR_FAILS.clear()
    for _ in range(A._PAIR_FAIL_LIMIT):
        r = client.post("/pair/claim", json={"code": "bogus", "device_name": "x"})
        assert r.status_code == 200
    blocked = client.post("/pair/claim", json={"code": "bogus", "device_name": "x"})
    assert blocked.status_code == 429
    A._PAIR_FAILS.clear()


def test_device_share_creates_bus_message_and_receipt(db):
    claim = _paired()
    headers = {"X-Mongoose-Device-Token": claim["device_token"]}
    r = client.post("/device/share", headers=headers,
                    json={"title": "PLS thing", "note": "remember this document", "url": "https://example.com"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    from mongoose.services.bus import list_messages
    msgs = list_messages(unread_only=False)
    assert any(m["subject"] == "PLS thing" for m in msgs)
    assert any(x["event"] == "share_intake_created" for x in D.device_receipts())


def test_device_launchpad_served_and_pair_page_stores_token(db):
    page = client.get("/device/launchpad")
    assert page.status_code == 200
    # New phone surface: must have push-to-talk, text chat, and voice endpoints wired
    assert 'id="ptt"' in page.text      # push-to-talk button
    assert 'id="msg"' in page.text      # text input
    assert '/device/voice' in page.text # voice endpoint
    assert '/device/chat' in page.text  # chat endpoint
    pair = client.get("/pair?code=abc")
    assert "mongoose_device_token" in pair.text


def test_prepare_card_uses_calendar_email_file_and_memory(db):
    now = datetime.now(timezone.utc)
    with connect() as con:
        con.execute("INSERT INTO events(source,title,start_at,end_at,busy_status,sensitivity,last_seen) VALUES('test','Meeting with Chris',?,?,'busy','normal',?)",
                    ((now - timedelta(hours=2)).isoformat(), (now + timedelta(hours=2)).isoformat(), now.isoformat()))
        con.execute("INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) VALUES('test','Chris <chris@example.com>','Chris agenda','agenda body',?,'normal',?)",
                    (now.isoformat(), now.isoformat()))
        con.execute("INSERT INTO email_fts(rowid,subject,body,sender,mailbox) VALUES(last_insert_rowid(),'Chris agenda','agenda body','Chris','inbox')")
        con.execute("INSERT INTO memory_facts(fact,source,dedupe_key,created_at,updated_at) VALUES('Chris prefers concise agenda notes','test','simonpref',?,?)",
                    (now.isoformat(), now.isoformat()))
        con.execute("INSERT INTO memory_fts(rowid,fact) VALUES(last_insert_rowid(),'Chris prefers concise agenda notes')")
        con.commit()
    from mongoose.services.prepare import prepare_card
    card = asyncio.run(prepare_card("Chris"))
    assert card["confidence"] == "grounded"
    sources = set(card["sources_used"])
    assert "calendar" in sources and "email" in sources and "memory" in sources
    assert card["suggested_next_step"]


def test_prepare_endpoint_and_cli_command_present(db):
    r = client.get("/cockpit/prepare", params={"q": "today"})
    assert r.status_code == 200
    assert "topic" in r.json()
    text = __import__("pathlib").Path("src/mongoose/cli.py").read_text()
    assert 'args.command == "prepare"' in text


def test_cockpit_overview_has_prepare_card(db):
    body = client.get("/ui").text
    assert "Prepare me" in body
    assert "prepareNow" in body


def test_surpriser_can_surface_device_readiness(db):
    import mongoose.services.surpriser as S
    _paired()
    cands = asyncio.run(S.candidate_suggestions())
    assert any(c["kind"] in ("device_setup", "device_stale") for c in cands)


def test_what_changed_reports_device_events(db):
    claim = _paired()
    D.record_device_event(claim["device_id"], "pwa_installed", {"ok": True})
    from mongoose.services.journal_layer import what_changed
    res = what_changed(acknowledge=False)
    assert res["changed"] is True
    assert "devices" in res["detail"]
    assert any("Devices:" in c for c in res["changes"])


def test_what_changed_has_useful_empty_states(db):
    from mongoose.services.journal_layer import what_changed
    res = what_changed(acknowledge=False)
    assert "empty_states" in res
    assert any("No" in x for x in res["empty_states"])
