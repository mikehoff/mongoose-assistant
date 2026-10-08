"""The lounge (phone IRC tab): ONE room shared by the owner, every guest, and mongoose as a
participant. Its own thread (group:irc), never the owner's brain and never a guest's private
thread. Mongoose speaks when addressed by name or asked a question and stays quiet otherwise,
and its turns run in guest_mode with explicit group rules so nothing private can leak into the
shared room."""
from __future__ import annotations

import time
import unittest.mock as mock
import warnings

warnings.filterwarnings("ignore")


def _wait_for(client, headers, predicate, tries=60, delay=0.05):
    """Mongoose's lounge reply is now generated OFF the request path, so poll history until it
    lands (each GET pumps the test event loop, letting the background task run)."""
    for _ in range(tries):
        h = client.get("/device/irc/history?since_id=0", headers=headers).json()
        if any(predicate(m) for m in h.get("messages", [])):
            return True
        time.sleep(delay)
    return False


def _guest(label, name, fact):
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    r = create_guest_invite(owner_label=label)
    assert r.get("ok"), r
    tok = r.get("token") or r["invite_url"].rsplit("/", 2)[-1].replace("/claim", "")
    cl = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": fact})
    assert cl.get("ok"), cl
    return cl["device_token"]


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "irc.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")  # guest invite readiness now also requires actual network binding, not just the allow_remote flag
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "ownertok")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://example.duckdns.org")
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    # These tests exercise the quiet-sidekick rules that apply WITH company present, so default
    # them to "not alone"; the lone-guest engagement path has its own tests (test_lounge_activity).
    import mongoose.services.lounge_surpriser as _LS
    monkeypatch.setattr(_LS, "is_messaging_alone", lambda device_id: False)
    return TestClient(A.app), A


