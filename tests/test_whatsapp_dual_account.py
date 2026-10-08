"""Dual-account WhatsApp: owner_read (Alex's inbox, read) and mongoose_send (Mongoose's own number, send).

Asserts session mapping (incl. legacy single-session fallback), inbound role tagging + search isolation,
outbound defaulting to the Mongoose sender, and the restricted owner-notification channel."""
from __future__ import annotations

import pytest

from mongoose.services import whatsapp as wa
from mongoose.services import whatsapp_accounts as acc
from mongoose.services.whatsapp import WhatsAppMessage, index_message, search_messages
from mongoose.services.whatsapp_accounts import OWNER_READ, MONGOOSE_SEND


def test_legacy_single_session_maps_both_roles(monkeypatch):
    monkeypatch.delenv("WHATSAPP_OWNER_SESSION", raising=False)
    monkeypatch.delenv("WHATSAPP_MONGOOSE_SESSION", raising=False)
    monkeypatch.setenv("WHATSAPP_SESSION", "default")
    assert acc.account_for_role(OWNER_READ).session == "default"
    assert acc.account_for_role(MONGOOSE_SEND).session == "default"
    assert acc.is_dual_mode() is False


def test_explicit_dual_sessions(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_SESSION", "alex")
    monkeypatch.setenv("WHATSAPP_MONGOOSE_SESSION", "mongoose")
    assert acc.account_for_role(OWNER_READ).session == "alex"
    assert acc.account_for_role(MONGOOSE_SEND).session == "mongoose"
    assert acc.is_dual_mode() is True
    assert acc.role_for_session("alex") == OWNER_READ
    assert acc.role_for_session("mongoose") == MONGOOSE_SEND
    assert acc.role_for_session("unknown") == ""


def _index(role, direction, body, mid):
    index_message(WhatsAppMessage(
        chat_id="c@c.us", contact="Someone", direction=direction, wa_message_id=mid,
        body=body, provider_session=role, session_role=role))


def test_owner_inbox_searchable_mongoose_inbound_hidden(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_SESSION", "alex")
    monkeypatch.setenv("WHATSAPP_MONGOOSE_SESSION", "mongoose")
    # Alex's inbound reply (owner_read) -> shown; a reply that hit Mongoose's number (mongoose_send) -> hidden
    _index("owner_read", "in", "lunch tomorrow at noon", "m-owner-in")
    _index("mongoose_send", "in", "lunch tomorrow at noon", "m-mong-in")
    # An outbound Mongoose notification -> stays searchable
    _index("mongoose_send", "out", "lunch tomorrow at noon", "m-mong-out")
    hits = search_messages("lunch")
    roles = {(h["session_role"], h["direction"]) for h in hits}
    assert ("mongoose_send", "in") not in roles     # reply to Mongoose's number hidden from Alex's inbox
    assert ("owner_read", "in") in roles              # Alex's own inbox shown
    assert ("mongoose_send", "out") in roles          # Mongoose's sent notification still searchable
    # opt-in to see everything
    all_hits = search_messages("lunch", include_all_accounts=True)
    assert len(all_hits) >= len(hits) + 1


def test_draft_defaults_to_mongoose_sender(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_SESSION", "alex")
    monkeypatch.setenv("WHATSAPP_MONGOOSE_SESSION", "mongoose")
    d = wa.draft_message("Jordan", "hi from mongoose", chat_id="g@c.us")
    assert d["send_role"] == MONGOOSE_SEND
    from mongoose.db import connect
    with connect() as con:
        row = con.execute("SELECT send_role, send_session FROM whatsapp_drafts WHERE id=?", (d["id"],)).fetchone()
    assert row["send_role"] == MONGOOSE_SEND and row["send_session"] == "mongoose"


def test_owner_notification_channel_is_restricted(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_NUMBER", "447700900123")
    monkeypatch.setenv("WHATSAPP_OWNER_SESSION", "alex")
    monkeypatch.setenv("WHATSAPP_MONGOOSE_SESSION", "mongoose")
    captured = {}

    class FakeProvider:
        def send_text(self, chat_id, text):
            captured["to"] = chat_id
            captured["text"] = text
            return {"ok": True}

    monkeypatch.setattr("mongoose.services.whatsapp_providers.base.get_provider",
                        lambda name=None, role=None: FakeProvider())
    r = wa.send_mongoose_notification_to_owner("system: backup done")
    assert r.get("ok") is True and r.get("sent") is True
    assert captured["to"] == "447700900123@c.us"   # routed to the owner number, not an arbitrary contact

    # no owner number configured -> refuses rather than guessing a contact
    monkeypatch.delenv("WHATSAPP_OWNER_NUMBER", raising=False)
    monkeypatch.delenv("WHATSAPP_SELF_NUMBER", raising=False)
    r2 = wa.send_mongoose_notification_to_owner("noop")
    assert r2.get("ok") is False and r2.get("error") == "no_owner_number"


@pytest.mark.asyncio
async def test_send_to_phone_whatsapp_fallback_when_enabled(monkeypatch):
    monkeypatch.setenv("WHATSAPP_NOTIFICATIONS", "true")
    monkeypatch.setenv("WHATSAPP_OWNER_NUMBER", "447700900123")
    import mongoose.services.notifications as N
    # no web push, no ntfy configured -> owner/system path should fall back to WhatsApp
    monkeypatch.setattr(N, "_whatsapp_owner_fallback", lambda title, body: {"sent": True, "channel": "whatsapp"})
    from mongoose.services.notifications import send_to_phone
    res = await send_to_phone("Mongoose", "backup done")
    assert res.get("channel") == "whatsapp" and res.get("sent") is True


def test_webhook_nested_session_classifies_mongoose_sender(monkeypatch):
    """Fix: some WAHA payload shapes carry the session inside payload (session/sessionName) rather than
    at the top level. Inbound on Mongoose's own sender number must still be tagged mongoose_send."""
    monkeypatch.setenv("WHATSAPP_OWNER_SESSION", "alex")
    monkeypatch.setenv("WHATSAPP_MONGOOSE_SESSION", "mongoose")
    from mongoose.services.whatsapp import process_webhook, search_messages
    process_webhook({
        "event": "message",
        "payload": {"sessionName": "mongoose", "id": "nested-M", "from": "owner@c.us",
                    "fromMe": False, "body": "nested sender reply", "timestamp": "1"},
    }, provider_name="waha")
    # Tagged mongoose_send: hidden from Alex's default inbox search, visible only with the flag.
    assert search_messages("nested sender reply") == []
    hits = search_messages("nested sender reply", include_all_accounts=True)
    assert hits and hits[0]["session_role"] == "mongoose_send"


def test_unanswered_inbound_uses_latest_owner_inbox_row(monkeypatch):
    """Fix: the old grouped query paired direction with MAX(indexed_at) unsafely. A window query now
    takes the actual latest row per contact, and Mongoose-sender inbound is excluded from Alex's list."""
    monkeypatch.setenv("WHATSAPP_OWNER_SESSION", "alex")
    monkeypatch.setenv("WHATSAPP_MONGOOSE_SESSION", "mongoose")
    from mongoose.services.whatsapp import process_webhook, unanswered_inbound

    def owner(mid, frm, fromme, body, ts):
        process_webhook({"event": "message", "session": "alex",
                         "payload": {"id": mid, "from": frm, "fromMe": fromme, "body": body, "timestamp": ts}},
                        provider_name="waha")

    owner("A-in", "friend@c.us", False, "are you there", "1")
    owner("A-out", "friend@c.us", True, "yes", "2")          # replied -> answered
    owner("B-in", "mum@c.us", False, "call me", "3")          # unanswered
    process_webhook({"event": "message",
                     "payload": {"session": "mongoose", "id": "C-in", "from": "bot@c.us",
                                 "fromMe": False, "body": "reply to mongoose", "timestamp": "4"}},
                    provider_name="waha")                     # mongoose-sender inbound -> excluded

    contacts = {r["contact"] for r in unanswered_inbound()}
    assert "friend@c.us" not in contacts
    assert "mum@c.us" in contacts
    assert "bot@c.us" not in contacts


def test_unanswered_inbound_honours_within_hours(monkeypatch):
    """The signature promises a time window; stale unanswered threads must age out of the list."""
    monkeypatch.setenv("WHATSAPP_OWNER_SESSION", "alex")
    monkeypatch.setenv("WHATSAPP_MONGOOSE_SESSION", "mongoose")
    from mongoose.db import connect
    from mongoose.services.whatsapp import WhatsAppMessage, index_message, unanswered_inbound
    index_message(WhatsAppMessage(chat_id="o@c.us", contact="OldFriend", direction="in",
                                  wa_message_id="old1", body="ping", session_role="owner_read"))
    index_message(WhatsAppMessage(chat_id="n@c.us", contact="NewFriend", direction="in",
                                  wa_message_id="new1", body="ping", session_role="owner_read"))
    with connect() as con:
        con.execute("UPDATE whatsapp_messages SET indexed_at='2000-01-01T00:00:00+00:00' "
                    "WHERE wa_message_id='old1'")
    recent = {r["contact"] for r in unanswered_inbound(within_hours=72)}
    assert "NewFriend" in recent and "OldFriend" not in recent
    alltime = {r["contact"] for r in unanswered_inbound(within_hours=0)}
    assert {"NewFriend", "OldFriend"} <= alltime
