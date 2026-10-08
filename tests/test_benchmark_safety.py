"""Tests for the benchmark runner safety budget: a slow/looping model must not block
the whole benchmark. Every call is bounded by a timeout and an output token cap, and
a timed-out task is marked failed so the run continues."""
import asyncio

import mongoose.benchmark as B


def test_timed_chat_sends_token_cap(monkeypatch):
    """The model call must cap output tokens (num_predict): the primary loop guard."""
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "ok"}}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            captured["payload"] = json
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    dt, out = asyncio.run(B._timed_chat("m", "hi"))
    assert out == "ok"
    assert captured["payload"]["options"]["num_predict"] == B._BENCH_MAX_OUTPUT_TOKENS


def test_timed_chat_raises_benchtimeout_on_timeout(monkeypatch):
    """A call that exceeds the per-call budget raises BenchTimeout (not hang)."""
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            raise asyncio.TimeoutError()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    try:
        asyncio.run(B._timed_chat("slow-model", "hi"))
        assert False, "should have raised BenchTimeout"
    except B.BenchTimeout as exc:
        assert "slow-model" in str(exc)


def test_all_calls_timeout_exits_gracefully_not_hang(monkeypatch):
    """If EVERY call times out, the benchmark must still return promptly with a
    timeout recorded, rather than hanging. (A model that can't even complete a warm
    chat call has nothing to score, so an early exit is correct, the point is it
    does not block.)"""
    async def always_timeout(model, prompt, *, system=None):
        raise B.BenchTimeout("boom")
    monkeypatch.setattr(B, "_timed_chat", always_timeout)

    r = asyncio.run(B.benchmark_model("m", measure_cold=False, repeats=1, warmup_rounds=0))
    assert r.get("timeouts", 0) >= 1
    assert "error" in r  # reported a failure rather than hanging


def test_timed_out_task_marked_failed_run_continues(monkeypatch):
    """If a TASK call times out but the model is otherwise responsive, that task is
    marked failed (timeout=True) and the run proceeds through the rest."""
    async def selective(model, prompt, *, system=None):
        # time out only the correspondence-drafting task; everything else is fine
        if "draft" in prompt.lower() or "correspondence" in prompt.lower() or "email" in prompt.lower():
            raise B.BenchTimeout("slow task")
        return 0.01, "ok response"

    monkeypatch.setattr(B, "_timed_chat", selective)
    r = asyncio.run(B.benchmark_model("m", measure_cold=False, repeats=1, warmup_rounds=0))
    assert "tasks" in r
    # the run completed all task entries (none blocked the rest)
    assert len(r["tasks"]) >= 5
    assert r["timeouts"] >= 1


def test_timeout_count_in_row():
    """The final table row includes a timeout count so the user sees it."""
    row = B._row({"model": "m", "tasks_passed": "5/6", "memory_extract_ok": True, "timeouts": 2})
    assert "timeouts=2" in row


def test_results_streamed_and_renderable(monkeypatch, tmp_path, capsys):
    """Each model's result is written to the results file as it finishes, and
    render_results_file() rebuilds the table from it, so a partial/interrupted run is
    inspectable."""
    # point the results file at a temp data dir
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))

    async def fake_timed(model, prompt, *, system=None):
        return 0.01, ('["a fact"]' if (system and "extract" in system.lower()) else "ok")
    monkeypatch.setattr(B, "_timed_chat", fake_timed)

    async def fake_installed():
        return ["modelA", "modelB"]
    monkeypatch.setattr(B, "installed_models", fake_installed)

    asyncio.run(B.run_benchmark(["modelA", "modelB"], measure_cold=False,
                                auto_pull=False, rounds=1, repeats=1, cooldown_s=0))
    # the streamed file exists with both models
    path = B._results_path()
    assert path.exists()
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2

    # render from file reproduces a table mentioning both models
    capsys.readouterr()
    n = B.render_results_file()
    out = capsys.readouterr().out
    assert n == 2 and "modelA" in out and "modelB" in out


def test_render_results_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    n = B.render_results_file()
    out = capsys.readouterr().out
    assert n == 0 and "No benchmark results" in out


def test_ha_task_skipped_when_unconfigured(monkeypatch, tmp_path):
    """A task with an unmet prereq (HA not configured) is SKIPPED, not failed, and
    excluded from the tasks_passed denominator."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)

    async def ft(model, prompt, *, system=None):
        return 0.1, '{"tool":"control_home","args":{"entity_id":"light.x"}}'
    monkeypatch.setattr(B, "_timed_chat", ft)
    r = asyncio.run(B.benchmark_model("m", measure_cold=False, repeats=1, warmup_rounds=0))
    live = next(t for t in r["tasks"] if t["task"] == "home_execute_live")
    assert live.get("skipped") is True
    assert r["tasks_skipped"] >= 1
    # denominator excludes the skip (6 scored, not 7)
    assert r["tasks_passed"].endswith("/6")


def test_ha_task_scored_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    monkeypatch.setenv("HA_URL", "http://ha.local:8123")
    monkeypatch.setenv("HA_TOKEN", "tok")

    async def ft(model, prompt, *, system=None):
        return 0.1, '{"tool":"control_home","args":{"entity_id":"light.x"}}'
    monkeypatch.setattr(B, "_timed_chat", ft)
    r = asyncio.run(B.benchmark_model("m", measure_cold=False, repeats=1, warmup_rounds=0))
    live = next(t for t in r["tasks"] if t["task"] == "home_execute_live")
    assert not live.get("skipped")
    assert r["tasks_passed"].endswith("/7")   # now scored


def test_failed_task_writes_diagnosis_log(monkeypatch, tmp_path):
    """A failing task writes a diagnosis file (raw output, parsed plan, reason), so
    an N isn't a mystery."""
    import glob
    import json as _json
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    monkeypatch.delenv("HA_URL", raising=False)

    async def ft(model, prompt, *, system=None):
        return 0.1, "just prose, no valid plan"   # fails the tool tasks
    monkeypatch.setattr(B, "_timed_chat", ft)
    asyncio.run(B.benchmark_model("diagmodel", measure_cold=False, repeats=1, warmup_rounds=0))
    logs = glob.glob(str(tmp_path / "data" / "benchmarks" / "*" / "diagmodel" / "*.json"))
    assert logs, "expected at least one failure diagnosis file"
    rec = _json.load(open(logs[0]))
    assert "raw_output" in rec and "parsed_plan" in rec and "score_reason" in rec
