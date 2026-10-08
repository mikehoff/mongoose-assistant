"""Tests for the v0.14.1 QA audit remediation: webhook guard, local-search policy,
outbound FTS, notification FTS, contact resolution, social tool routing, scheduler
on crawler, backfill mode, and verify-data social coverage."""
import hashlib
import hmac
import json

import pytest

from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- #1 webhook reaches its HMAC verifier (non-loopback) ---------------------
def test_webhook_path_is_allowlisted_past_global_guard(db, monkeypatch):
    import mongoose.app as app_mod
    # the webhook prefix must be in the public allowlist so the guard doesn't 403 it
    assert any("/whatsapp/webhook" in p for p in app_mod._PUBLIC_PREFIXES)


def test_webhook_still_rejects_unverified(db, monkeypatch):
    monkeypatch.delenv("WHATSAPP_HOOK_HMAC_KEY", raising=False)
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.post("/whatsapp/webhook/waha", content=b'{"event":"message"}')
    assert r.status_code == 401   # allowlisted past guard, but route still verifies


def test_webhook_accepts_valid_hmac_end_to_end(db, monkeypatch):
    monkeypatch.setenv("WHATSAPP_HOOK_HMAC_KEY", "k")
    body = json.dumps({"event": "message", "session": "default",
                       "payload": {"id": "Z1", "from": "g@c.us", "fromMe": False,
                                   "body": "hi", "notifyName": "Sam", "timestamp": "1"}}).encode()
    sig = hmac.new(b"k", body, hashlib.sha512).hexdigest()
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.post("/whatsapp/webhook/waha", content=body, headers={"X-Webhook-Hmac": sig})
    assert r.status_code == 200 and r.json().get("ingested") == 1


# --- #2 local search returns body; shared surface masks ----------------------
def test_private_message_searchable_locally_masked_on_shared(db):
    from mongoose.services import whatsapp as wa
    wa.index_message(wa.WhatsAppMessage(chat_id="c@c.us", contact="Clinic", direction="in",
                                        wa_message_id="m1", body="dermatology results ready",
                                        privacy="high"))
    local = wa.search_messages("dermatology")
    assert local and local[0]["snippet"] == "dermatology results ready"
    shared = wa.search_messages("dermatology", shared_surface=True)
    assert shared and shared[0]["snippet"] == "[private message]"


# --- #3 sent outbound is searchable ------------------------------------------
def test_sent_outbound_is_indexed_in_fts(db, monkeypatch):
    from mongoose.services import whatsapp as wa
    d = wa.draft_message("Sam", "unique-outbound-marker-xyz", chat_id="g@c.us")
    wa.approve_and_send(d["id"], send_fn=lambda c, b: None)
    assert any("unique-outbound-marker-xyz" in (h["snippet"] or "")
               for h in wa.search_messages("unique-outbound-marker-xyz"))


# --- #4 WhatsApp notifications go through notification FTS --------------------
def test_whatsapp_notification_is_fts_searchable(db):
    from mongoose.services.whatsapp import process_webhook
    process_webhook({
        "event": "message", "session": "default",
        "payload": {"id": "N1", "from": "g@c.us", "fromMe": False,
                    "body": "x", "notifyName": "Samir", "timestamp": "1"},
    }, provider_name="waha")
    from mongoose.services.notifications import search_notifications
    assert any("whatsapp" in (n.get("app") or "") for n in search_notifications("Samir"))


# --- #5 contact resolution before send ---------------------------------------
def test_send_refuses_unknown_contact_with_provider(db, monkeypatch):
    from mongoose.services import whatsapp as wa

    class FakeProvider:
        def send_text(self, chat_id, text):
            raise AssertionError("should not send to an unresolved name")

    monkeypatch.setattr("mongoose.services.whatsapp_providers.base.get_provider",
                        lambda name=None: FakeProvider())
    d = wa.draft_message("UnknownPerson", "hi")   # no chat_id, no alias
    res = wa.approve_and_send(d["id"])
    assert res["ok"] is False and res["error"] == "contact_unresolved"


def test_resolve_contact_learns_from_inbound(db):
    from mongoose.services import whatsapp as wa
    wa.index_message(wa.WhatsAppMessage(chat_id="447700@c.us", contact="Sam", direction="in",
                                        wa_message_id="m1", body="hi"))
    r = wa.resolve_contact("Sam")
    assert r["ok"] and r["chat_id"] == "447700@c.us"


