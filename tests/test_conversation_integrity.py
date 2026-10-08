"""Two confirmed field bugs affecting the single shared conversation's integrity:

1. The System tab's message count queried COUNT(*) FROM messages with no conversation_id
   filter at all, while its own label claimed to represent "your one continuous conversation"
   specifically. Correct only by accident while no other conversation_id was ever used; wrong
   the instant one is.

2. The conversation-clear route (irreversible: wipes DEFAULT_CONVERSATION_ID, the single
   conversation shared by every surface, cockpit/IRC chat/voice/phone) had no cockpit-nonce
   gate at all, unlike every sibling destructive action in this app -- protected only by
   loopback trust, which any process or browser tab on the same Mac satisfies."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_chat_history_count_scoped_to_the_single_conversation(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "count.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID

    with connect() as con:
        con.execute("INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?,?,?,?)",
                   (DEFAULT_CONVERSATION_ID, "main", "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
        for i in range(3):
            con.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                       (DEFAULT_CONVERSATION_ID, "user", f"msg {i}", "2026-01-01T00:00:00"))
        con.execute("INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?,?,?,?)",
                   ("some-other-scratch-conversation", "other", "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
        con.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                   ("some-other-scratch-conversation", "user", "unrelated", "2026-01-01T00:00:00"))
        con.commit()

    from mongoose.transparency import _data_categories
    rows = _data_categories()
    chat_row = next(r for r in rows if r["category"] == "Chat history")
    assert chat_row["count"] == 3


def test_conversation_clear_requires_cockpit_nonce(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "clear.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID

    with connect() as con:
        con.execute("INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?,?,?,?)",
                   (DEFAULT_CONVERSATION_ID, "main", "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
        con.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                   (DEFAULT_CONVERSATION_ID, "user", "precious history", "2026-01-01T00:00:00"))
        con.commit()

    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)

    r0 = c.post(f"/conversation/{DEFAULT_CONVERSATION_ID}/clear")
    assert r0.status_code == 403

    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                        (DEFAULT_CONVERSATION_ID,)).fetchone()[0]
    assert n == 1, "an unauthenticated request must never be able to delete real history"

    r1 = c.post(f"/conversation/{DEFAULT_CONVERSATION_ID}/clear",
               headers={"x-cockpit-nonce": A._COCKPIT_NONCE})
    assert r1.status_code == 200
    assert r1.json()["deleted_messages"] == 1


def test_guest_messages_excluded_from_owner_count_and_reported_separately(monkeypatch, tmp_path):
    """M was right: guests DO write into the same messages table under guest:<device_id>
    conversation ids (see /device/chat), so the unscoped COUNT(*) was not just latently wrong,
    it actively counted guest messages into 'your one continuous conversation' the moment any
    guest chatted. Owner count must exclude them; they must be reported as their own category."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "guest.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    with connect() as con:
        for cid, n in [(DEFAULT_CONVERSATION_ID, 3), ("guest:dev-jo", 2), ("guest:dev-sam", 1)]:
            con.execute("INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?,?,?,?)",
                       (cid, cid, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
            for i in range(n):
                con.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                           (cid, "user", f"m{i}", "2026-01-01T00:00:00"))
        con.commit()
    from mongoose.transparency import _data_categories
    rows = _data_categories()
    assert next(r for r in rows if r["category"] == "Chat history")["count"] == 3
    guest = next(r for r in rows if r["category"] == "Guest chat history")
    assert guest["count"] == 3
    assert "2 isolated guest conversations" in guest["what"]


def test_stale_tab_write_refused_server_side(monkeypatch, tmp_path):
    """The epoch system was advisory only: no write route ever validated the caller's epoch, so
    a superseded browser could write into the shared conversation indefinitely. Now /cockpit/chat
    refuses (409) a write carrying a superseded epoch or a pre-restart boot id, accepts the
    current claimant, and leaves headerless callers (phone route, scripts) unchanged."""
    import unittest.mock as mock
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "epoch.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    NONCE = {"x-cockpit-nonce": A._COCKPIT_NONCE}
    s1 = c.get("/cockpit/session/claim").json()
    s2 = c.get("/cockpit/session/claim").json()

    async def fake_cwm(*a, **k):
        return {"reply": "hi", "remembered": []}

    with mock.patch("mongoose.llm.chat_with_memory", side_effect=fake_cwm):
        r = c.post("/cockpit/chat", json={"text": "stale"},
                  headers={**NONCE, "x-cockpit-epoch": f"{s1['boot_id']}:{s1['epoch']}"})
        assert r.status_code == 409 and r.json()["error"] == "stale_session"
        r2 = c.post("/cockpit/chat", json={"text": "current"},
                   headers={**NONCE, "x-cockpit-epoch": f"{s2['boot_id']}:{s2['epoch']}"})
        assert r2.status_code == 200 and r2.json().get("ok") is True
        r3 = c.post("/cockpit/chat", json={"text": "pre-restart"},
                   headers={**NONCE, "x-cockpit-epoch": f"old-boot:{s2['epoch']}"})
        assert r3.status_code == 409
        r4 = c.post("/cockpit/chat", json={"text": "headerless"}, headers=NONCE)
        assert r4.status_code == 200 and r4.json().get("ok") is True


def test_cockpit_sends_epoch_and_handles_409():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert '"X-Cockpit-Epoch":__ep' in html
    assert '__ep=__cockpitSession.boot_id+":"+__cockpitSession.epoch' in html
    assert "r.status===409" in html
    # regression: a let binding is NOT a window property; reading window.__cockpitSession would
    # always be undefined and silently disable enforcement
    assert "window.__cockpitSession?" not in html


def _mk_claim(label, name, fact):
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    r = create_guest_invite(owner_label=label)
    assert r.get("ok"), r
    tok = r.get("token") or r["invite_url"].rsplit("/", 2)[-1].replace("/claim", "")
    c = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": fact})
    assert c.get("ok"), c
    return c


def test_anonymous_loopback_can_ask_but_never_persist(monkeypatch, tmp_path):
    """The smoke-test incident, made structurally impossible: an anonymous local caller can
    still get answers from /llm/chat and /assist (loopback trust for READING is unchanged) but
    can no longer leave anything behind in the owner's, or any guest's, permanent history --
    not by default, and not by explicitly naming a conversation_id."""
    import unittest.mock as mock
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "anon.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)

    def total():
        with connect() as con:
            return con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    with mock.patch("mongoose.app.ask_ollama", return_value="Hi!"), \
         mock.patch("mongoose.llm.ask_ollama", return_value="Hi!"):
        b = total()
        assert c.post("/llm/chat", json={"prompt": "x", "role": "chat"}).status_code == 200
        assert c.post("/llm/chat", json={"prompt": "x", "role": "chat",
                                         "conversation_id": DEFAULT_CONVERSATION_ID}).status_code == 200
        assert c.post("/llm/chat", json={"prompt": "x", "role": "chat",
                                         "conversation_id": "guest:p:jo"}).status_code == 200
        assert c.post("/assist", json={"text": "hello mongoose"}).status_code == 200
        assert total() == b


def test_owner_token_surfaces_persist_unchanged(monkeypatch, tmp_path):
    import unittest.mock as mock
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "owner.sqlite3"))
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "ownertok")
    from mongoose.db import init_db, connect
    init_db()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)

    async def fake_cwm(prompt, *, conversation_id, role, mode=None, system=None, guest_mode=False):
        with connect() as con:
            con.execute("INSERT OR IGNORE INTO conversations(id,title,created_at,updated_at) "
                        "VALUES(?,?,datetime('now'),datetime('now'))", (conversation_id, "t"))
            con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                        "VALUES (?,?,?,datetime('now'))", (conversation_id, "user", prompt))
            con.commit()
        return {"reply": "ok", "remembered": []}

    def brain():
        with connect() as con:
            return con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                               (DEFAULT_CONVERSATION_ID,)).fetchone()[0]

    with mock.patch("mongoose.app.chat_with_memory", side_effect=fake_cwm), \
         mock.patch("mongoose.llm.chat_with_memory", side_effect=fake_cwm):
        b = brain()
        assert c.post("/llm/chat", headers={"x-mongoose-token": "ownertok"},
                     json={"prompt": "p", "role": "chat"}).status_code == 200
        assert brain() == b + 1
        assert c.post("/assist", headers={"x-mongoose-token": "ownertok"},
                     json={"text": "assist me"}).status_code == 200
        assert brain() == b + 2


