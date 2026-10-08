"""Wiring tests for the three 'ask Mongoose' surfaces: HA Assist (/assist),
Claude (MCP tools), and the Alexa boundary. These verify the wiring with mocked
models; they do not need Ollama, the Pi, or Claude Desktop."""
import asyncio
import pytest
from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- HA: "ask Mongoose" via the /assist endpoint the HA component forwards to ---
def test_ha_assist_home_control(db, monkeypatch):
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod

    async def fake_tool(prompt):
        return {"reply": "Turning on the lamp.", "executed": True,
                "tool": "control_home", "stop_reason": "done"}
    monkeypatch.setattr(app_mod, "run_tool_turn", fake_tool)
    c = TestClient(app_mod.app)
    r = c.post("/assist", json={"text": "turn on the lamp", "conversation_id": "ha"})
    assert r.status_code == 200
    body = r.json()
    assert body["controlled"] is True
    assert body["speech"] == "Turning on the lamp."


def test_ha_assist_chat(db, monkeypatch):
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod

    async def fake_chat(text, **kwargs):
        return {"reply": "Hello there.", "remembered": []}
    monkeypatch.setattr(app_mod, "chat_with_memory", fake_chat)
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "ownertok")
    c = TestClient(app_mod.app)
    r = c.post("/assist", json={"text": "say hello", "conversation_id": "ha"},
               headers={"x-mongoose-token": "ownertok"})
    assert r.status_code == 200, r.json()
    assert r.json()["speech"] == "Hello there."
    assert r.json()["controlled"] is False


def test_ha_assist_empty_text(db):
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.post("/assist", json={"text": "  ", "conversation_id": "ha"})
    assert r.status_code == 200
    assert r.json()["controlled"] is False


# --- Claude: "ask Mongoose" via the MCP tools ---
def test_mcp_exposes_expected_tools(db):
    from mongoose.mcp_server import _tools
    tools = _tools()
    for name in ("bus_send", "bus_inbox", "bus_read", "bus_search",
                 "control_home", "get_weather", "check_availability",
                 "search_email", "search_files"):
        assert name in tools


def test_mcp_bus_roundtrip(db):
    from mongoose.mcp_server import _tools
    tools = _tools()
    send = tools["bus_send"]["handler"]
    inbox = tools["bus_inbox"]["handler"]
    r = asyncio.run(send({"to": "alex", "subject": "hi from claude", "body": "x", "type": "note"}))
    assert r["ok"] is True
    msgs = asyncio.run(inbox({"to": "alex"}))
    assert "hi from claude" in str(msgs)


# --- Alexa: input is NOT supported, by design (output-fallback only) ---
def test_alexa_is_output_only_not_an_input_skill():
    # There is deliberately no Alexa skill/intent handler: "Alexa, ask Mongoose"
    # would require a cloud-hosted skill, which violates local-first. Alexa is only
    # an output fallback. This test documents that as intentional, so nobody later
    # assumes an input path exists.
    # the alexa surface is a fallback for SPEAKING, gated by privacy; there is no
    # inbound alexa route on the app.
    from mongoose.app import app
    routes = {getattr(r, "path", "") for r in app.routes}
    assert not any("alexa" in p.lower() for p in routes), "no inbound alexa route should exist"


def test_doctor_reports_ask_mongoose_surfaces(db):
    from mongoose.doctor import run_doctor
    r = run_doctor()
    assert "ask_mongoose" in r
    for surface in ("home_assistant_assist", "claude_mcp", "alexa_ask"):
        assert surface in r["ask_mongoose"]
        assert "ready" in r["ask_mongoose"][surface]
    # alexa input is never "ready" (unsupported by design)
    assert r["ask_mongoose"]["alexa_ask"]["ready"] is False


def test_doctor_ha_assist_ready_when_configured(db, monkeypatch):
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "secret")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    from mongoose.doctor import run_doctor
    r = run_doctor()
    assert r["ask_mongoose"]["home_assistant_assist"]["ready"] is True