def test_send_uses_resolved_chat_id(db, monkeypatch):
    from mongoose.services import whatsapp as wa
    wa.index_message(wa.WhatsAppMessage(chat_id="447700@c.us", contact="Sam", direction="in",
                                        wa_message_id="m1", body="hi"))
    sent = []

    class FakeProvider:
        def send_text(self, chat_id, text):
            sent.append(chat_id)
            return {"ok": True}

    monkeypatch.setattr("mongoose.services.whatsapp_providers.base.get_provider",
                        lambda name=None: FakeProvider())
    d = wa.draft_message("Sam", "hello")   # name only
    wa.approve_and_send(d["id"])
    assert sent == ["447700@c.us"]   # resolved, not "Sam"


# --- #6 social tools in the chat tool loop -----------------------------------
def test_social_tools_in_allowlist(db):
    from mongoose.tools import TOOLS
    for t in ("what_changed", "ritual", "whatsapp_search", "whatsapp_draft"):
        assert t in TOOLS


def test_no_whatsapp_send_tool_in_allowlist(db):
    from mongoose.tools import TOOLS
    assert not any("whatsapp" in t and "send" in t for t in TOOLS)


def test_social_phrases_route_to_tool():
    from mongoose.role_router import classify_role
    for phrase in ["what changed?", "physio check", "journal me",
                   "draft a soft check-in to Sam", "what did Sam say",
                   "who have I not replied to"]:
        assert classify_role(phrase) == "tool", phrase


def test_social_routing_no_false_positives():
    from mongoose.role_router import classify_role
    for phrase in ["what does close reading mean", "what is a journal",
                   "the breathing techniques in yoga"]:
        assert classify_role(phrase) == "chat", phrase


# --- #8 scheduler uses crawler -----------------------------------------------
def test_scheduler_reindex_uses_crawler(db, tmp_path, monkeypatch):
    import mongoose.services.scheduler as sched
    monkeypatch.setattr(sched, "get_settings",
                        lambda: type("S", (), {"index_roots": (f"{tmp_path}:mac",)})())
    called = {}

    def fake_crawl(path, device="local", mode="fresh"):
        called["used_crawler"] = True
        called["mode"] = mode
        return {"ok": True}
    monkeypatch.setattr("mongoose.services.crawler.crawl_files", fake_crawl)
    import asyncio
    asyncio.run(sched._job_reindex_files())
    assert called.get("used_crawler") and called["mode"] == "fresh"


# --- #9 backfill mode updates backfill state ---------------------------------
def test_backfill_mode_updates_state(db, tmp_path, monkeypatch):
    from mongoose.services import crawler as cr
    import os
    monkeypatch.setattr(cr, "_disk_ok", lambda: True)  # don't depend on sandbox disk
    d = tmp_path / "tree"
    d.mkdir()
    (d / "a.md").write_text("x")
    r = cr.crawl_files(str(d), device="mac", mode="backfill")
    assert r["mode"] == "backfill"
    src = cr.get_source(f"files:{os.path.realpath(str(d))}")
    assert src["backfill_status"] == "done"
    # The end-of-tree fix (week-three lived-use) CLEARS the cursor on completion so the next scheduled
    # pass wraps to the beginning and picks up edits/deletes in earlier filenames. Previously the cursor
    # persisted at the last path, so the crawl kept restarting after the end forever and never re-saw
    # earlier files. So a completed backfill now leaves an empty cursor, not a retained one; the
    # completion signal is backfill_status == "done" (asserted above), not a non-empty cursor.
    assert src["backfill_cursor"] in ("", None)


# --- #10 verify-data covers social tables ------------------------------------
def test_verify_data_checks_whatsapp_and_social(db):
    from mongoose.services import whatsapp as wa
    from mongoose.services.data_verify import verify_data
    wa.index_message(wa.WhatsAppMessage(chat_id="g@c.us", contact="Sam", direction="in",
                                        wa_message_id="m1", body="hi"))
    report = verify_data()
    checks = {f["check"] for f in report["findings"]}
    assert "whatsapp.unique_message_id" in checks
    assert "whatsapp.fts_coverage" in checks
    assert any(c.startswith("checkpoints.") or c.startswith("diary.") or c.startswith("ritual.")
               for c in checks)