def test_guest_person_continuity_across_devices(monkeypatch, tmp_path):
    """One person, one lasting history: the same friend re-invited on a NEW device (invites
    are single-use) continues the SAME conversation; a different guest stays isolated; and a
    legacy device-scoped thread migrates forward once -- including the exact case that first
    broke (legacy thread exists, person thread has no conversations row yet)."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "guests.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")  # guest invite readiness now also requires actual network binding, not just the allow_remote flag
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "ownertok")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://example.duckdns.org")
    from mongoose.db import init_db, connect
    init_db()
    import mongoose.app as A

    cg = _mk_claim("Sam", "Sam", "coffee")
    legacy_id = f"guest:{cg['device_id']}"
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO conversations(id,title,created_at,updated_at) "
                    "VALUES(?,?,datetime('now'),datetime('now'))", (legacy_id, "legacy"))
        con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                    "VALUES (?,?,?,datetime('now'))", (legacy_id, "user", "legacy"))
        con.commit()
    conv_g = A._guest_conv_id({"device_id": cg["device_id"], "profile": "trusted_guest"})
    assert conv_g == "guest:p:sam"
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                           (legacy_id,)).fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                           (conv_g,)).fetchone()[0] == 1

    c1 = _mk_claim("Jo", "Jo", "kayaks")
    conv1 = A._guest_conv_id({"device_id": c1["device_id"], "profile": "trusted_guest"})
    with connect() as con:
        con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                    "VALUES (?,?,?,datetime('now'))", (conv1, "user", "hi from phone"))
        con.commit()
    c2 = _mk_claim("Jo", "Jo", "kayaks more")
    conv2 = A._guest_conv_id({"device_id": c2["device_id"], "profile": "trusted_guest"})
    assert conv2 == conv1 == "guest:p:jo"
    assert conv1 != conv_g
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                           (conv2,)).fetchone()[0] == 1


def test_repeated_init_db_never_touches_messages(monkeypatch, tmp_path):
    """Update survival: init_db runs on every server start after an update swap; it must be
    purely additive to schema and never touch existing conversation rows."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "upd.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    with connect() as con:
        con.execute("INSERT INTO conversations(id,title,created_at,updated_at) "
                    "VALUES(?,?,datetime('now'),datetime('now'))", (DEFAULT_CONVERSATION_ID, "m"))
        con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                    "VALUES (?,?,?,datetime('now'))", (DEFAULT_CONVERSATION_ID, "user", "keep me"))
        con.commit()
    init_db()
    init_db()
    with connect() as con:
        rows = con.execute("SELECT content FROM messages").fetchall()
    assert [r["content"] for r in rows] == ["keep me"]


