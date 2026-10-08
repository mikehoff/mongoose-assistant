"""Regression tests for the HA/Alexa/MCP control-path hardening (the robustness audit).
These cover the exact control-boundary bugs found: payload override, missing domain/service
inference, HA-first ordering, fallback privacy policy, outcome validation, and MCP acting
opt-in , the small places that matter for real household control."""
import asyncio
import os


from mongoose.db import init_db
from mongoose.services.home_router import (
    HomeCommand, _alexa_fallback_allowed, route_home_command,
)
import mongoose.services.home_router as H


# --- P0.1: payload entity_id must not override the resolved target ----------

def test_payload_entity_id_mismatch_is_rejected(monkeypatch):
    os.environ["HA_URL"] = "http://ha.local"
    os.environ["HA_TOKEN"] = "x"
    init_db()
    cmd = HomeCommand(room="r", device_id="d", action="turn_off",
                      entity_id="light.safe", domain="light", service="turn_off",
                      privacy="low", payload={"entity_id": "light.OTHER"})
    r = asyncio.run(H._call_home_assistant(cmd))
    assert r.success is False
    assert "entity_id_mismatch" in (r.reason or "")


# --- P0.2: entity_id + action must infer domain/service ---------------------

def test_infer_domain_service_from_entity_and_action():
    from mongoose.tools import _infer_domain_service
    c = _infer_domain_service(HomeCommand(room="r", device_id="d", action="turn_off",
                                          entity_id="light.lounge"))
    assert c.domain == "light" and c.service == "turn_off"


def test_infer_does_not_override_explicit_service():
    from mongoose.tools import _infer_domain_service
    c = _infer_domain_service(HomeCommand(room="r", device_id="d", action="turn_off",
                                          entity_id="light.x", domain="light", service="toggle"))
    assert c.service == "toggle"      # explicit choice preserved


# --- P1.1: HA is always tried first -----------------------------------------

def test_ha_tried_first_even_with_learned_alexa_preference(monkeypatch):
    init_db()
    monkeypatch.setattr(H, "_get_preference", lambda k: "alexa")
    order = []
    from mongoose.services.home_router import RouteAttempt

    async def fha(cmd):
        order.append("home_assistant")
        return RouteAttempt("home_assistant", False, "x")

    async def fal(cmd):
        order.append("alexa")
        return RouteAttempt("alexa", False, "x")

    monkeypatch.setattr(H, "_call_home_assistant", fha)
    monkeypatch.setattr(H, "_call_alexa_bridge", fal)
    cmd = HomeCommand(room="r", device_id="d", action="turn_off", entity_id="light.x",
                      domain="light", service="turn_off", privacy="low")
    asyncio.run(route_home_command(cmd))
    assert order[0] == "home_assistant"


# --- P1.2: low-risk control is Alexa-eligible regardless of privacy default --

def test_low_risk_control_alexa_eligible_at_medium_privacy():
    cmd = HomeCommand(room="r", device_id="d", action="turn_off", entity_id="light.x",
                      domain="light", service="turn_off", privacy="medium")
    assert _alexa_fallback_allowed(cmd) is True


def test_lock_still_requires_low_privacy_for_alexa():
    cmd = HomeCommand(room="r", device_id="d", action="lock", entity_id="lock.x",
                      domain="lock", service="lock", privacy="medium")
    assert _alexa_fallback_allowed(cmd) is False


# --- P1.3: outcome validation -----------------------------------------------

def _fake_ha(monkeypatch, post_code, get_state):
    class FakeResp:
        def __init__(self, code, state=None):
            self.status_code = code
            self._state = state
            self.headers = {"content-type": "application/json"}

        def json(self):
            return {"state": self._state} if self._state is not None else {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **k):
            return FakeResp(post_code)

        async def get(self, url, **k):
            return FakeResp(200, get_state)

    monkeypatch.setattr(H.httpx, "AsyncClient", lambda **k: FakeClient())


def test_state_mismatch_is_not_success(monkeypatch):
    os.environ["HA_URL"] = "http://ha.local"
    os.environ["HA_TOKEN"] = "x"
    init_db()
    _fake_ha(monkeypatch, 200, "on")    # asked to turn off, still on
    cmd = HomeCommand(room="r", device_id="d", action="turn_off", entity_id="light.x",
                      domain="light", service="turn_off", privacy="low")
    r = asyncio.run(H._call_home_assistant(cmd))
    assert r.success is False
    assert "state_mismatch" in (r.reason or "")


def test_matching_state_is_verified_success(monkeypatch):
    os.environ["HA_URL"] = "http://ha.local"
    os.environ["HA_TOKEN"] = "x"
    init_db()
    _fake_ha(monkeypatch, 200, "off")
    cmd = HomeCommand(room="r", device_id="d", action="turn_off", entity_id="light.x",
                      domain="light", service="turn_off", privacy="low")
    r = asyncio.run(H._call_home_assistant(cmd))
    assert r.success is True
    assert r.response.get("outcome") == "verified"


def test_toggle_is_accepted_unverified(monkeypatch):
    os.environ["HA_URL"] = "http://ha.local"
    os.environ["HA_TOKEN"] = "x"
    init_db()
    _fake_ha(monkeypatch, 200, "on")
    cmd = HomeCommand(room="r", device_id="d", action="toggle", entity_id="light.x",
                      domain="light", service="toggle", privacy="low")
    r = asyncio.run(H._call_home_assistant(cmd))
    assert r.success is True
    assert r.response.get("outcome") == "accepted_unverified"


# --- P1.4: MCP acting tool is opt-in ----------------------------------------

def test_mcp_control_home_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MONGOOSE_MCP_ENABLE_ACTING", raising=False)
    from mongoose.mcp_server import _tools
    tools = _tools()
    handler = tools["control_home"]["handler"]
    r = asyncio.run(handler({"entity_id": "light.x", "action": "turn_off"}))
    assert r["ok"] is False
    assert "disabled over MCP" in r["error"]


def test_mcp_control_home_enabled_with_optin(monkeypatch):
    monkeypatch.setenv("MONGOOSE_MCP_ENABLE_ACTING", "true")
    # mcp_server imports _tool_home locally from .tools, so patch it at the source module.
    import mongoose.tools as T

    async def fake_tool(args):
        return {"ok": True, "routed": True}

    monkeypatch.setattr(T, "_tool_home", fake_tool)
    import mongoose.mcp_server as M
    tools = M._tools()
    r = asyncio.run(tools["control_home"]["handler"]({"entity_id": "light.x", "action": "turn_off"}))
    assert r.get("routed") is True
