"""Claude Code integration: discovery (local + Pi), the ANTHROPIC_API_KEY billing trap, and
cost-aware routing that keeps free work on the local Gemma loop."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("PI_SSH_HOST", None)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _reload_settings():
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()


def test_discover_returns_local_and_pi():
    _fresh()
    from mongoose.services.claude_code import discover
    d = discover()
    assert "local" in d and "pi" in d
    assert "reuse_target" in d


def test_api_key_trap_detected_locally():
    _fresh()
    os.environ["ANTHROPIC_API_KEY"] = "sk-test"
    _reload_settings()
    from mongoose.services.claude_code import status_report
    rep = status_report()
    assert len(rep["billing_traps"]) >= 1
    assert "API rates" in rep["billing_traps"][0]
    os.environ.pop("ANTHROPIC_API_KEY", None)


def test_no_trap_when_no_api_key():
    _fresh()
    from mongoose.services.claude_code import status_report
    rep = status_report()
    assert rep["billing_traps"] == []


def test_household_routes_to_free_local_loop():
    _fresh()
    from mongoose.services.claude_code import recommend_route
    r = recommend_route("household")
    assert r["route"] == "local_gemma_loop"
    assert r["billing"] == "free"


def test_code_change_routes_to_claude_code_with_billing_note():
    _fresh()
    from mongoose.services.claude_code import recommend_route
    r = recommend_route("code_change")
    assert r["route"] == "claude_code"
    assert "metered" in r["billing"] or "subscription" in r["billing"]


def test_code_change_surfaces_api_key_warning():
    _fresh()
    os.environ["ANTHROPIC_API_KEY"] = "sk-test"
    _reload_settings()
    from mongoose.services.claude_code import recommend_route
    r = recommend_route("code_change")
    assert any("API rates" in w for w in r.get("billing_warnings", []))
    os.environ.pop("ANTHROPIC_API_KEY", None)


def test_endpoint_returns_status(monkeypatch):
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/cockpit/claude-code/status", headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    body = r.json()
    assert "discovery" in body and "headline" in body


# Agentic depth: the local Gemma loop's step ceiling is configurable but safety-bounded.

def test_max_tool_steps_default():
    os.environ.pop("MONGOOSE_MAX_TOOL_STEPS", None)
    import mongoose.tools as T
    assert T._resolve_max_steps() == 4


def test_max_tool_steps_configurable():
    import mongoose.tools as T
    os.environ["MONGOOSE_MAX_TOOL_STEPS"] = "8"
    assert T._resolve_max_steps() == 8
    os.environ.pop("MONGOOSE_MAX_TOOL_STEPS", None)


def test_max_tool_steps_hard_capped():
    import mongoose.tools as T
    os.environ["MONGOOSE_MAX_TOOL_STEPS"] = "999"
    assert T._resolve_max_steps() == 12  # absolute safety ceiling
    os.environ.pop("MONGOOSE_MAX_TOOL_STEPS", None)


def test_max_tool_steps_garbage_safe():
    import mongoose.tools as T
    os.environ["MONGOOSE_MAX_TOOL_STEPS"] = "notanumber"
    assert T._resolve_max_steps() == 4
    os.environ.pop("MONGOOSE_MAX_TOOL_STEPS", None)
