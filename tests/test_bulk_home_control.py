"""Bulk 'all the lights' control via HA's live list, with panel-safety entities always excluded."""
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
        self._d = data or []

    def json(self):
        return self._d


def _fake_client(states, calls):
    class FC:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp(200, states)

        async def post(self, url, headers=None, json=None):
            calls.append({"url": url, "json": json})
            return _Resp(200)
    return FC


def test_all_lights_excludes_panel_entities(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    states = [
        {"entity_id": "light.lounge"}, {"entity_id": "light.kitchen"},
        {"entity_id": "light.control_hub"},            # panel , must be excluded
        {"entity_id": "light.control_hub_ensuite"},    # panel , must be excluded
        {"entity_id": "switch.kettle"},                # wrong domain , ignored
    ]
    calls = []
    monkeypatch.setattr(H.httpx, "AsyncClient", _fake_client(states, calls))
    r = asyncio.run(H.control_all_in_domain("light", "on"))
    assert r["ok"] is True
    assert r["count"] == 2                              # only the two safe lights
    targeted = calls[0]["json"]["entity_id"]
    assert "light.lounge" in targeted and "light.kitchen" in targeted
    assert "light.control_hub" not in targeted          # PANEL never swept
    assert "light.control_hub_ensuite" not in targeted
    assert "switch.kettle" not in targeted              # wrong domain


def test_all_lights_off_uses_turn_off(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    calls = []
    monkeypatch.setattr(H.httpx, "AsyncClient", _fake_client([{"entity_id": "light.a"}], calls))
    r = asyncio.run(H.control_all_in_domain("light", "off"))
    assert r["service"] == "light.turn_off"
    assert calls[0]["url"].endswith("/light/turn_off")


def test_bulk_not_configured_is_friendly(monkeypatch):
    _fresh()  # no HA_URL/HA_TOKEN
    import asyncio
    import mongoose.services.home_router as H
    r = asyncio.run(H.control_all_in_domain("light", "on"))
    assert r["ok"] is False and r["reason"] == "not_configured"


def test_bulk_rejects_non_bulkable_domain(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    r = asyncio.run(H.control_all_in_domain("climate", "on"))
    assert r["ok"] is False and "climate" in r["reason"]


def test_tool_routes_all_lights(monkeypatch):
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    import mongoose.services.home_router as H
    from mongoose.tools import TOOLS
    calls = []
    monkeypatch.setattr(H.httpx, "AsyncClient",
                        _fake_client([{"entity_id": "light.a"}, {"entity_id": "light.control_hub"}], calls))
    r = asyncio.run(TOOLS["control_home"]["executor"]({"room": "all the lights", "action": "on"}))
    assert r["count"] == 1                               # panel excluded
    assert "light.control_hub" not in calls[0]["json"]["entity_id"]


def test_single_entity_control_does_not_hit_bulk(monkeypatch):
    """A normal room/device command must NOT be treated as bulk (no 'all' keyword)."""
    _fresh(HA_URL="http://ha.local", HA_TOKEN="t")
    import asyncio
    from mongoose.tools import TOOLS
    bulk_called = {"hit": False}
    import mongoose.services.home_router as H

    async def fake_bulk(domain, action):
        bulk_called["hit"] = True
        return {"ok": True, "count": 0}
    monkeypatch.setattr(H, "control_all_in_domain", fake_bulk)
    # route the single-entity path to a stub so we don't need a live resolver
    import mongoose.services.entity_resolver as ER

    async def fake_resolve(room, device, action):
        return {"entity_id": "light.lounge", "domain": "light", "service": "turn_on"}
    monkeypatch.setattr(ER, "resolve_entity_with_model", fake_resolve)

    async def fake_route(cmd):
        return {"ok": True, "route": "home_assistant", "say": "Sorted."}
    monkeypatch.setattr("mongoose.tools.route_home_command", fake_route)
    r = asyncio.run(TOOLS["control_home"]["executor"]({"room": "lounge", "device_id": "lamp", "action": "on"}))
    assert bulk_called["hit"] is False     # 'lounge' is not 'all' , bulk must not fire
    assert r["ok"] is True
