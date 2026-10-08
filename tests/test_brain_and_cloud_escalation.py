"""Two field bugs, both closed:

1. smoke_test.sh's automated health check hit the LIVE /llm/chat endpoint with role="chat" and no
   isolation. That endpoint's code comment says plainly: "SINGLE BRAIN... IRC chat, voice, the
   cockpit browser, and the phone all share one memory." So an internal readiness probe was
   writing its literal canned prompt/response directly into the user's real, permanent
   conversation on every install/update run, indistinguishable from something they actually
   typed -- and with no prior entries visible, it could look exactly like "my real history is
   gone, replaced by this fake conversation." Fixed with an explicit persist=false opt-out that
   defaults to True (today's behaviour, unchanged) for every real UI surface.

2. The Overview tab's status strip concatenated a raw, underscored check key directly against
   its detail text with zero separator (<b>key</b><em>detail</em>, no space), producing exactly
   "cloud_escalationdisabled by default". Separately, CLOUD_ESCALATION_ENABLED had no UI control
   anywhere in the app -- only a terminal .env edit could change it, contradicting every other
   setup step -- so configuring OpenAI/Gemini keys (which DOES make their own integration cards
   green) could never make this chip agree, because nothing could ever turn it on. Fixed with a
   real toggle backed by a working endpoint, and readable label formatting."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_llm_chat_request_has_persist_flag_defaulting_true():
    from mongoose.app import ChatRequest
    req = ChatRequest(prompt="hello", role="chat")
    assert req.persist is True  # unchanged default for every real UI surface


def test_persist_false_skips_the_single_brain_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "brain.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    with connect() as con:
        con.execute("INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?,?,?,?)",
                   (DEFAULT_CONVERSATION_ID, "main", "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
        con.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                   (DEFAULT_CONVERSATION_ID, "user", "a real prior message", "2026-01-01T00:00:00"))
        con.commit()

    def count():
        with connect() as con:
            return con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    import unittest.mock as mock
    from fastapi.testclient import TestClient
    import mongoose.app as A

    before = count()
    with mock.patch("mongoose.app.ask_ollama", return_value="Hi there!"):
        c = TestClient(A.app)
        r = c.post("/llm/chat", json={"prompt": "say hi in three words",
                                      "role": "chat", "persist": False})
    assert r.status_code == 200
    after = count()
    assert after == before, "persist:false must never write into the single-brain conversation"


def test_smoke_test_sh_uses_persist_false():
    t = open("scripts/smoke_test.sh").read()
    i = t.index('"/llm/chat"')
    seg = t[i:i + 300]
    assert '"persist":false' in seg


def test_cloud_escalation_toggle_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "toggle.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    from mongoose.settings import get_settings

    assert get_settings().cloud_escalation_enabled is False
    c = TestClient(A.app)
    r = c.post("/cockpit/cloud-escalation", headers={"x-cockpit-nonce": A._COCKPIT_NONCE},
              json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert get_settings().cloud_escalation_enabled is True

    r2 = c.post("/cockpit/cloud-escalation", headers={"x-cockpit-nonce": A._COCKPIT_NONCE},
               json={"enabled": False})
    assert r2.status_code == 200
    assert get_settings().cloud_escalation_enabled is False


def test_cloud_escalation_toggle_requires_cockpit_nonce():
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.post("/cockpit/cloud-escalation", json={"enabled": True})
    assert r.status_code == 403


def test_status_strip_label_is_humanized_with_real_separator():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "function humanizeKey(key)" in html
    assert '<b>${esc(key)}</b><em>' not in html  # the exact squished pattern
    assert '${detail?` <em>${esc(detail)}</em>`:""}' in html


def test_cloud_escalation_chip_has_a_working_toggle_button():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "async function toggleCloudEscalation(btn,enable)" in html
    assert 'toggleCloudEscalation(this,true)' in html
    assert 'toggleCloudEscalation(this,false)' in html
