from fastapi.testclient import TestClient

import mongoose.app as app_module
from mongoose.app import app


def test_llm_chat_returns_503_when_local_model_unavailable(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("ollama unavailable")

    monkeypatch.setattr(app_module, "ask_ollama", boom)
    client = TestClient(app)
    r = client.post("/llm/chat", json={"prompt": "hello", "role": "chat"})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "local_model_unavailable"


def test_tool_turn_returns_503_when_tool_model_unavailable(monkeypatch):
    async def boom(prompt):
        raise RuntimeError("tool model unavailable")

    monkeypatch.setattr(app_module, "run_tool_turn", boom)
    client = TestClient(app)
    r = client.post("/llm/chat", json={"prompt": "turn on the kitchen light", "role": "tool"})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "local_tool_model_unavailable"


def test_tool_turn_response_exposes_step_metadata(monkeypatch):
    async def fake_tool_turn(prompt):
        return {
            "tool": "search_files",
            "executed": True,
            "result": {"results": []},
            "steps": [{"tool": "search_files", "args": {"query": "x"}, "result": {"results": []}}],
            "stop_reason": "model_finished",
            "reply": "No matching files.",
        }

    monkeypatch.setattr(app_module, "run_tool_turn", fake_tool_turn)
    client = TestClient(app)
    r = client.post("/llm/chat", json={"prompt": "search my files for x", "role": "tool"})
    assert r.status_code == 200
    data = r.json()
    assert data["steps"]
    assert data["stop_reason"] == "model_finished"
    assert data["tool"] == "search_files"


def test_local_model_error_maps_to_503(monkeypatch):
    from fastapi.testclient import TestClient
    import mongoose.app as app_module
    from mongoose.llm import LocalModelError

    async def boom(*args, **kwargs):
        raise LocalModelError("friendly message", detail="HTTP 404")

    monkeypatch.setattr(app_module, "ask_ollama", boom)
    client = TestClient(app_module.app)
    r = client.post("/llm/chat", json={"prompt": "hi", "role": "chat"})
    assert r.status_code == 503
