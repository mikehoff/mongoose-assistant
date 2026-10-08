from __future__ import annotations

import asyncio


def test_resource_guard_snapshot_is_small_and_local():
    from mongoose.services.resource_guard import assess_runtime_pressure
    snap = assess_runtime_pressure(include_process_table=False)
    assert snap["level"] in {"green", "amber", "red"}
    assert "current_process" in snap
    assert snap["current_process"].get("pid")
    assert "thread_count" in snap["current_process"]
    assert "system_memory" in snap


def test_scheduler_times_out_one_job_and_runs_later_jobs(monkeypatch):
    from mongoose.services import scheduler as S

    monkeypatch.setattr(S, "_job_timeout_seconds", lambda name: 0.03)
    calls = {"slow": 0, "safe": 0}

    async def slow():
        calls["slow"] += 1
        await asyncio.sleep(0.08)
        return {"ok": True}

    async def safe():
        calls["safe"] += 1
        return {"ok": True}

    monkeypatch.setattr(S, "_build_registry", lambda: {
        "slow": (("interval", 1), slow),
        "safe": (("interval", 1), safe),
    })
    ran = asyncio.run(S.run_due_jobs())
    assert ran["slow"]["status"] == "timeout"
    assert "timed out" in ran["slow"]["result"]["error"]
    assert ran["safe"]["status"] == "ok"
    assert calls["safe"] == 1


def test_scheduler_skips_deferrable_jobs_under_memory_pressure(monkeypatch):
    from mongoose.services import scheduler as S

    calls = {"benchmark": 0, "fire_alerts": 0}

    async def benchmark():
        calls["benchmark"] += 1
        return {"ok": True}

    async def fire_alerts():
        calls["fire_alerts"] += 1
        return {"ok": True}

    monkeypatch.setattr(S, "_resource_pressure_snapshot", lambda: {"level": "red", "warnings": ["low memory"]})
    monkeypatch.setattr(S, "_build_registry", lambda: {
        "benchmark": (("interval", 1), benchmark),       # deferrable/model-heavy
        "fire_alerts": (("interval", 1), fire_alerts),   # safety/continuity lane
    })
    ran = asyncio.run(S.run_due_jobs())
    assert ran["benchmark"]["status"] == "skipped"
    assert ran["benchmark"]["result"]["skipped"] == "resource_pressure"
    assert ran["fire_alerts"]["status"] == "ok"
    assert calls == {"benchmark": 0, "fire_alerts": 1}


def test_llm_payload_caps_output_tokens(monkeypatch):
    import mongoose.llm as L
    import mongoose.settings as settings

    monkeypatch.setenv("MONGOOSE_OLLAMA_NUM_PREDICT", "123")
    settings._SETTINGS = None
    captured = {}

    class Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"message": {"content": "ok"}}

    class Client:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            captured["payload"] = json
            return Resp()

    monkeypatch.setattr(L.httpx, "AsyncClient", Client)
    assert asyncio.run(L.ask_ollama("hello")) == "ok"
    assert captured["payload"]["options"]["num_predict"] == 123
    assert captured["payload"]["options"]["num_ctx"] >= 1


def test_system_status_resources_area():
    from mongoose.services.status_tools import tool_system_status
    r = asyncio.run(tool_system_status({"area": "resources"}))
    assert r["area"] == "resources"
    assert r["ok"] is True
    assert "resources" in r
    assert r["resources"]["level"] in {"green", "amber", "red"}


def test_external_watchdog_can_restart_on_python_rss_pressure(monkeypatch):
    import mongoose.services.resource_guard as RG
    import mongoose.services.server_watchdog as W

    monkeypatch.setattr(W, "read_pidfile", lambda: (4321, 8765))
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=5.0: True)
    monkeypatch.setattr(RG, "server_rss_exceeds_restart_threshold", lambda pid: {
        "pid": pid, "rss_mb": 9999, "threshold_mb": 1, "exceeded": True,
    })
    calls = {"n": 0}
    monkeypatch.setattr(W, "restart_server", lambda port, repo_root=None: (calls.__setitem__("n", calls["n"] + 1) or {"ok": True}))
    state = {"consecutive_failures": 0, "last_restart": 0.0}
    first = W.watch_once(state, fail_threshold=2, cooldown_seconds=0)
    second = W.watch_once(state, fail_threshold=2, cooldown_seconds=0)
    assert first["event"] == "memory_pressure"
    assert second["event"] == "memory_restarted"
    assert calls["n"] == 1


def test_local_model_calls_serialise_across_event_loops(monkeypatch):
    """Scheduler worker loops and chat loops must not hit Ollama concurrently."""
    import threading
    import mongoose.llm as L
    import mongoose.settings as settings

    settings._SETTINGS = None
    in_flight = 0
    max_seen = 0
    gate = threading.Lock()
    results: list[str] = []
    errors: list[BaseException] = []

    class Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"message": {"content": "ok"}}

    class Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            nonlocal in_flight, max_seen
            with gate:
                in_flight += 1
                max_seen = max(max_seen, in_flight)
            await asyncio.sleep(0.05)
            with gate:
                in_flight -= 1
            return Resp()

    monkeypatch.setattr(L.httpx, "AsyncClient", Client)

    def worker():
        try:
            results.append(asyncio.run(L.ask_ollama("hello")))
        except BaseException as exc:  # pragma: no cover - test diagnostics
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    assert sorted(results) == ["ok", "ok"]
    assert max_seen == 1