def test_stale_tab_voice_write_refused_server_side(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "vep.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    s1 = c.get("/cockpit/session/claim").json()
    c.get("/cockpit/session/claim")  # supersede
    r = c.post("/cockpit/voice", content=b"fakeaudio",
              headers={"x-cockpit-nonce": A._COCKPIT_NONCE,
                       "x-cockpit-epoch": f"{s1['boot_id']}:{s1['epoch']}",
                       "content-type": "application/octet-stream"})
    assert r.status_code == 409
    assert r.json()["error"] == "stale_session"


def test_doctor_conversation_census_flags_stray_threads(monkeypatch, tmp_path):
    """The disappeared-chats incident was invisible because nothing listed which conversation
    ids exist. The census flags any id outside the expected shapes (shared brain + guest
    threads) so a mis-chained thread is caught the day it appears."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "census.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    with connect() as con:
        for cid in (DEFAULT_CONVERSATION_ID, "guest:p:jo", "mystery-thread"):
            con.execute("INSERT INTO conversations(id,title,created_at,updated_at) "
                        "VALUES(?,?,datetime('now'),datetime('now'))", (cid, cid))
            con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                        "VALUES (?,?,?,datetime('now'))", (cid, "user", "x"))
        con.commit()
    from mongoose.doctor import run_doctor
    c = run_doctor()["checks"]["conversations"]
    assert c["ok"] is False and "mystery-thread" in c["detail"]
    with connect() as con:
        con.execute("DELETE FROM messages WHERE conversation_id='mystery-thread'")
        con.commit()
    assert run_doctor()["checks"]["conversations"]["ok"] is True


def test_phone_chat_shows_which_thread_it_writes_into():
    """Prevention for the disappeared-chats scenario: a guest-profiled device writing into its
    own isolated thread must be visibly labelled at the moment of chatting, not discovered
    weeks later as apparent data loss."""
    from mongoose.cockpit import DEVICE_LAUNCHPAD_HTML as H
    assert 'id="chat-identity"' in H
    assert "shared conversation with the Mac" in H
    assert "guest chat: your own private thread" in H
