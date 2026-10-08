"""Second feature-pass regressions: gaps found after the first auditor build."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _fresh(monkeypatch=None, tmp_path=None):
    d = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    if monkeypatch is not None:
        monkeypatch.setenv("MONGOOSE_DATA_DIR", d)
        monkeypatch.setenv("MONGOOSE_DB_PATH", os.path.join(d, "m.sqlite3"))
        monkeypatch.setenv("MONGOOSE_INPROCESS_SCHEDULER", "false")
        monkeypatch.setenv("MONGOOSE_RUNTIME_WATCHDOG", "false")
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    return d


def _nonce():
    import mongoose.app as A
    return A._COCKPIT_NONCE


# --- sensitive boundaries overlooked in first pass ---

def test_wp_plugin_action_is_sensitive_and_still_acting():
    from mongoose.tools import ACTING_TOOLS, READ_TOOLS, SENSITIVE_TOOLS
    assert "wp_plugin_action" in ACTING_TOOLS
    assert "wp_plugin_action" in SENSITIVE_TOOLS
    assert "wp_plugin_action" not in READ_TOOLS


def test_wp_plugin_action_confirmation_dispatches_once(monkeypatch):
    _fresh(monkeypatch)
    from mongoose.services import sensitive_tools as S
    calls = []
    monkeypatch.setattr(S, "_execute_wp_plugin_action", lambda args: calls.append(args) or {"ok": True, "built": True})
    p = S.propose_sensitive_action("wp_plugin_action", {"action": "build", "version_suffix": "beta"})
    assert p["needs_confirmation"] is True
    assert "plugin" in p["summary"].lower()
    r = S.confirm_sensitive_action(p["confirm_token"])
    assert r == {"ok": True, "built": True}
    assert calls == [{"action": "build", "version_suffix": "beta"}]
    assert S.confirm_sensitive_action(p["confirm_token"])["ok"] is False


def test_whatsapp_draft_is_acting_not_read_tool():
    from mongoose.tools import ACTING_TOOLS, READ_TOOLS
    assert "whatsapp_draft" in ACTING_TOOLS
    assert "whatsapp_draft" not in READ_TOOLS




def test_acting_after_read_guard_understands_non_home_actions():
    from mongoose.tools import _user_explicitly_requested_action
    assert _user_explicitly_requested_action("search email then draft a WhatsApp message to Chris", "whatsapp_draft")
    assert _user_explicitly_requested_action("find the RTM notes then build the WordPress plugin", "wp_plugin_action")
    assert _user_explicitly_requested_action("read my tasks and remind me tomorrow", "set_reminder")
    assert not _user_explicitly_requested_action("search my email for Chris", "whatsapp_draft")
    assert not _user_explicitly_requested_action("read this plugin status", "wp_plugin_action")


# --- v3 readiness must be callable through the single brain, not only cockpit ---

def test_v3_coordination_handshake_mode(monkeypatch):
    _fresh(monkeypatch)
    import mongoose.services.protocol_v3 as P
    from mongoose.tools import TOOLS
    monkeypatch.setattr(P, "handshake_test", lambda: {"ok": True, "ready": True, "steps": ["mock"]})
    r = asyncio.run(TOOLS["v3_coordination"]["executor"]({"mode": "handshake"}))
    assert r["ok"] is True and r["ready"] is True


def test_v3_and_rtm_source_questions_route_to_tools():
    from mongoose.role_router import classify_role
    for q in [
        "test v3 readiness",
        "check protocol v3 handshake",
        "where is the RTM source of truth kept?",
        "what is canonical for the Example Court build spec?",
        "show the connector lifecycle",
    ]:
        assert classify_role(q) == "tool", q


# --- connector lifecycle/journey view, not just green/red probes ---

def test_connector_journey_maps_setup_broken_working(monkeypatch):
    import mongoose.connector_probes as P
    monkeypatch.setattr(P, "probe_all", lambda only=None: {
        "checked_at": "now",
        "summary": {"total": 3, "configured": 2, "usable": 1, "broken": 1, "unproven": 0},
        "connectors": [
            {"name": "gmail", "configured": False, "usable": False, "detail": "not signed in", "evidence": ""},
            {"name": "imap", "configured": True, "usable": False, "detail": "login failed", "evidence": "NOOP=BAD"},
            {"name": "google_calendar", "configured": True, "usable": True, "detail": "ok", "evidence": "listed 1"},
        ],
    })
    monkeypatch.setattr(P, "_sync_registry", lambda: {"imap": lambda: {"ok": True}, "google_calendar": lambda: {"ok": True}})
    r = P.connector_journey()
    states = {j["name"]: j["state"] for j in r["journeys"]}
    assert states == {"gmail": "not_set_up", "imap": "configured_but_broken", "google_calendar": "working"}
    assert any(j["syncable"] for j in r["journeys"] if j["name"] == "imap")
    assert "read-only" in r["guardrail"]


def test_connector_status_tool_journey_action(monkeypatch):
    _fresh(monkeypatch)
    import mongoose.connector_probes as P
    from mongoose.tools import TOOLS
    monkeypatch.setattr(P, "connector_journey", lambda only=None: {"ok": True, "only": only, "journeys": []})
    r = asyncio.run(TOOLS["connector_status"]["executor"]({"action": "journey", "name": "calendar"}))
    assert r == {"ok": True, "only": ["google_calendar"], "journeys": []}


def test_connectors_journey_endpoint(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.connector_probes as P
    import mongoose.app as A
    monkeypatch.setattr(P, "connector_journey", lambda only=None: {"ok": True, "only": only, "journeys": []})
    c = TestClient(A.app)
    r = c.get("/connectors/journey?only=imap")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "only": ["imap"], "journeys": []}


# --- RTM build source-of-truth runtime surface ---

def test_rtm_build_source_tool_registered_and_readonly():
    from mongoose.tools import READ_TOOLS, TOOLS
    assert "rtm_build_source" in TOOLS
    assert "rtm_build_source" in READ_TOOLS
    assert TOOLS["rtm_build_source"]["description"].startswith("Read-only")


def test_rtm_build_source_status_names_canonical_doc(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.services.rtm_build_source import source_of_truth_status
    r = source_of_truth_status()
    assert r["ok"] is True
    assert r["canonical"]["main_doc"] == "docs/RTM_BUILD_SOURCE_OF_TRUTH.md"
    paths = {d["path"] for d in r["docs"]}
    assert "docs/RTM_BUILD_SOURCE_OF_TRUTH.md" in paths
    assert "ui" in r["entry_points"] and "chat" in r["entry_points"]




def test_rtm_build_source_does_not_create_plugin_build_dir(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.services.rtm_build_source import source_of_truth_status
    build_dir = Path(tmp_path) / "builds" / "plugin"
    assert not build_dir.exists()
    r = source_of_truth_status()
    assert r["ok"] is True
    assert not build_dir.exists(), "read-only provenance status must not create build folders"


def test_rtm_build_source_endpoint_is_gated(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    assert c.get("/rtm/build/source-of-truth").status_code == 403
    r = c.get("/rtm/build/source-of-truth", headers={"X-Cockpit-Nonce": _nonce()})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_setup_ui_surfaces_connector_journey_copy():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "/connectors/journey" in html
    assert "next real-life step" in html


# --- per-session connectivity diagnosis: loopback/LAN/DDNS/proxy confusion ---

def test_connectivity_context_classifies_loopback_and_lan():
    from mongoose.services.connectivity_context import classify_client_host
    assert classify_client_host("127.0.0.1")["scope"] == "loopback"
    assert classify_client_host("localhost")["scope"] == "loopback"
    assert classify_client_host("192.168.1.22")["scope"] == "private_lan_or_vpn"
    assert classify_client_host("example.duckdns.org")["scope"] == "public_or_ddns"


def test_connectivity_context_endpoint_is_gated_and_warns(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    assert c.get("/connectivity/context").status_code == 403
    r = c.get("/connectivity/context", headers={
        "X-Cockpit-Nonce": _nonce(),
        "Host": "localhost:8765",
        "X-Forwarded-For": "192.168.1.50",
    })
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["client"]["scope"] == "private_lan_or_vpn"
    assert any("loopback" in w.lower() for w in j["warnings"])
