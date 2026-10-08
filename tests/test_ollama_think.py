import pytest

import mongoose.llm as llm
from mongoose.settings import get_settings


def test_think_defaults_false(monkeypatch):
    monkeypatch.delenv("OLLAMA_THINK", raising=False)
    assert get_settings().ollama_think is False


def test_think_parses_true(monkeypatch):
    monkeypatch.setenv("OLLAMA_THINK", "true")
    assert get_settings().ollama_think is True


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, captured):
        self._payload = payload
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self._captured["request"] = json
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_payload_includes_think_flag(monkeypatch):
    monkeypatch.setenv("OLLAMA_THINK", "false")
    captured = {}
    resp = {"message": {"role": "assistant", "content": "hi"}}
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp, captured))
    out = await llm.ask_ollama("hello", role="chat")
    assert out == "hi"
    assert captured["request"]["think"] is False


@pytest.mark.asyncio
async def test_empty_content_falls_back_to_thinking(monkeypatch):
    captured = {}
    # content empty, answer only in thinking -> must not return silence
    resp = {"message": {"role": "assistant", "content": "", "thinking": "the answer"}}
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp, captured))
    out = await llm.ask_ollama("hello", role="chat")
    assert out == "the answer"


class _Resp404:
    status_code = 404
    def raise_for_status(self):
        import httpx
        req = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
        resp = httpx.Response(404, request=req)
        raise httpx.HTTPStatusError("not found", request=req, response=resp)
    def json(self):
        return {}


class _Client404:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None): return _Resp404()


@pytest.mark.asyncio
async def test_http_error_raises_local_model_error(monkeypatch):
    from mongoose.llm import LocalModelError
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _Client404())
    with pytest.raises(LocalModelError) as ei:
        await llm.ask_ollama("hello", role="chat")
    assert "error" in ei.value.user_message.lower()


def test_keep_alive_value_types(monkeypatch):
    from mongoose.settings import get_settings
    # bare numbers (incl -1 "forever" and 0 "unload now") must be ints
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "-1")
    assert get_settings().keep_alive_value == -1
    assert isinstance(get_settings().keep_alive_value, int)
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "0")
    assert get_settings().keep_alive_value == 0
    # duration strings stay strings
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "10m")
    assert get_settings().keep_alive_value == "10m"
