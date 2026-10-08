"""HA state-read tool + HA-Assist intent reconstruction (let HA understand ambiguous prompts)."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    for k in ("HA_URL", "HA_TOKEN"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    return d


class _Resp:
    def __init__(self, code, data=None):
        self.status_code = code
        self._d = data if data is not None else []

    def json(self):
        return self._d


# ---- read-state tool ----

def _sync_client(states):
    class C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            return _Resp(200, states)
    return C


def test_read_home_state_summarises(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import mongoose.services.ha_state as HS
    states = [
        {"entity_id": "light.lounge", "state": "on", "attributes": {"friendly_name": "Lounge"}},
        {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen"}},
        {"entity_id": "climate.hall", "state": "heat",
         "attributes": {"friendly_name": "Hall", "current_temperature": 19.5}},
    ]
    monkeypatch.setattr(HS.httpx, "Client", _sync_client(states))
    r = HS.read_home_state()
    assert r["ok"] is True
    assert r["rollup"]["lights_on"] == 1
    assert "light" in r["by_domain"]


def test_read_home_state_flags_panels(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import mongoose.services.ha_state as HS
    states = [{"entity_id": "light.control_hub", "state": "on",
               "attributes": {"friendly_name": "Wall Panel"}}]
    monkeypatch.setattr(HS.httpx, "Client", _sync_client(states))
    r = HS.read_home_state()
    panel = r["by_domain"]["light"][0]
    assert panel.get("panel_protected") is True   # read is fine, but flagged


def test_read_home_state_filters(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import mongoose.services.ha_state as HS
    states = [
        {"entity_id": "cover.garage", "state": "open", "attributes": {"friendly_name": "Garage Door"}},
        {"entity_id": "light.lounge", "state": "on", "attributes": {"friendly_name": "Lounge"}},
    ]
    monkeypatch.setattr(HS.httpx, "Client", _sync_client(states))
    r = HS.read_home_state("garage")
    assert r["rollup"]["entities_matched"] == 1     # only the garage cover matched


def test_read_home_state_not_configured():
    _fresh()
    import mongoose.services.ha_state as HS
    r = HS.read_home_state()
    assert r["ok"] is False and r["reason"] == "not_configured"


def test_home_state_tool_capability_protocol():
    _fresh()
    from mongoose.tools import TOOLS, READ_TOOLS, _tool_catalogue
    assert "home_state" in TOOLS
    assert "home_state" in READ_TOOLS                       # in the read tier
    assert "home_state" in _tool_catalogue()                # selectable by the planner
    assert TOOLS["home_state"]["description"].startswith("Read-only")  # self-reflection recognises it


def test_home_state_reads_not_classified_as_control():
    _fresh()
    from mongoose.role_router import looks_like_home_control, looks_like_home_read
    # questions are reads, never control
    assert looks_like_home_control("are the lounge lights on") is False
    assert looks_like_home_read("are the lounge lights on") is True
    # commands are control
    assert looks_like_home_control("turn the lights on") is True
    # mixed: command wins
    assert looks_like_home_control("the lights are on, turn them off") is True


# ---- HA Assist intent reconstruction ----

def _assist_client(resp_json, code=200):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return _Resp(code, resp_json)

        async def get(self, url, headers=None):
            return _Resp(200, [])
    return C


def test_ha_assist_reconstructs_ambiguous_intent(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    monkeypatch.setattr(H.httpx, "AsyncClient", _assist_client(
        {"response": {"response_type": "action_done",
                      "speech": {"plain": {"speech": "Turned on the lounge lamp."}},
                      "data": {"success": [{"id": "light.lounge_lamp"}], "failed": []}}}))
    r = asyncio.run(H.control_via_ha_assist("turn on the thing by the sofa"))
    assert r["ok"] is True and r["route"] == "ha_assist"
    assert r["targets_succeeded"] == 1


def test_ha_assist_refuses_panel_prompt(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    r = asyncio.run(H.control_via_ha_assist("turn off the wall panel"))
    assert r["ok"] is False and r["reason"] == "panel_safety" and r["fall_back"] is True


def test_ha_assist_no_match_falls_back(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    monkeypatch.setattr(H.httpx, "AsyncClient", _assist_client(
        {"response": {"response_type": "error",
                      "speech": {"plain": {"speech": "Sorry, I didn't understand"}}}}))
    r = asyncio.run(H.control_via_ha_assist("wibble the wozzle"))
    assert r["fall_back"] is True


def test_tool_prefers_ha_assist_for_ambiguous_prompt(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    from mongoose.tools import TOOLS
    monkeypatch.setattr(H.httpx, "AsyncClient", _assist_client(
        {"response": {"response_type": "action_done",
                      "speech": {"plain": {"speech": "Done."}},
                      "data": {"success": [{"id": "light.x"}], "failed": []}}}))
    r = asyncio.run(TOOLS["control_home"]["executor"](
        {"room": "by the sofa", "device_id": "that lamp", "action": "on",
         "_raw_prompt": "turn on the thing by the sofa"}))
    assert r.get("route") == "ha_assist"


def test_all_lights_still_panel_safe_with_raw_prompt(monkeypatch):
    """The bulk 'all' path must win over HA Assist (panel-safe), even with a raw prompt present."""
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    from mongoose.tools import TOOLS
    states = [{"entity_id": "light.a"}, {"entity_id": "light.control_hub"}]
    calls = []

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp(200, states)

        async def post(self, url, headers=None, json=None):
            calls.append(json)
            return _Resp(200)
    monkeypatch.setattr(H.httpx, "AsyncClient", C)
    r = asyncio.run(TOOLS["control_home"]["executor"](
        {"room": "all lights", "action": "on", "_raw_prompt": "turn on all the lights"}))
    assert r["count"] == 1                                   # panel excluded
    assert "light.control_hub" not in calls[0]["entity_id"]
