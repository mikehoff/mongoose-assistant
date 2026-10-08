"""Tests for the WhatsApp WAHA provider layer: payload normalisation, dedup, HMAC
verification, webhook route auth, and provider-backed approved-send. All mocked, no
Docker / no real WAHA."""
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


# --- WAHA payload normalisation ----------------------------------------------
def test_waha_normalises_inbound_message():
    from mongoose.services.whatsapp_providers.waha import WahaProvider
    payload = {
        "event": "message",
        "session": "default",
        "payload": {
            "id": "ABC123", "from": "447700900000@c.us", "fromMe": False,
            "body": "hey, thinking of you", "notifyName": "Jordan", "timestamp": "1700000000",
        },
    }
    msgs = WahaProvider().normalise_webhook(payload)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.provider == "waha" and m.provider_message_id == "ABC123"
    assert m.direction == "inbound" and m.sender_name == "Jordan"
    assert m.text == "hey, thinking of you" and m.privacy == "high"


def test_waha_session_status_event_yields_nothing():
    from mongoose.services.whatsapp_providers.waha import WahaProvider
    assert WahaProvider().normalise_webhook({"event": "session.status", "payload": {}}) == []


def test_waha_ignores_message_without_id_or_chat():
    from mongoose.services.whatsapp_providers.waha import WahaProvider
    assert WahaProvider().normalise_webhook({"event": "message", "payload": {"body": "x"}}) == []


# --- HMAC verification (reject bad signatures) -------------------------------
def test_hmac_verify_accepts_valid_rejects_invalid(monkeypatch):
    from mongoose.services.whatsapp_providers import waha
    monkeypatch.setenv("WHATSAPP_HOOK_HMAC_KEY", "secret-key")
    body = b'{"event":"message"}'
    good = hmac.new(b"secret-key", body, hashlib.sha512).hexdigest()
    assert waha.verify_hmac(body, good) is True
    assert waha.verify_hmac(body, "deadbeef") is False
    assert waha.verify_hmac(body, None) is False


def test_hmac_verify_fails_closed_without_key(monkeypatch):
    from mongoose.services.whatsapp_providers import waha
    monkeypatch.delenv("WHATSAPP_HOOK_HMAC_KEY", raising=False)
    # no key configured -> cannot verify -> reject (route must then require a token)
    assert waha.verify_hmac(b"x", "anything") is False


# --- webhook ingestion end-to-end (via service) ------------------------------
def test_process_webhook_ingests_and_dedupes(db):
    from mongoose.services.whatsapp import process_webhook, search_messages
    payload = {
        "event": "message", "session": "default",
        "payload": {"id": "M1", "from": "g@c.us", "fromMe": False,
                    "body": "dinner friday?", "notifyName": "Jordan", "timestamp": "1"},
    }
    r1 = process_webhook(payload, provider_name="waha")
    assert r1["ingested"] == 1
    # duplicate delivery is deduped
    r2 = process_webhook(payload, provider_name="waha")
    assert r2["ingested"] == 0
    # Local-first: the body is searchable locally (privacy=high masks only on shared
    # surfaces, not in the authenticated local index).
    hits = search_messages("dinner")
    assert any(h["contact"] == "Jordan" for h in hits)
    # On a shared surface the body would be masked.
    shared = search_messages("dinner", shared_surface=True)
    assert all(h["snippet"] == "[private message]" for h in shared)


def test_inbound_webhook_creates_notification(db):
    from mongoose.services.whatsapp import process_webhook
    from mongoose.services.notifications import recent_notifications
    process_webhook({
        "event": "message", "session": "default",
        "payload": {"id": "N1", "from": "g@c.us", "fromMe": False,
                    "body": "call me", "notifyName": "Mum", "timestamp": "1"},
    }, provider_name="waha")
    notifs = recent_notifications(hours=24, limit=10)
    assert any(n.get("app") == "whatsapp" for n in notifs)


# --- webhook ROUTE auth (reject unverified) ----------------------------------
def test_webhook_route_rejects_unverified(db, monkeypatch):
    monkeypatch.delenv("WHATSAPP_HOOK_HMAC_KEY", raising=False)
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    # no HMAC, no token -> 401
    r = c.post("/whatsapp/webhook/waha", content=b'{"event":"message"}')
    assert r.status_code == 401


