"""Liveliness: an addressed lounge message must be stored (and thus visible to every participant)
IMMEDIATELY, with mongoose's reply generated off the request path. Previously the human line was
only persisted after the model finished composing (up to ~2 min cold), so the sender's line was
invisible to the room and out of send-order until then."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "ar.sqlite3"),
                 "MONGOOSE_DATA_DIR": str(tmp_path / "d"),
                 "MONGOOSE_ALLOW_REMOTE": "true", "MONGOOSE_HOST": "0.0.0.0",
                 "MONGOOSE_API_TOKEN": "t", "MONGOOSE_LOUNGE_STARTERS": "off",
                 "MONGOOSE_PUBLIC_URL": "https://example.duckdns.org"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app)


def _guest(c, name):
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label=name)
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": "x"})
    return {"x-mongoose-device-token": cl["device_token"]}


def test_addressed_message_stored_immediately_reply_is_async(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    # Stub the model so the test never touches a real ollama and cannot block.
    import mongoose.llm as LLM
    calls = {"n": 0}

    async def fake_cwm(prompt, **kw):
        calls["n"] += 1
        assert kw.get("skip_user_persist") is True  # human line already stored by the handler
        from mongoose.services import lounge_surpriser as _LS
        _LS.post_mongoose_line("stub reply", kind="chat")
        return {"reply": "stub reply"}

    monkeypatch.setattr(LLM, "chat_with_memory", fake_cwm)

    A = _guest(c, "Jo")
    c.post("/device/irc/send", headers=A, json={"text": "hi"})  # consume the one-time orientation
    q = "mongoose what should we cook tonight?"                  # a question -> the model branch
    r = c.post("/device/irc/send", headers=A, json={"text": q}).json()
    # Handler returned WITHOUT waiting on the model: pending, not a synchronous reply.
    assert r["ok"] and r.get("pending") is True and r.get("replied") is False

    # The human line is already in the room, visible to everyone, before any reply.
    texts = [m["text"] for m in c.get("/device/irc/history?since_id=0", headers=A).json()["messages"]]
    assert q in texts

    # And the human line carries correct device attribution right away (no post-hoc mark step that
    # could mis-attribute identical concurrent lines).
    msgs = c.get("/device/irc/history?since_id=0", headers=A).json()["messages"]
    mine = [m for m in msgs if m["text"] == q]
    assert mine and mine[0]["mine"] is True

    # The reply lands out of band via the background task.
    import time
    seen = False
    for _ in range(60):
        h = c.get("/device/irc/history?since_id=0", headers=A).json()["messages"]
        if any(m["mongoose"] and "stub reply" in m["text"] for m in h):
            seen = True
            break
        time.sleep(0.05)
    assert seen and calls["n"] >= 1