def test_lounge_stores_attributed_lines_and_mongoose_participation_rules(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    jo = _guest("Jo", "Jo", "kayaks")
    H = {"x-mongoose-device-token": jo}

    r = c.post("/device/irc/send", headers=H, json={"text": "hi everyone"})
    body = r.json()
    # (the send response also carries last_id so the client can advance its poll cursor past its
    # own optimistic echo; assert the meaningful fields rather than exact equality)
    assert body["ok"] is True and body["sender"] == "Jo" and body["replied"] is False

    # The reply is generated off the request path now, so capture the prompt context here (a bg
    # task swallows exceptions, so asserting INSIDE the mock would be invisible) and verify outside.
    captured = {}

    async def fake_cwm(prompt, **kw):
        captured["conversation_id"] = kw.get("conversation_id")
        captured["guest_mode"] = kw.get("guest_mode")
        captured["system"] = kw.get("system") or ""
        captured["skip_user_persist"] = kw.get("skip_user_persist")
        from mongoose.services import lounge_surpriser as _LS
        _LS.post_mongoose_line("Hey Jo!", kind="chat")
        return {"reply": "Hey Jo!", "remembered": []}

    with mock.patch("mongoose.llm.chat_with_memory", side_effect=fake_cwm):
        b1 = c.post("/device/irc/send", headers=H, json={"text": "anyone here?"}).json()
        assert b1["ok"] is True and b1.get("pending") is True and b1.get("replied") is False
        assert _wait_for(c, H, lambda m: m["mongoose"] and "Hey Jo!" in m["text"])
    assert captured.get("conversation_id") == "group:irc"
    assert captured.get("guest_mode") is True
    assert captured.get("skip_user_persist") is True  # human line already stored by the handler
    assert "participant" in captured.get("system", "")

    h = c.get("/device/irc/history", headers=H).json()
    senders = [(m["sender"], m["mongoose"]) for m in h["messages"]]
    assert ("Jo", False) in senders and ("mongoose", True) in senders


def test_lounge_never_touches_private_threads(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    from mongoose.db import connect
    # Claim WITH an explicit join-announcement consent (the API defaults to silent by design, so a
    # programmatic claim that omits the flag gets no beat). This checks that a consented beat lands
    # in group:irc and never leaks into a private thread.
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label="Jo")
    tok = inv.get("token") or inv["invite_url"].rsplit("/", 2)[-1].replace("/claim", "")
    cl = claim_guest_invite(tok, {"mongoose_name": "Jo", "public_name": "Jo",
                                  "fun_fact": "kayaks", "announce_join": True})
    jo = cl["device_token"]
    c.post("/device/irc/send", headers={"x-mongoose-device-token": jo}, json={"text": "just the room"})
    with connect() as con:
        for cid in ("mongoose-main", "guest:p:jo"):
            assert con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                               (cid,)).fetchone()[0] == 0
        # Two room events under presence-is-consent: the default join beat, then Jo's line.
        rows = con.execute("SELECT role, content FROM messages WHERE conversation_id='group:irc' "
                           "ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0]["role"] == "assistant" and "Jo" in rows[0]["content"]
        assert "just the room" in rows[1]["content"]


def test_lounge_incremental_polling(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    jo = _guest("Jo", "Jo", "kayaks")
    H = {"x-mongoose-device-token": jo}
    c.post("/device/irc/send", headers=H, json={"text": "one"})
    last = c.get("/device/irc/history", headers=H).json()["messages"][-1]["id"]
    c.post("/device/irc/send", headers=H, json={"text": "two"})
    inc = c.get(f"/device/irc/history?since_id={last}", headers=H).json()["messages"]
    assert [m["text"] for m in inc] == ["two"]


def test_lounge_routes_on_guest_allowlist_and_census_expected(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    assert "/device/irc/send" in A._GUEST_DEVICE_ROUTES
    assert "/device/irc/history" in A._GUEST_DEVICE_ROUTES
    jo = _guest("Jo", "Jo", "kayaks")
    c.post("/device/irc/send", headers={"x-mongoose-device-token": jo}, json={"text": "hello"})
    from mongoose.doctor import run_doctor
    cc = run_doctor()["checks"]["conversations"]
    assert cc["ok"] is True and "group:irc" in cc["detail"]


def test_lounge_tab_present_in_launchpad():
    from mongoose.cockpit import DEVICE_LAUNCHPAD_HTML as H
    assert 'id="tab-irc"' in H and "IRC" in H
    assert 'id="irc-pane"' in H and 'id="irc-log"' in H
    assert "/device/irc/history?since_id=" in H
    assert "/device/irc/send" in H


def test_lounge_starter_host_not_bot(monkeypatch, tmp_path):
    """The starter behaves like a host noticing a lull: one themed starter (trivia,
    culture, music) when someone opens a quiet room; never two in a row; never interrupts an
    active conversation; cooldown between starters; kill switch respected."""
    c, A = _setup(monkeypatch, tmp_path)
    from mongoose.db import connect, set_app_state
    jo = _guest("Jo", "Jo", "kayaks")
    H = {"x-mongoose-device-token": jo}

    h = c.get("/device/irc/history", headers=H).json()
    assert len(h["messages"]) == 1 and h["messages"][0]["mongoose"] is True

    assert len(c.get("/device/irc/history", headers=H).json()["messages"]) == 1

    c.post("/device/irc/send", headers=H, json={"text": "unicorns obviously"})
    set_app_state("lounge_last_starter", "2020-01-01T00:00:00+00:00")
    assert len(c.get("/device/irc/history", headers=H).json()["messages"]) == 2

    with connect() as con:
        con.execute("UPDATE messages SET created_at='2020-01-01T00:00:00' "
                    "WHERE conversation_id='group:irc'")
        con.commit()
    h4 = c.get("/device/irc/history", headers=H).json()
    assert len(h4["messages"]) == 3 and h4["messages"][-1]["mongoose"] is True

    c.post("/device/irc/send", headers=H, json={"text": "still here"})
    with connect() as con:
        con.execute("UPDATE messages SET created_at='2020-01-01T00:00:00' "
                    "WHERE conversation_id='group:irc'")
        con.commit()
    assert len(c.get("/device/irc/history", headers=H).json()["messages"]) == 4

    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTERS", "off")
    set_app_state("lounge_last_starter", "2020-01-01T00:00:00+00:00")
    assert len(c.get("/device/irc/history", headers=H).json()["messages"]) == 4


def test_lounge_starters_cover_the_three_themes():
    import mongoose.app as A
    pool = " ".join(A._LOUNGE_STARTERS).lower()
    assert "trivia" in pool
    assert "music" in pool or "song" in pool
    assert len(A._LOUNGE_STARTERS) >= 20


def test_history_marks_my_own_lines_server_side(monkeypatch, tmp_path):
    """Own-line highlighting used to rely on the client comparing against the pairing display
    name, but attribution prefers the guest profile's lounge name, so a guest whose lounge name
    differed never saw their own lines in gold. The server knows whose line it is; it now says
    so with a per-message mine flag."""
    client, A = _setup(monkeypatch, tmp_path)
    tok_a = _guest("sam", "Samster", "swims")     # lounge name differs from any display name
    tok_b = _guest("visitor", "Kasia", "reads")
    ha = {"X-Mongoose-Device-Token": tok_a}
    hb = {"X-Mongoose-Device-Token": tok_b}
    client.post("/device/irc/send", headers=ha, json={"text": "hej wszystkim"})
    client.post("/device/irc/send", headers=hb, json={"text": "czesc"})
    msgs_a = client.get("/device/irc/history", headers=ha).json()["messages"]
    by_text = {m["text"]: m for m in msgs_a}
    assert by_text["hej wszystkim"]["mine"] is True
    assert by_text["czesc"]["mine"] is False
    msgs_b = client.get("/device/irc/history", headers=hb).json()["messages"]
    by_text_b = {m["text"]: m for m in msgs_b}
    assert by_text_b["hej wszystkim"]["mine"] is False
    assert by_text_b["czesc"]["mine"] is True


def test_lounge_join_announcement_uses_only_consented_guest_card(monkeypatch, tmp_path):
    client, A = _setup(monkeypatch, tmp_path)
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite

    inv = create_guest_invite(owner_label="private-owner-label", suggested_name="Lee")
    token = inv.get("token") or inv["invite_url"].rsplit("/", 1)[-1]
    cl = claim_guest_invite(token, {
        "mongoose_name": "Lee",
        "public_name": "Lee Public",
        "fun_fact": "I collect tiny spoons",
        "announce_join": True,
        "share_fun_fact": True,
    })
    assert cl["ok"], cl
    h = client.get("/device/irc/history", headers={"x-mongoose-device-token": cl["device_token"]}).json()
    texts = [m["text"] for m in h["messages"] if m["mongoose"]]
    joined = "\n".join(texts)
    assert "Lee Public has joined #mongoose" in joined
    assert "tiny spoons" in joined
    assert "private-owner-label" not in joined
    # The fun fact is quoted and ATTRIBUTED to the joiner (not glued on as Mongoose's own
    # words), and Mongoose's sidekick self-intro is no longer crammed into the join line , that
    # three-voices-in-one-line join is exactly what read as confusing.
    assert "Lee Public's fun fact" in joined
    assert "sidekick" not in joined


def test_history_marks_same_display_name_by_device_not_name(monkeypatch, tmp_path):
    client, A = _setup(monkeypatch, tmp_path)
    tok_a = _guest("alex-a", "Alex", "first")
    tok_b = _guest("alex-b", "Alex", "second")
    ha = {"X-Mongoose-Device-Token": tok_a}
    hb = {"X-Mongoose-Device-Token": tok_b}
    client.post("/device/irc/send", headers=ha, json={"text": "from first Alex"})
    client.post("/device/irc/send", headers=hb, json={"text": "from second Alex"})

    msgs_a = {m["text"]: m for m in client.get("/device/irc/history", headers=ha).json()["messages"]}
    msgs_b = {m["text"]: m for m in client.get("/device/irc/history", headers=hb).json()["messages"]}
    assert msgs_a["from first Alex"]["mine"] is True
    assert msgs_a["from second Alex"]["mine"] is False
    assert msgs_b["from first Alex"]["mine"] is False
    assert msgs_b["from second Alex"]["mine"] is True


def test_lounge_model_prompt_gets_public_group_context(monkeypatch, tmp_path):
    client, A = _setup(monkeypatch, tmp_path)
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite

    inv = create_guest_invite(owner_label="Lee internal", suggested_name="Lee")
    token = inv.get("token") or inv["invite_url"].rsplit("/", 1)[-1]
    cl = claim_guest_invite(token, {
        "mongoose_name": "Lee",
        "public_name": "Lee",
        "fun_fact": "I like kayaks",
        "announce_join": True,
        "share_fun_fact": True,
    })
    H = {"X-Mongoose-Device-Token": cl["device_token"]}

    captured = {}

    async def fake_cwm(prompt, **kw):
        captured["conversation_id"] = kw.get("conversation_id")
        captured["guest_mode"] = kw.get("guest_mode")
        captured["system"] = kw.get("system") or ""
        from mongoose.services import lounge_surpriser as _LS
        _LS.post_mongoose_line("Kayaks noted.", kind="chat")
        return {"reply": "Kayaks noted.", "remembered": []}

    with mock.patch("mongoose.llm.chat_with_memory", side_effect=fake_cwm):
        r = client.post("/device/irc/send", headers=H, json={"text": "mongoose what should the room discuss?"}).json()
        assert r["ok"] is True and r.get("pending") is True
        assert _wait_for(client, H, lambda m: m["mongoose"] and "Kayaks noted." in m["text"])
    # The model was handed ONLY public room context (never the hidden owner_label or private data).
    assert captured.get("conversation_id") == "group:irc"
    assert captured.get("guest_mode") is True
    assert "Visible #mongoose room context" in captured.get("system", "")
    assert "Lee: public calling card" in captured.get("system", "")
    assert "kayaks" in captured.get("system", "")
    assert "sidekick" in captured.get("system", "")
    assert "Lee internal" not in captured.get("system", "")


def test_lounge_async_reflection_uses_narrative_after_safe_lull(monkeypatch, tmp_path):
    client, A = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    tok_a = _guest("jo", "Jo", "pizza")
    tok_b = _guest("rin", "Lee", "music")
    ha = {"X-Mongoose-Device-Token": tok_a}
    hb = {"X-Mongoose-Device-Token": tok_b}
    client.post("/device/irc/send", headers=ha, json={"text": "pizza is the correct dinner"})
    client.post("/device/irc/send", headers=hb, json={"text": "pasta is also food diplomacy"})

    h = client.get("/device/irc/history", headers=ha).json()
    mongoose_lines = [m["text"] for m in h["messages"] if m["mongoose"]]
    assert mongoose_lines
    joined = "\n".join(mongoose_lines).lower()
    assert "food" in joined or "snack" in joined or "subplot" in joined
    assert "sidekick" in joined or "previously in #mongoose" in joined or "micro-recap" in joined


def test_lounge_reflection_suppresses_triggering_or_private_context(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    from mongoose.services import lounge_surpriser as LS

    LS.store_human_message(device_id="dev1", sender="Jo", text="I feel suicidal tonight", line="Jo: I feel suicidal tonight")
    res = LS.maybe_lounge_surprise()
    assert res["posted"] is False
    assert res["reason"] == "sensitive recent context"


def test_group_conversation_has_own_memory_scope():
    from mongoose.services.memory import scope_for_conversation
    assert scope_for_conversation("group:irc") == "group:irc"
