"""Tests for the unified receipts ledger, Mongoose's one honest answer to "what did you
do?". The ledger composes existing receipt sources (devices, surpriser, scheduler,
WhatsApp drafts); it must add no new write path, sort newest-first, filter by source, and
be honest about an empty state."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from mongoose.app import app
from mongoose.db import connect, init_db
import mongoose.devices as D
from mongoose.services.receipts import receipts_ledger, format_ledger

client = TestClient(app)


def _seed_whatsapp_draft(status="sent"):
    with connect() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS whatsapp_drafts(id INTEGER PRIMARY KEY, chat_id TEXT, "
            "contact TEXT, body TEXT, status TEXT, created_at TEXT, decided_at TEXT, error TEXT)")
        con.execute(
            "INSERT INTO whatsapp_drafts(contact,body,status,created_at,decided_at) VALUES(?,?,?,?,?)",
            ("Jordan", "hi", status, datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()))
        con.commit()


def test_ledger_composes_multiple_sources():
    init_db()
    c = D.create_pairing_code("primary_phone")
    claim = D.claim_pairing_code(c["code"], "Phone", "android")
    D.revoke_device(claim["device_id"])
    _seed_whatsapp_draft("sent")
    ledger = receipts_ledger()
    assert ledger["count"] >= 3
    assert ledger["by_source"].get("devices", 0) >= 2   # paired + revoked
    assert ledger["by_source"].get("whatsapp", 0) >= 1


def test_ledger_sorted_newest_first():
    init_db()
    D.claim_pairing_code(D.create_pairing_code()["code"], "A", "android")
    ledger = receipts_ledger()
    times = [e["at"] for e in ledger["entries"] if e["at"]]
    assert times == sorted(times, reverse=True)


def test_ledger_source_filter():
    init_db()
    D.claim_pairing_code(D.create_pairing_code()["code"], "B", "android")
    _seed_whatsapp_draft("cancelled")
    only_wa = receipts_ledger(source="whatsapp")
    assert all(e["source"] == "whatsapp" for e in only_wa["entries"])
    assert only_wa["entries"]   # there is at least one


def test_whatsapp_sent_receipt_says_after_approval():
    init_db()
    with connect() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS whatsapp_drafts(id INTEGER PRIMARY KEY, chat_id TEXT, "
            "contact TEXT, body TEXT, status TEXT, created_at TEXT, decided_at TEXT, error TEXT)")
        con.execute(
            "INSERT INTO whatsapp_drafts(contact,body,status,created_at,decided_at) VALUES(?,?,?,?,?)",
            ("UniqueSentContact", "hi", "sent", datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()))
        con.commit()
    mine = [e for e in receipts_ledger(source="whatsapp")["entries"] if "UniqueSentContact" in e["text"]]
    assert mine and "after approval" in mine[0]["text"]


def test_whatsapp_cancelled_receipt_says_did_not_send():
    init_db()
    with connect() as con:
        con.execute("DELETE FROM whatsapp_drafts")  # clean slate; other tests seed too
        con.execute(
            "INSERT INTO whatsapp_drafts(contact,body,status,created_at,decided_at) VALUES(?,?,?,?,?)",
            ("UniqueCancelledContact", "hi", "cancelled", datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()))
        con.commit()
    mine = [e for e in receipts_ledger(source="whatsapp")["entries"] if "UniqueCancelledContact" in e["text"]]
    assert mine and "NOT send" in mine[0]["text"]


def test_ledger_empty_state_is_honest():
    init_db()
    ledger = receipts_ledger()
    assert ledger["count"] == 0
    assert "empty" in ledger["note"].lower() or "thin" in ledger["note"].lower()
    # format makes the empty state legible, not alarming
    assert "hasn't recorded" in format_ledger(ledger)


def test_ledger_endpoint():
    r = client.get("/cockpit/receipts?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body and "by_source" in body and "sources" in body


def test_ledger_endpoint_source_filter():
    init_db()
    _seed_whatsapp_draft("sent")
    r = client.get("/cockpit/receipts?source=whatsapp")
    assert r.status_code == 200
    assert all(e["source"] == "whatsapp" for e in r.json()["entries"])


def test_ledger_leads_activity_tab():
    body = client.get("/ui").text
    assert "What Mongoose did" in body
    assert "cockpit/receipts" in body