def test_webhook_route_accepts_valid_hmac(db, monkeypatch):
    monkeypatch.setenv("WHATSAPP_HOOK_HMAC_KEY", "k")
    body = json.dumps({"event": "message", "session": "default",
                       "payload": {"id": "R1", "from": "g@c.us", "fromMe": False,
                                   "body": "hi", "notifyName": "Jordan", "timestamp": "1"}}).encode()
    sig = hmac.new(b"k", body, hashlib.sha512).hexdigest()
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.post("/whatsapp/webhook/waha", content=body, headers={"X-Webhook-Hmac": sig})
    assert r.status_code == 200 and r.json().get("ingested") == 1


# --- provider-backed approved send (still gated) -----------------------------
def test_approved_send_uses_provider_transport(db, monkeypatch):
    """approve_and_send with no explicit send_fn must route through the configured
    provider's send_text, and ONLY after approval of a chosen draft."""
    from mongoose.services import whatsapp as wa
    sent = []

    class FakeProvider:
        def send_text(self, chat_id, text):
            sent.append((chat_id, text))
            return {"ok": True}

    monkeypatch.setattr("mongoose.services.whatsapp_providers.base.get_provider",
                        lambda name=None: FakeProvider())
    d = wa.draft_message("Jordan", "hope today's kind", chat_id="g@c.us")
    res = wa.approve_and_send(d["id"])   # no send_fn -> provider transport
    assert res["ok"] and res["sent"] is True
    assert sent == [("g@c.us", "hope today's kind")]


def test_provider_sends_api_key_header_on_every_call(monkeypatch):
    """Regression: the devlikeapro/waha image rejects every /api call with 401 unless
    X-Api-Key is presented. Without it, Connect and QR silently fail and no QR appears.
    Assert WahaProvider attaches the key (from WAHA_API_KEY) to the httpx client for status,
    session start, and QR alike: the exact calls that power the Connect journey."""
    import httpx

    from mongoose.services.whatsapp_providers import waha

    monkeypatch.setenv("WAHA_API_KEY", "test-key-abc")
    seen_headers = []

    class FakeResp:
        status_code = 200
        content = b"{}"
        headers = {"content-type": "application/json"}

        def json(self):
            return []

    class FakeClient:
        def __init__(self, *a, **kw):
            seen_headers.append(dict(kw.get("headers") or {}))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return FakeResp()

        def post(self, *a, **kw):
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    prov = waha.WahaProvider(session="alex")
    prov.status()
    prov.start_session()
    prov.qr()
    assert seen_headers, "provider made no HTTP calls"
    for h in seen_headers:
        assert h.get("X-Api-Key") == "test-key-abc", f"missing/instance api key header: {h}"


def test_provider_sends_no_key_header_when_unset(monkeypatch):
    """When WAHA_API_KEY is unset (older keyless WAHA), send no empty header: an empty
    X-Api-Key would itself be rejected by some builds. _headers() must be empty then."""
    from mongoose.services.whatsapp_providers import waha
    monkeypatch.delenv("WAHA_API_KEY", raising=False)
    assert waha._headers() == {}


def test_model_still_has_no_send_path(db):
    # the MCP surface still exposes draft only, never send
    from mongoose.mcp_server import _tools
    t = _tools()
    assert "whatsapp_draft" in t
    assert not any("whatsapp" in n and "send" in n for n in t)


def test_provider_send_failure_marks_not_sent(db, monkeypatch):
    from mongoose.services import whatsapp as wa

    class BadProvider:
        def send_text(self, chat_id, text):
            raise RuntimeError("WAHA down")

    monkeypatch.setattr("mongoose.services.whatsapp_providers.base.get_provider",
                        lambda name=None: BadProvider())
    d = wa.draft_message("Jordan", "x", chat_id="g@c.us")
    res = wa.approve_and_send(d["id"])
    assert res["ok"] is False and "transport_failed" in res["error"]
    # draft NOT marked sent
    assert wa.list_drafts(status="pending")  # still pending, recoverable
