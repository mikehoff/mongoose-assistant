"""Tests for the SearXNG web-search integration in the research worker.

Covers the best-practice contract (format=json, 403 handling, result mapping), the
SSRF/network guard (public must be https, LAN opt-in allowed), opt-in default
(disabled unless configured), and the local-first/web-only-if-thin policy. No real
network: httpx and find are mocked."""
import asyncio

import mongoose.web_search as WS
import mongoose.research_worker as RW


# --- network guard -----------------------------------------------------------
def test_public_http_refused(monkeypatch):
    monkeypatch.setattr(WS, "_is_private_host", lambda h: False)
    try:
        WS._validate_base_url("http://searx.example.org")
        assert False, "public http should be refused"
    except WS.SearxngError as e:
        assert "https" in str(e)


def test_public_https_allowed(monkeypatch):
    monkeypatch.setattr(WS, "_is_private_host", lambda h: False)
    assert WS._validate_base_url("https://searx.example.org/") == "https://searx.example.org"


def test_lan_http_allowed_optin(monkeypatch):
    # a LAN/private host is allowed over http because the user configured it
    monkeypatch.setattr(WS, "_is_private_host", lambda h: True)
    assert WS._validate_base_url("http://192.168.1.50:8080") == "http://192.168.1.50:8080"


def test_invalid_url_refused():
    for bad in ("not-a-url", "ftp://x", ""):
        try:
            WS._validate_base_url(bad)
            assert False, f"{bad!r} should be refused"
        except WS.SearxngError:
            pass


# --- opt-in default ----------------------------------------------------------
def test_enabled_by_default_with_local_instance(monkeypatch):
    """Web search now ships ON by default, pointed at the LOCAL SearXNG instance
    (local-first: queries route through SearXNG on this machine)."""
    monkeypatch.delenv("MONGOOSE_SEARXNG_URL", raising=False)
    monkeypatch.setenv("MONGOOSE_DB_PATH", "/tmp/wstest/data/mongoose.sqlite3")
    from mongoose.settings import Settings
    s = Settings()
    assert s.searxng_url == "http://127.0.0.1:8888"   # local instance, on by default


def test_can_be_disabled_by_emptying_url(monkeypatch):
    """The off switch must still work: setting MONGOOSE_SEARXNG_URL empty disables web
    search entirely."""
    monkeypatch.setenv("MONGOOSE_SEARXNG_URL", "")
    monkeypatch.setenv("MONGOOSE_DB_PATH", "/tmp/wstest/data/mongoose.sqlite3")
    from mongoose.settings import Settings
    s = Settings()
    assert s.searxng_url == ""


def test_search_web_raises_when_disabled(monkeypatch):
    # Fully disabled = no SearXNG AND the native fallback switched off.
    monkeypatch.setattr(WS, "configured_url", lambda: None)
    monkeypatch.setenv("MONGOOSE_WEB_SEARCH_FALLBACK", "false")
    try:
        asyncio.run(WS.search_web("anything"))
        assert False
    except WS.SearxngError as e:
        assert "disabled" in str(e)


def test_search_web_falls_back_to_native_when_no_searxng(monkeypatch):
    # With SearXNG unconfigured but the fallback allowed (default), web search still works via the
    # native path , this is the "Docker is off" case. DuckDuckGo is mocked so the test is offline.
    monkeypatch.setattr(WS, "configured_url", lambda: None)
    monkeypatch.setenv("MONGOOSE_WEB_SEARCH_FALLBACK", "true")

    async def _fake_ddg(query, limit, time_range):
        return [{"kind": "web", "title": "Kelela", "where": "https://en.wikipedia.org/wiki/Kelela",
                 "open": "https://en.wikipedia.org/wiki/Kelela", "snippet": "singer", "engine": "duckduckgo"}]

    monkeypatch.setattr(WS, "_duckduckgo_search", _fake_ddg)
    res = asyncio.run(WS.search_web("kelela"))
    assert res and res[0]["engine"] == "duckduckgo"


