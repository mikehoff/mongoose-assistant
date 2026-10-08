"""Live connector probes (configured-means-usable) + PWA mobile surface scaffolding."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
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


# --- connector probes: configured != usable ---

def test_probe_all_returns_structured_summary():
    _fresh()
    from mongoose.connector_probes import probe_all
    agg = probe_all()
    assert "summary" in agg and "connectors" in agg
    sm = agg["summary"]
    for k in ("total", "configured", "usable", "broken", "unproven", "broken_names"):
        assert k in sm


def test_probe_distinguishes_configured_from_usable():
    """An unconfigured connector is configured=False, never a false green."""
    _fresh()
    from mongoose.connector_probes import probe_imap, probe_microsoft, probe_home_assistant
    for probe in (probe_imap, probe_microsoft, probe_home_assistant):
        r = probe()
        assert r.configured is False
        assert r.usable is False  # not configured => not usable, stated plainly


def test_probe_never_raises_each_returns_result():
    _fresh()
    from mongoose.connector_probes import probe_names, probe_one
    for name in probe_names():
        r = probe_one(name)
        assert r.name == name
        assert hasattr(r, "configured") and hasattr(r, "usable") and hasattr(r, "detail")


def test_unknown_connector_is_handled():
    _fresh()
    from mongoose.connector_probes import probe_one
    r = probe_one("does-not-exist")
    assert r.configured is False
    assert "unknown" in r.detail.lower()


def test_connectors_status_endpoint():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        r = c.get("/connectors/status")
        assert r.status_code == 200
        assert "summary" in r.json()
        r2 = c.get("/connectors/probe/ollama")
        assert r2.status_code == 200
        assert r2.json()["name"] == "ollama"


def test_home_assistant_probe_makes_live_call_when_configured(monkeypatch):
    """When HA is configured, the probe must actually hit /api/ , configured alone isn't usable."""
    _fresh(HA_URL="http://ha.local:8123", HA_TOKEN="tok")
    import httpx
    from mongoose import connector_probes as P

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None): return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    r = P.probe_home_assistant()
    assert r.configured is True
    assert r.usable is True
    assert "/api/" in r.evidence


# --- PWA mobile surface ---

def test_pwa_manifest_served():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        r = c.get("/manifest.webmanifest")
        assert r.status_code == 200
        m = r.json()
        assert m["name"] == "Mongoose"
        assert m["display"] == "standalone"
        assert m["start_url"] == "/ui"
        assert "share_target" in m


def test_service_worker_served():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        r = c.get("/sw.js")
        assert r.status_code == 200
        assert "serviceWorker" not in r.text  # it IS the worker, doesn't register itself
        assert "caches" in r.text


def test_share_target_captures_content():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        r = c.get("/share?title=Hello&text=world&url=http://x.com")
        assert r.status_code == 200
        assert "Mongoose" in r.text


def test_cockpit_references_manifest_and_sw():
    _fresh()
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "/manifest.webmanifest" in html
    assert "/sw.js" in html


def test_pwa_icons_exist():
    """Manifest references 192 + 512 icons, which must actually exist (no broken install)."""
    import mongoose
    base = os.path.dirname(mongoose.__file__)
    assert os.path.exists(os.path.join(base, "static", "img", "mongoose-192.png"))
    assert os.path.exists(os.path.join(base, "static", "img", "mongoose-512.png"))


# --- single-brain reachability: connector status via chat tool + system_status + routing ---

def test_connector_status_tool_registered_and_readonly():
    _fresh()
    from mongoose.tools import TOOLS, READ_TOOLS
    assert "connector_status" in TOOLS
    assert "connector_status" in READ_TOOLS


def test_connector_status_tool_all_and_single():
    _fresh()
    import asyncio
    from mongoose.tools import TOOLS
    allr = asyncio.run(TOOLS["connector_status"]["executor"]({}))
    assert allr["ok"] is True
    assert "summary" in allr and "broken" in allr
    # alias resolution
    one = asyncio.run(TOOLS["connector_status"]["executor"]({"name": "calendar"}))
    assert one["ok"] is True
    assert one["connector"]["name"] == "google_calendar"
    # unknown handled
    bad = asyncio.run(TOOLS["connector_status"]["executor"]({"name": "banana"}))
    assert bad["ok"] is False and "known" in bad


def test_system_status_connectors_area():
    _fresh()
    import asyncio
    from mongoose.services.status_tools import tool_system_status
    r = asyncio.run(tool_system_status({"area": "connectors"}))
    assert r["area"] == "connectors"
    assert "connectors" in r


def test_all_sweep_excludes_live_connectors_probe():
    """The 'all' status sweep must NOT run live connector probes (network) on every call."""
    _fresh()
    import asyncio
    from mongoose.services.status_tools import tool_system_status
    r = asyncio.run(tool_system_status({"area": "all"}))
    assert "connectors" not in r.get("subsystems", {})


def test_connector_questions_route_to_tool():
    """Single-brain: connector-health questions must route to the tool planner, not chat (which would
    guess). Covers voice + text since both go through classify_role."""
    _fresh()
    from mongoose.role_router import classify_role
    for q in ["is Gmail working?", "are my connectors ok?", "check home assistant connection",
              "is my email connected", "how are my connectors", "test ollama", "connector status",
              "check gmail", "why won't my calendar sync"]:
        assert classify_role(q) == "tool", f"{q!r} did not route to tool"


def test_connector_routing_no_overcapture():
    """Normal chat must NOT be captured by the connector patterns."""
    _fresh()
    from mongoose.role_router import classify_role
    for q in ["how was your day", "is it going to rain", "test my patience",
              "drive to the shops", "check this out"]:
        assert classify_role(q) != "tool", f"{q!r} was wrongly routed to tool"


def test_connector_status_in_planner_and_public_catalogue():
    _fresh()
    from mongoose.tools import _tool_catalogue, tool_catalogue
    assert "connector_status" in _tool_catalogue()
    assert "connector_status" in str(tool_catalogue())
