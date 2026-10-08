"""Third-pass underserved features: small surfaces that make the system feel coherent in real use."""
from __future__ import annotations

import json
import subprocess

from fastapi.testclient import TestClient


def _nonce(client: TestClient) -> str:
    return client.get("/cockpit/nonce").json()["cockpit_nonce"]


def test_scheduler_status_has_ui_aliases_and_control_endpoint_is_nonce_gated():
    import mongoose.app as A
    from mongoose.services import scheduler as S

    rows = S.job_status()
    assert rows, "scheduler registry should be visible in status"
    first = rows[0]
    assert first["name"] == first["job"]
    assert first["id"] == first["job"]
    assert "last" in first

    c = TestClient(A.app, raise_server_exceptions=False)
    assert c.post("/cockpit/scheduler/control", json={"action": "disable", "jobs": "benchmark"}).status_code == 403
    n = _nonce(c)
    r = c.post(
        "/cockpit/scheduler/control",
        headers={"X-Cockpit-Nonce": n},
        json={"action": "disable", "jobs": "benchmark", "reason": "ui pause test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "benchmark" in body["changed"]
    jobs = {j["job"]: j for j in c.get("/cockpit/scheduler").json()["jobs"]}
    assert jobs["benchmark"]["enabled"] is False
    assert "ui pause test" in jobs["benchmark"]["disabled_reason"]

    r2 = c.post(
        "/cockpit/scheduler/control",
        headers={"X-Cockpit-Nonce": n},
        json={"action": "enable", "jobs": "benchmark"},
    )
    assert r2.status_code == 200 and r2.json()["ok"] is True


def test_watchdog_spawn_detached_throttles_duplicate_spawn_attempts(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    import mongoose.settings as Settings

    if hasattr(Settings, "_settings"):
        Settings._settings = None
    if hasattr(Settings.get_settings, "cache_clear"):
        Settings.get_settings.cache_clear()

    from mongoose.services import server_watchdog as W

    class _FakeProc:
        pid = 4242

    calls = []

    def _fake_popen(argv, **kw):
        calls.append((argv, kw))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    first = W.spawn_detached("first click")
    second = W.spawn_detached("second click")
    assert first.get("spawned_pid") == 4242
    assert second.get("spawn_pending") is True
    assert len(calls) == 1, "repeated UI/startup checks must not spawn a supervisor storm"


def test_guest_privacy_report_redacts_guest_content_and_is_cockpit_gated():
    from mongoose.devices import claim_pairing_code, create_pairing_code
    from mongoose.services.guest_privacy import guest_privacy_report
    from mongoose.services.guest_tasks import add_item
    from mongoose.services.memory import add_message, ensure_conversation, remember_fact
    import mongoose.app as A

    code = create_pairing_code("trusted_guest")["code"]
    claim = claim_pairing_code(code, "Jordan's phone", "phone")
    assert claim["ok"] is True
    scope = f"guest:{claim['device_id']}"
    add_item(scope, kind="note", text="secret guest note about Tuesday")
    remember_fact("guest secret favourite tea", scope=scope)
    ensure_conversation(scope)
    add_message(scope, "user", "guest chat secret text")

    report = guest_privacy_report()
    assert report["ok"] is True
    assert report["guest_count"] == 1
    assert report["guests"][0]["guest_item_counts"]["note"] == 1
    assert report["guests"][0]["memory_fact_count"] == 1
    assert report["guests"][0]["conversation"]["count"] == 1

    dumped = json.dumps(report).lower()
    assert "secret guest note" not in dumped
    assert "guest secret favourite" not in dumped
    assert "guest chat secret" not in dumped
    assert "token_hash" not in dumped
    assert "device_token" not in dumped

    c = TestClient(A.app, raise_server_exceptions=False)
    assert c.get("/cockpit/guests/privacy").status_code == 403
    r = c.get("/cockpit/guests/privacy", headers={"X-Cockpit-Nonce": _nonce(c)})
    assert r.status_code == 200 and r.json()["guest_count"] == 1


def test_conversation_continuity_report_is_metadata_only_and_gated():
    from mongoose.services.conversation_continuity import continuity_report
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID, add_message, ensure_conversation
    import mongoose.app as A

    ensure_conversation(DEFAULT_CONVERSATION_ID)
    add_message(DEFAULT_CONVERSATION_ID, "user", "owner load bearing secret context")
    ensure_conversation("guest:device-a")
    add_message("guest:device-a", "user", "guest private context")

    report = continuity_report()
    assert report["ok"] is True
    assert report["main"]["conversation_id"] == DEFAULT_CONVERSATION_ID
    assert report["main"]["message_count"] >= 1
    assert report["guest_threads"] and report["guest_threads"][0]["conversation_id"].startswith("guest:")
    dumped = json.dumps(report).lower()
    assert "owner load bearing secret" not in dumped
    assert "guest private context" not in dumped
    assert "content_returned" in dumped

    c = TestClient(A.app, raise_server_exceptions=False)
    assert c.get("/conversation/continuity").status_code == 403
    r = c.get("/conversation/continuity", headers={"X-Cockpit-Nonce": _nonce(c)})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_privacy_and_continuity_are_read_only_chat_tools_and_route_to_tools():
    import mongoose.tools as T
    from mongoose.role_router import classify_role

    for name in ("guest_privacy", "conversation_continuity"):
        assert name in T.TOOLS
        assert name in T.READ_TOOLS
        assert name not in T.ACTING_TOOLS
        assert name not in T.SENSITIVE_TOOLS

    assert classify_role("can Alex read guest state or are guest boundaries isolated?") == "tool"
    assert classify_role("check chat context continuity after a restart") == "tool"


def test_chat_tool_loop_uses_local_status_plans_for_privacy_and_context(monkeypatch):
    import mongoose.tools as T

    async def fail_planner(*args, **kwargs):  # should not be needed for these narrow status reports
        raise AssertionError("local status plan should avoid the model planner")

    async def phrase(prompt: str, role: str = "chat"):
        return "phrased"

    monkeypatch.setattr(T, "ask_ollama", fail_planner)
    monkeypatch.setattr(T, "ask_persona", phrase)

    import asyncio
    a = asyncio.run(T.run_tool_turn("can Alex read guest state? audit the guest boundary"))
    assert a["executed"] is True
    assert a["steps"][0]["tool"] == "guest_privacy"
    assert a["result"]["ok"] is True

    b = asyncio.run(T.run_tool_turn("check chat context continuity after restart"))
    assert b["executed"] is True
    assert b["steps"][0]["tool"] == "conversation_continuity"
    assert b["result"]["ok"] is True