# --- API contract (mocked httpx) ---------------------------------------------
def _mock_httpx(monkeypatch, *, status=200, payload=None):
    class FakeResp:
        status_code = status
        text = ""
        def json(self): return payload or {}
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            FakeClient.last_url = url
            return FakeResp()
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return FakeClient


def test_search_web_maps_results(monkeypatch):
    monkeypatch.setattr(WS, "configured_url", lambda: "https://searx.example.org")
    monkeypatch.setattr(WS, "_validate_base_url", lambda u: u.rstrip("/"))
    fc = _mock_httpx(monkeypatch, payload={"results": [
        {"title": "Qwen3.5 benchmarks", "url": "https://ex.org/q", "content": "scores...", "engine": "duckduckgo"}]})
    out = asyncio.run(WS.search_web("qwen3.5 benchmarks"))
    assert "format=json" in fc.last_url            # documented JSON contract
    assert out[0]["kind"] == "web"
    assert out[0]["where"] == "https://ex.org/q" and out[0]["open"] == "https://ex.org/q"
    assert out[0]["snippet"] == "scores..."


def test_search_web_403_is_clear(monkeypatch):
    monkeypatch.setattr(WS, "configured_url", lambda: "https://searx.example.org")
    monkeypatch.setattr(WS, "_validate_base_url", lambda u: u.rstrip("/"))
    _mock_httpx(monkeypatch, status=403)
    try:
        asyncio.run(WS.search_web("x"))
        assert False
    except WS.SearxngError as e:
        assert "json" in str(e).lower()   # tells the user to enable json format


# --- local-first, web-only-if-thin -------------------------------------------
def _mock_worker_model(monkeypatch):
    async def fake_ask(prompt, system=None, role="chat", model=None):
        return '{"findings":["note"],"used_sources":[],"confidence":"low","gaps":[]}'
    import mongoose.llm as llm
    monkeypatch.setattr(llm, "ask_ollama", fake_ask)


def test_rich_local_does_not_touch_web(monkeypatch):
    _mock_worker_model(monkeypatch)
    import mongoose.services.find as F
    monkeypatch.setattr(F, "find", lambda q, limit=10: {"results": [
        {"kind": "file", "title": "a", "where": "~/a", "snippet": "x", "open": "~/a"},
        {"kind": "file", "title": "b", "where": "~/b", "snippet": "y", "open": "~/b"}]})
    monkeypatch.setattr(WS, "is_enabled", lambda: True)
    async def boom(q, limit=5): raise AssertionError("web ran despite rich local")
    monkeypatch.setattr(WS, "search_web", boom)
    notes = asyncio.run(RW.research_worker("q"))
    assert notes["used_web"] is False


def test_thin_local_augments_with_web(monkeypatch):
    _mock_worker_model(monkeypatch)
    import mongoose.services.find as F
    monkeypatch.setattr(F, "find", lambda q, limit=10: {"results": []})
    monkeypatch.setattr(WS, "is_enabled", lambda: True)
    async def web_ok(q, limit=5):
        return [{"kind": "web", "title": "W", "where": "https://x", "open": "https://x",
                 "snippet": "web", "engine": "ddg"}]
    monkeypatch.setattr(WS, "search_web", web_ok)
    notes = asyncio.run(RW.research_worker("obscure"))
    assert notes["used_web"] is True
    assert any(s["kind"] == "web" for s in notes["sources"])


def test_thin_local_web_disabled_records_gap(monkeypatch):
    _mock_worker_model(monkeypatch)
    import mongoose.services.find as F
    monkeypatch.setattr(F, "find", lambda q, limit=10: {"results": []})
    monkeypatch.setattr(WS, "is_enabled", lambda: False)
    notes = asyncio.run(RW.research_worker("obscure"))
    assert notes["used_web"] is False
    # honest: records that web was unavailable rather than silently omitting
    assert any("web search disabled" in g for g in notes.get("gaps", []))
