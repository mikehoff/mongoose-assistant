"""Guest phone timeout fix: /device/chat bounds its wait so a slow local model returns a graceful
'still thinking' reply within budget instead of leaving the phone's HTTP client to time out (the
'guest's phone suddenly times out even though nothing changed, still on the LAN' report)."""
import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_GUEST_CHAT_BUDGET_S"] = "0.3"  # tiny budget for the test
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _pair_guest():
    from mongoose.devices import create_pairing_code, claim_pairing_code
    code = create_pairing_code("trusted_guest")
    cs = code["code"] if isinstance(code, dict) else code
    claim = claim_pairing_code(cs, "Sam's iPhone", "phone")
    return claim["device_token"]


def test_slow_chat_returns_graceful_within_budget(monkeypatch):
    _fresh()
    token = _pair_guest()

    async def slow_chat(*a, **k):
        await asyncio.sleep(5)  # far longer than the 0.3s test budget
        return {"reply": "too late"}

    import mongoose.llm
    monkeypatch.setattr(mongoose.llm, "chat_with_memory", slow_chat)

    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    import time
    t0 = time.time()
    r = c.post("/device/chat", headers={"x-mongoose-device-token": token},
               json={"message": "hello"})
    elapsed = time.time() - t0
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Current contract: the turn is NOT cancelled; the route hands back a job id and an honest
    # "still working" reply, and the phone polls /device/chat/poll for the answer.
    assert body.get("pending") is True
    assert body.get("job_id")
    assert "still working" in body["reply"].lower()
    # responded within a small multiple of the budget, NOT the 5s the model would have taken
    assert elapsed < 3, f"took {elapsed}s, should return near the 0.3s budget"


def test_fast_chat_returns_normally(monkeypatch):
    _fresh()
    token = _pair_guest()

    async def fast_chat(*a, **k):
        return {"reply": "hi there"}

    import mongoose.llm
    monkeypatch.setattr(mongoose.llm, "chat_with_memory", fast_chat)

    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/device/chat", headers={"x-mongoose-device-token": token},
               json={"message": "hello"})
    assert r.status_code == 200
    assert r.json()["reply"] == "hi there"
    assert r.json().get("pending") is not True


def test_resend_recovers_reply_instead_of_dead_end(monkeypatch):
    """The idempotency guard must never dead-end. When the phone loses a reply and replays the SAME
    client_msg_id (its outbox does exactly this after a slow turn outlives the POST), the re-send must
    resurface the real answer, not the old "still working on your last message, no need to resend"
    that stranded the owner indefinitely. Here the first turn completes and persists its reply; the
    replay must return THAT reply from history."""
    _fresh()
    token = _pair_guest()

    from mongoose.services.memory import add_message

    async def chat(message, conversation_id=None, **k):
        # Mimic the real turn persisting its assistant reply into the thread.
        add_message(conversation_id, "assistant", "the real answer")
        return {"reply": "the real answer"}

    import mongoose.llm
    monkeypatch.setattr(mongoose.llm, "chat_with_memory", chat)

    from fastapi.testclient import TestClient
    from mongoose.app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r1 = c.post("/device/chat", headers={"x-mongoose-device-token": token},
                    json={"message": "hello", "client_msg_id": "abc"})
        assert r1.json()["reply"] == "the real answer"
        # Phone lost r1 and replays the SAME id: must recover the answer, not dead-end.
        r2 = c.post("/device/chat", headers={"x-mongoose-device-token": token},
                    json={"message": "hello", "client_msg_id": "abc"})
        b2 = r2.json()
    assert b2["ok"] is True
    assert b2.get("duplicate") is True
    assert b2["reply"] == "the real answer"
    assert "no need to resend" not in b2["reply"].lower()


def test_resend_while_in_flight_resumes_polling(monkeypatch):
    """A replay of the SAME id while the original turn is STILL generating must hand back the same
    job id (pending) so the phone resumes polling it, rather than starting a second turn or dead-ending."""
    _fresh()
    token = _pair_guest()

    async def slow_chat(*a, **k):
        await asyncio.sleep(5)  # far longer than the 0.3s test budget
        return {"reply": "eventually"}

    import mongoose.llm
    monkeypatch.setattr(mongoose.llm, "chat_with_memory", slow_chat)

    from fastapi.testclient import TestClient
    from mongoose.app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r1 = c.post("/device/chat", headers={"x-mongoose-device-token": token},
                    json={"message": "hello", "client_msg_id": "xyz"})
        b1 = r1.json()
        assert b1.get("pending") is True and b1.get("job_id")
        r2 = c.post("/device/chat", headers={"x-mongoose-device-token": token},
                    json={"message": "hello", "client_msg_id": "xyz"})
        b2 = r2.json()
    assert b2.get("duplicate") is True
    assert b2.get("pending") is True
    assert b2.get("job_id") == b1.get("job_id")  # SAME turn, resume polling
    assert "no need to resend" not in (b2.get("reply") or "").lower()
