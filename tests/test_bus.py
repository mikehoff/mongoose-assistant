import pytest
from mongoose.db import init_db
from mongoose.services import bus


@pytest.fixture
def busdb(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def test_send_and_list(busdb):
    bus.send_message(sender="a", to="b", subject="hi", type="note", body="hello")
    msgs = bus.list_messages(to="b")
    assert len(msgs) == 1 and msgs[0]["subject"] == "hi"
    assert msgs[0]["status"] == "unread"


def test_read_archives(busdb):
    m = bus.send_message(sender="a", to="b", subject="task one", type="task")
    full = bus.read_message(m["id"])
    assert full["status"] == "read"
    # no longer in unread list
    assert bus.list_messages(to="b") == []
    # but still readable from archive
    again = bus.read_message(m["id"])
    assert again is not None and again["subject"] == "task one"


def test_filters(busdb):
    bus.send_message(sender="code", to="alex", subject="brief x", type="brief")
    bus.send_message(sender="alex", to="code", subject="task y", type="task")
    assert len(bus.list_messages(to="alex")) == 1
    assert len(bus.list_messages(sender="alex")) == 1
    assert len(bus.list_messages(type="brief")) == 1


def test_search(busdb):
    bus.send_message(sender="a", to="b", subject="boiler issue", type="alert", body="the boiler tripped")
    bus.send_message(sender="a", to="b", subject="weather", type="note", body="sunny")
    res = bus.search_messages("boiler")
    assert len(res) == 1 and "boiler" in res[0]["subject"]


def test_invalid_type_rejected(busdb):
    r = bus.send_message(sender="a", to="b", subject="x", type="bogus")
    assert r["ok"] is False


def test_multiline_subject_rejected(busdb):
    r = bus.send_message(sender="a", to="b", subject="line1\nline2", type="note")
    assert r["ok"] is False


def test_reindex_recovers_from_disk(busdb):
    bus.send_message(sender="a", to="b", subject="one", type="note")
    bus.send_message(sender="a", to="b", subject="two", type="note")
    # wipe index, rebuild from files
    from mongoose.db import connect
    with connect() as con:
        con.execute("DELETE FROM bus_messages")
        con.execute("DELETE FROM bus_fts")
    assert bus.reindex() == 2
    assert len(bus.list_messages(to="b")) == 2


def test_atomic_roundtrip_format(busdb):
    m = bus.send_message(sender="claude_code", to="alex", subject="fmt", type="brief", body="body text")
    full = bus.read_message(m["id"], archive=False)
    assert full["from"] == "claude_code"
    assert full["to"] == "alex"
    assert full["body"] == "body text"


def test_assist_routes_control_vs_chat(busdb, monkeypatch):
    from fastapi.testclient import TestClient
    import mongoose.app as appmod
    from mongoose.app import app

    async def fake_tool(text):
        return {"reply": "Turning on the lamp.", "executed": True, "tool": "control_home"}
    async def fake_chat(text, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "Hello there.", "remembered": []}
    async def fake_ask_ollama(text, *, system=None, role="chat"):
        return "Hello there."
    monkeypatch.setattr(appmod, "run_tool_turn", fake_tool)
    monkeypatch.setattr(appmod, "chat_with_memory", fake_chat)
    monkeypatch.setattr(appmod, "ask_ollama", fake_ask_ollama)

    c = TestClient(app)
    r1 = c.post("/assist", json={"text": "turn on the lamp"})
    assert r1.json()["controlled"] is True
    r2 = c.post("/assist", json={"text": "how are you today"})
    assert r2.json()["controlled"] is False
    assert "Hello" in r2.json()["speech"]
    r3 = c.post("/assist", json={"text": "  "})
    assert r3.json()["controlled"] is False


def test_assist_blocks_remote(busdb):
    from fastapi.testclient import TestClient
    from mongoose.app import app
    rc = TestClient(app, client=("203.0.113.4", 9))
    assert rc.post("/assist", json={"text": "hi"}).status_code == 403
