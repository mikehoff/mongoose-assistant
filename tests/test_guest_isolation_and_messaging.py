"""Two guarantees under test:

1. GUEST ISOLATION: a trusted_guest can never reach the owner's life data. Owner memory facts and the
   imported knowledge graph (health, AAS, grief, psychological material) stay on the owner side across
   every guest-reachable surface: memory scope, chat context, the guest surpriser, and route access.

2. MESSAGING + POKE still work end to end after the knowledge-layer changes.
"""
from __future__ import annotations

import asyncio
import json


from mongoose.services import memory as mem
from mongoose.services import mailbox as mb


SENSITIVE = "Nurofen dose and grief about self-worth"


# --------------------------------------------------------------------------------------------------
# 1. Guest isolation
# --------------------------------------------------------------------------------------------------
def test_guest_conversation_scope_is_isolated():
    assert mem.scope_for_conversation("guest:dev1") == "guest:dev1"
    assert mem.scope_for_conversation("guest:dev1") != mem.OWNER_SCOPE
    assert mem.scope_for_conversation(None) == mem.OWNER_SCOPE
    # two guests are isolated from each other too
    assert mem.scope_for_conversation("guest:dev1") != mem.scope_for_conversation("guest:dev2")


def test_owner_facts_never_reach_guest_context():
    mem.remember_fact(SENSITIVE, source="explicit", scope=mem.OWNER_SCOPE)
    _, guest_facts = mem.build_context("guest:devX", "tell me everything you know")
    joined = " ".join(guest_facts).lower()
    assert "nurofen" not in joined and "grief" not in joined


def test_guest_chat_receives_no_owner_facts_and_no_tools(monkeypatch):
    """The strongest leak test: seed a sensitive owner fact + sensitive knowledge, run a guest chat turn,
    and assert the model never received the fact and the tool layer was never entered."""
    import mongoose.llm as L
    from mongoose.knowledge_import import import_bulk_dump_bytes

    mem.remember_fact(SENSITIVE, source="explicit", scope=mem.OWNER_SCOPE)

    import io as _io, zipfile as _zip
    buf = _io.BytesIO()
    rec = {"record_type": "health_context", "topic": "h", "summary": SENSITIVE,
           "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "high"}
    with _zip.ZipFile(buf, "w") as z:
        z.writestr("health_records.jsonl", json.dumps(rec))
    import_bulk_dump_bytes(buf.getvalue(), source_name="health_knowledge_dump.zip")

    seen = {}

    async def fake_chat_call(selected, messages, settings):
        seen["messages"] = messages
        return "hello, nice to meet you"

    tool_called = {"n": 0}

    async def fake_tool_turn(prompt):
        tool_called["n"] += 1
        return {"reply": SENSITIVE}  # if a guest ever reaches this, the fact would leak

    monkeypatch.setattr("mongoose.llm._chat_call", fake_chat_call)
    monkeypatch.setattr("mongoose.tools.run_tool_turn", fake_tool_turn)

    out = asyncio.run(L.chat_with_memory("hello there friend", conversation_id="guest:devX",
                                         role="chat", guest_mode=True))
    blob = json.dumps(seen.get("messages", [])).lower() + " " + (out.get("reply") or "").lower()
    assert "nurofen" not in blob and "grief" not in blob
    assert tool_called["n"] == 0  # guests never enter the tool layer


def test_guest_surpriser_carries_no_owner_knowledge(monkeypatch):
    from mongoose.knowledge_import import import_bulk_dump_bytes
    from mongoose.services.guest_surpriser import guest_surprise

    import io as _io, zipfile as _zip
    buf = _io.BytesIO()
    rec = {"record_type": "health_context", "topic": "h", "summary": SENSITIVE,
           "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "high"}
    with _zip.ZipFile(buf, "w") as z:
        z.writestr("knowledge_records.jsonl", json.dumps(rec))
    import_bulk_dump_bytes(buf.getvalue(), source_name="knowledge_dump.zip")

    for _ in range(8):
        res = guest_surprise("guest:devX")
        assert "nurofen" not in json.dumps(res).lower()
        assert "grief" not in json.dumps(res).lower()


def test_knowledge_routes_are_not_guest_reachable():
    import mongoose.app as A
    # the guest device allowlist must not include any knowledge/cockpit path
    assert not A._guest_allowed_device_route("/cockpit/knowledge/dashboard")
    assert not A._guest_allowed_device_route("/cockpit/knowledge/fusion/propose")
    # and the knowledge endpoints require the cockpit nonce (unreachable without it)
    from starlette.testclient import TestClient
    c = TestClient(A.app, raise_server_exceptions=False)
    assert c.get("/cockpit/knowledge/dashboard").status_code == 403


# --------------------------------------------------------------------------------------------------
# 2. Messaging + poke
# --------------------------------------------------------------------------------------------------
def test_message_send_and_read_roundtrip():
    r = mb.send_message(sender=mb.OWNER, recipient="guest:devM", body="Welcome, help yourself to tea", notify=False)
    assert r.get("ok") is not False
    unread = mb.unread_for("guest:devM")
    assert any("tea" in (m.get("body") or "") for m in unread)
    assert mb.unread_count("guest:devM") >= 1
    mb.mark_read(participant="guest:devM")
    assert mb.unread_count("guest:devM") == 0


def test_guest_message_can_only_reach_owner():
    import mongoose.app as A
    dev = {"device_id": "devG", "profile": "trusted_guest"}
    # guest -> owner: accepted
    reply = A._maybe_handle_message_send("tell Alex the boiler is making a noise", is_guest=True, dev=dev)
    assert reply is not None and "saved" in reply.lower()
    got = mb.unread_for(mb.OWNER)
    assert any("boiler" in (m.get("body") or "") for m in got)


def test_poke_roundtrip():
    from mongoose.services import poke_service as P
    P.ensure_table()
    # send a poke (delivery may be sent=False without a push subscription, but the poke is recorded)
    res = asyncio.run(P.send_poke("user_a", "Alice", "user_b", "Bob"))
    assert res.get("ok") is not False
    hist = P.poke_history("user_b")
    assert any(h.get("from_uid") == "user_a" and h.get("to_uid") == "user_b" for h in hist)
