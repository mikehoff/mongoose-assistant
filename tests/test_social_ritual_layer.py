"""Tests for the v0.14 social + ritual layer: WhatsApp bridge (with the hard
no-autosend safety boundary), ritual engine, what-changed deltas, diary, receipts,
and the brief's social signal."""
import pytest

from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- WhatsApp: indexing + search + privacy -----------------------------------
def test_whatsapp_index_and_search(db):
    from mongoose.services import whatsapp as wa
    wa.index_message(wa.WhatsAppMessage(chat_id="g", contact="Jordan", direction="in",
                                        wa_message_id="m1", body="thinking of you"))
    assert any(r["contact"] == "Jordan" for r in wa.search_messages("thinking"))


def test_whatsapp_private_message_masked(db):
    from mongoose.services import whatsapp as wa
    wa.index_message(wa.WhatsAppMessage(chat_id="c", contact="Clinic", direction="in",
                                        wa_message_id="p1", body="your results are ready",
                                        privacy="high"))
    # Local-first: a private message IS searchable by body for the authenticated
    # local user (the plaintext is already on disk).
    local = wa.search_messages("results")
    assert local and local[0]["contact"] == "Clinic"
    assert local[0]["snippet"] == "your results are ready"
    # But on a SHARED surface (spoken / HA / Alexa), private content is masked.
    shared = wa.search_messages("results", shared_surface=True)
    assert shared and shared[0]["snippet"] == "[private message]"


def test_whatsapp_index_idempotent(db):
    from mongoose.services import whatsapp as wa
    wa.index_message(wa.WhatsAppMessage(chat_id="g", contact="Jordan", direction="in",
                                        wa_message_id="dup", body="hi"))
    r = wa.index_message(wa.WhatsAppMessage(chat_id="g", contact="Jordan", direction="in",
                                            wa_message_id="dup", body="hi"))
    assert r.get("skipped") == "already_indexed"


# --- WhatsApp: the SAFETY BOUNDARY (no autosend) -----------------------------
def test_draft_does_not_send(db):
    from mongoose.services import whatsapp as wa
    d = wa.draft_message("Jordan", "hope today's being kind")
    assert d["status"] == "pending"
    # nothing was transmitted; the draft just exists pending
    assert wa.list_drafts() and wa.list_drafts()[0]["id"] == d["id"]


def test_send_requires_explicit_approval_with_draft_id(db):
    from mongoose.services import whatsapp as wa
    d = wa.draft_message("Jordan", "small hello")
    sent = []
    res = wa.approve_and_send(d["id"], send_fn=lambda c, b: sent.append((c, b)))
    assert res["ok"] and res["sent"] is True
    assert sent == [("Jordan", "small hello")]   # transport called exactly once


def test_draft_cannot_be_sent_twice(db):
    from mongoose.services import whatsapp as wa
    d = wa.draft_message("Jordan", "x")
    wa.approve_and_send(d["id"], send_fn=lambda c, b: None)
    again = wa.approve_and_send(d["id"], send_fn=lambda c, b: None)
    assert again["ok"] is False and "sent" in again["error"]


def test_no_mcp_tool_can_send_whatsapp(db):
    # The model-facing MCP surface must expose draft but NOT send.
    from mongoose.mcp_server import _tools
    t = _tools()
    assert "whatsapp_draft" in t
    assert not any("whatsapp" in name and "send" in name for name in t)


def test_cancelled_draft_not_sendable(db):
    from mongoose.services import whatsapp as wa
    d = wa.draft_message("Jordan", "x")
    wa.cancel_draft(d["id"])
    res = wa.approve_and_send(d["id"], send_fn=lambda c, b: None)
    assert res["ok"] is False


# --- rituals -----------------------------------------------------------------
def test_ritual_match_by_phrase(db):
    from mongoose.services.rituals import match_ritual
    assert match_ritual("Mongoose, physio check").name == "physio"
    assert match_ritual("breathing minute").name == "breathing"
    assert match_ritual("nonsense") is None


def test_ritual_start_and_complete(db):
    from mongoose.services.rituals import start_ritual, complete_ritual, ritual_stats
    s = start_ritual("physio")
    assert s["ok"] and s["script"] and s["duration_s"] == 120
    complete_ritual(s["log_id"], "completed")
    stats = ritual_stats()["by_ritual"]
    assert stats["physio"]["completed"] == 1


def test_ritual_cooldown_flag(db):
    from mongoose.services.rituals import start_ritual
    start_ritual("breathing")
    again = start_ritual("breathing")
    assert "recently_done" in again   # flagged, not blocked


# --- what changed ------------------------------------------------------------
def test_what_changed_reports_then_resets(db):
    from mongoose.services import whatsapp as wa
    from mongoose.services.journal_layer import what_changed
    wa.index_message(wa.WhatsAppMessage(chat_id="g", contact="Jordan", direction="in",
                                        wa_message_id="w1", body="hey"))
    first = what_changed(acknowledge=True)
    assert first["changed"] and "Jordan" in first["summary"]
    # second call: nothing new (checkpoint advanced)
    second = what_changed()
    assert not second["changed"]


def test_what_changed_peek_does_not_reset(db):
    from mongoose.services import whatsapp as wa
    from mongoose.services.journal_layer import what_changed
    wa.index_message(wa.WhatsAppMessage(chat_id="g", contact="Jordan", direction="in",
                                        wa_message_id="w2", body="hey"))
    what_changed(acknowledge=False)   # peek
    again = what_changed(acknowledge=False)
    assert again["changed"]   # still shows because peek didn't advance checkpoint


# --- diary + receipts --------------------------------------------------------
def test_receipt_is_honest_and_logged(db):
    from mongoose.services.journal_layer import receipt, diary_today
    text = receipt("Indexed phone files", {"indexed": 318, "attachments": 12, "skipped": 2})
    assert "318 indexed" in text and "12 attachments" in text
    assert any("318 indexed" in e["summary"] for e in diary_today())


def test_diary_today_and_week(db):
    from mongoose.services.journal_layer import log_diary, diary_today, diary_week
    log_diary("ritual", "helped with one breathing reset")
    log_diary("file", "found the ScottishPower attachment")
    assert len(diary_today()) == 2
    week = diary_week()
    assert week["by_kind"].get("ritual") == 1 and week["by_kind"].get("file") == 1


# --- brief social signal -----------------------------------------------------
def test_brief_has_social_signal(db):
    import asyncio
    from mongoose.services import whatsapp as wa
    from mongoose.services.brief import gather, cross_signals
    wa.index_message(wa.WhatsAppMessage(chat_id="g", contact="Jordan", direction="in",
                                        wa_message_id="s1", body="hey"))
    picture = asyncio.run(gather())
    sigs = cross_signals(picture)
    assert any(s.startswith("SOCIAL") and "Jordan" in s for s in sigs)
