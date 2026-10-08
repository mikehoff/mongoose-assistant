"""Tests for the chat-path disorder fix (4 parts):

  PART 1 atomic per-conversation turn lock: two turns on the SAME conversation id are serialised
         (the second reads the first's persisted exchange); DIFFERENT conversation ids run in parallel.
  PART 2 deterministic intent pre-router: unambiguous "recommend <song> to <who>" / "message <who>: <x>"
         shapes route to the exact tool; a stated preference does NOT.
  PART 3 confirm-gate: recommend_song / message_person are proposed (confirmation token) not sent inline.
  PART 4 stable client render ordering: the chat-history payload carries the monotonic message `id`.

These exercise the real code paths; the local model call is stubbed so nothing needs Ollama.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "data"))
    from mongoose.db import init_db
    init_db()
    return tmp_path


def _neutralise_routing(monkeypatch):
    """Force chat_with_memory past every deterministic short-circuit to the model path, so the turn
    lock (not a short-circuit) is what we are measuring. classify_role->chat keeps it out of the tool
    loop; the intent resolver returns a non-None intent no handler claims, so no keyword detector fires."""
    monkeypatch.setattr("mongoose.role_router.classify_role", lambda p: "chat", raising=False)
    monkeypatch.setattr("mongoose.services.intent_resolver.resolve",
                        lambda p, guest=False: ("smalltalk", 1.0), raising=False)


# ---------------------------------------------------------------------------------------------------
# PART 1: atomic turn lock
# ---------------------------------------------------------------------------------------------------

def test_same_conversation_turns_are_serialised(db, monkeypatch):
    _neutralise_routing(monkeypatch)
    from mongoose import llm

    seen: list[list[str]] = []

    async def stub_chat_call(selected, messages, settings):
        # Record the user messages this model call saw (system + replayed history + the new prompt).
        seen.append([m["content"] for m in messages if m.get("role") == "user"])
        await asyncio.sleep(0.05)  # widen the window an unlocked turn would interleave in
        return "reply::" + (messages[-1]["content"] if messages else "")

    monkeypatch.setattr(llm, "_chat_call", stub_chat_call)

    async def run():
        await asyncio.gather(
            llm.chat_with_memory("first message here", conversation_id="conv-A"),
            llm.chat_with_memory("second message here", conversation_id="conv-A"),
        )

    asyncio.run(run())

    assert len(seen) == 2
    # If serialised, the turn that ran SECOND replayed the first turn's persisted user+assistant, so its
    # model call saw TWO user messages (the earlier prompt + its own). Without the lock both turns read
    # empty history and each would see only its own single user message.
    assert max(len(s) for s in seen) == 2, f"turns interleaved (no serialisation): {seen}"
    assert min(len(s) for s in seen) == 1


def test_different_conversations_run_in_parallel(db, monkeypatch):
    _neutralise_routing(monkeypatch)
    from mongoose import llm

    both_inside = asyncio.Event()
    arrivals = {"n": 0}

    async def stub_chat_call(selected, messages, settings):
        arrivals["n"] += 1
        if arrivals["n"] >= 2:
            both_inside.set()
        # Each call blocks until the OTHER conversation's call is also inside the model call. If the two
        # conversations shared a lock, only one could ever be here and this would time out.
        await asyncio.wait_for(both_inside.wait(), timeout=3.0)
        return "ok"

    monkeypatch.setattr(llm, "_chat_call", stub_chat_call)

    async def run():
        await asyncio.gather(
            llm.chat_with_memory("hello from A", conversation_id="conv-A"),
            llm.chat_with_memory("hello from B", conversation_id="conv-B"),
        )

    asyncio.run(run())  # would raise TimeoutError if guest/owner convs were serialised against each other
    assert arrivals["n"] == 2


def test_turn_lock_registry_is_per_conversation(db, monkeypatch):
    from mongoose import llm

    async def run():
        a1 = llm._turn_lock_for("conv-A")
        a2 = llm._turn_lock_for("conv-A")
        b1 = llm._turn_lock_for("conv-B")
        assert a1 is a2          # same conversation -> same lock (real serialisation)
        assert a1 is not b1      # different conversations -> different locks (parallelism)

    asyncio.run(run())


# ---------------------------------------------------------------------------------------------------
# PART 2: deterministic intent pre-router
# ---------------------------------------------------------------------------------------------------

def test_router_matches_recommend_song_to_named_person():
    from mongoose.services.chat_prerouter import deterministic_tool_route
    out = deterministic_tool_route("recommend Don't Tell Me to Jo")
    assert out is not None
    tool, args = out
    assert tool == "recommend_song"
    assert args["song"] == "Don't Tell Me"
    assert args["to"] == "Jo"


def test_router_matches_tell_person_to_listen():
    from mongoose.services.chat_prerouter import deterministic_tool_route
    tool, args = deterministic_tool_route("tell Sam to listen to Bohemian Rhapsody")
    assert tool == "recommend_song"
    assert args["to"] == "Sam"
    assert args["song"] == "Bohemian Rhapsody"


def test_router_matches_message_person():
    from mongoose.services.chat_prerouter import deterministic_tool_route
    tool, args = deterministic_tool_route("message Toby: dinner is at 8")
    assert tool == "message_person"
    assert args["to"] == "Toby"
    assert args["text"] == "dinner is at 8"


def test_router_ignores_stated_preference():
    from mongoose.services.chat_prerouter import deterministic_tool_route
    # A stated favourite must NOT be read as a command to broadcast a song.
    assert deterministic_tool_route("well Madonna Ray of Light obviously") is None
    assert deterministic_tool_route("my go-to was Madonna") is None
    assert deterministic_tool_route("I love Blackbird by the Beatles") is None


# ---------------------------------------------------------------------------------------------------
# PART 3: confirm-gate for outward/social actions
# ---------------------------------------------------------------------------------------------------

def test_recommend_song_and_message_person_are_sensitive():
    from mongoose.tools import SENSITIVE_TOOLS, ACTING_TOOLS
    for name in ("recommend_song", "message_person", "whatsapp_draft"):
        assert name in SENSITIVE_TOOLS, f"{name} must be confirm-gated"
        assert name in ACTING_TOOLS


def test_recommend_song_proposes_before_sending(db, monkeypatch):
    from mongoose.services import sensitive_tools as S
    import mongoose.tools as T

    calls = []

    async def fake_send(args):
        calls.append(args)
        return {"ok": True, "detail": "Passed 'X' to Jo."}

    monkeypatch.setattr(T, "_tool_recommend_song", fake_send)

    p = S.propose_sensitive_action("recommend_song", {"song": "Blackbird", "to": "Jo"})
    assert p["needs_confirmation"] is True
    assert "confirm_token" in p and p["confirm_token"]
    assert calls == []  # proposing must NOT send

    async def confirm():
        return await S.confirm_sensitive_action_async(p["confirm_token"])

    res = asyncio.run(confirm())
    assert res.get("ok") is True
    assert calls == [{"song": "Blackbird", "to": "Jo"}]  # executed exactly once, only after confirm


def test_prerouter_recommend_asks_to_confirm_then_sends(db, monkeypatch):
    """End-to-end through chat_with_memory: a routed recommend returns a confirm proposal (nothing sent);
    the follow-up 'confirm <token>' executes it via the same gate."""
    _neutralise_routing(monkeypatch)
    import mongoose.tools as T

    sent = []

    async def fake_send(args):
        sent.append(args)
        return {"ok": True, "detail": f"Passed {args.get('song')!r} to {args.get('to')}."}

    monkeypatch.setattr(T, "_tool_recommend_song", fake_send)
    from mongoose import llm

    out = asyncio.run(llm.chat_with_memory("recommend Blackbird to Jo", conversation_id="conv-social"))
    assert out["source"] == "prerouter:recommend_song"
    assert "confirm" in out["reply"].lower()
    assert sent == []  # asked, did not send
    token = out["proposal"]["confirm_token"]

    out2 = asyncio.run(llm.chat_with_memory(f"confirm {token}", conversation_id="conv-social"))
    assert out2["source"] == "social_confirm"
    assert sent == [{"song": "Blackbird", "to": "Jo", "_sender": "owner"}]
    assert "Jo" in out2["reply"]


def test_all_sensitive_tools_have_confirm_dispatch():
    """Guards the recurring 'proposed but never executes' bug for the newly-gated social tools too."""
    import inspect
    from mongoose.tools import SENSITIVE_TOOLS
    from mongoose.services import sensitive_tools as S
    src = inspect.getsource(S._execute) + inspect.getsource(S._execute_async)
    for tool in SENSITIVE_TOOLS:
        assert f'"{tool}"' in src or f"'{tool}'" in src, f"{tool} has no confirm dispatch branch"


# ---------------------------------------------------------------------------------------------------
# PART 1 smoke: a normal single turn still completes (no deadlock/hang)
# ---------------------------------------------------------------------------------------------------

def test_single_normal_turn_completes(db, monkeypatch):
    _neutralise_routing(monkeypatch)
    from mongoose import llm

    async def stub_chat_call(selected, messages, settings):
        return "hello back"

    monkeypatch.setattr(llm, "_chat_call", stub_chat_call)

    async def run():
        return await asyncio.wait_for(
            llm.chat_with_memory("just chatting here", conversation_id="conv-smoke"), timeout=5.0)

    out = asyncio.run(run())
    assert out["reply"] == "hello back"


# ---------------------------------------------------------------------------------------------------
# PART 4: chat-history payload carries the monotonic id (stable client ordering key)
# ---------------------------------------------------------------------------------------------------

def test_cockpit_chat_history_payload_carries_id(db):
    from fastapi.testclient import TestClient
    import mongoose.app as A
    from mongoose.services.memory import add_message, ensure_conversation, DEFAULT_CONVERSATION_ID

    ensure_conversation(DEFAULT_CONVERSATION_ID)
    add_message(DEFAULT_CONVERSATION_ID, "user", "first")
    add_message(DEFAULT_CONVERSATION_ID, "assistant", "second")

    c = TestClient(A.app)
    nonce = c.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = c.get("/cockpit/chat/history", headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) >= 2
    for m in msgs:
        assert "id" in m and isinstance(m["id"], int)
        assert "created_at" in m
    # ids are strictly ascending: the authoritative render order the client sorts by.
    ids = [m["id"] for m in msgs]
    assert ids == sorted(ids)
