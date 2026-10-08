"""Fully-mocked tests for the benchmark: realistic use-case tasks and the
auto-pull of missing models. No live model and no real downloads, every model
call and Ollama HTTP call is mocked, so this runs in CI and offline."""
import json


import mongoose.benchmark as B


# --- task scorers judge real outputs correctly -------------------------------
def test_home_control_scorer():
    good = json.dumps({"tool": "control_home", "args": {"device_id": "light.lounge", "action": "turn_off"}})
    bad = json.dumps({"tool": "get_weather", "args": {}})
    assert B._score_home_control(good) is True
    assert B._score_home_control(bad) is False
    assert B._score_home_control("not json at all") is False


def test_find_intent_scorer_routes_to_search():
    for tool in ("find", "search_files", "search_attachments", "search_email"):
        plan = json.dumps({"tool": tool, "args": {"query": "service charge"}})
        assert B._is_search_plan(plan) is True
    assert B._is_search_plan(json.dumps({"tool": "control_home", "args": {}})) is False


def test_memory_json_scorer():
    assert B._score_memory_json('["drives a Honda", "dog named Toby"]') is True
    assert B._score_memory_json("```[\"x\"]```") is True   # fenced
    assert B._score_memory_json("not json") is False
    assert B._score_memory_json("[]") is False             # empty = no facts


def test_mentions_scorers():
    s = B._score_mentions("16 july", "1 july")
    assert s("Management transfers on 16 July 2026; no demands from 1 July.") is True
    assert s("Management transfers on 16 July.") is False  # missing 1 july
    a = B._score_any("lawfirm", "dentist")
    assert a("the Lawfirm call clashes") is True
    assert a("nothing relevant") is False


# --- benchmark_model with a fully mocked model -------------------------------
def _mock_model(monkeypatch, responder):
    """Patch the benchmark's timed model call so every call returns
    responder(prompt, system). _timed_chat is the real seam now (it no longer routes
    through ask_ollama, so it can enforce the per-call timeout and token cap)."""
    import mongoose.benchmark as B

    async def fake_timed_chat(model, prompt, *, system=None):
        return 0.01, responder(prompt, system)
    monkeypatch.setattr(B, "_timed_chat", fake_timed_chat)


def test_benchmark_model_all_tasks_pass_with_good_model(monkeypatch):
    # a "perfect" model: returns the right shape for each task
    def responder(prompt, system):
        p = prompt.lower()
        if "turn off" in p and "lounge" in p:
            return json.dumps({"tool": "control_home", "args": {"device_id": "light.lounge", "action": "off"}})
        if "find the service charge" in p:
            return json.dumps({"tool": "find", "args": {"query": "service charge certificate"}})
        if "automations both turn" in p:
            return "Both automations have a conflicting trigger at sunset; add a condition to fix it."
        if "16 july" in p:
            return "Management transfers to the RTM company on 16 July 2026. No service charge demands should be issued from 1 July."
        if system and "extract" in system.lower():
            return '["drives a Honda", "dog named Toby", "cycles on Saturdays"]'
        if "confirmed signals" in p:
            return "Heads up: your Lawfirm call clashes with the dentist, and rain is due 3-5pm."
        return "ok"
    _mock_model(monkeypatch, responder)
    import asyncio
    r = asyncio.run(B.benchmark_model("mock-good", measure_cold=False))
    assert r["tasks_passed"] == "6/6"   # 6 scored tasks all pass
    # every SCORED task passed; the HA-gated live task is skipped (no HA in tests)
    assert all(t["ok"] for t in r["tasks"] if not t.get("skipped"))
    assert any(t.get("skipped") for t in r["tasks"])  # home_execute_live skipped


def test_benchmark_model_flags_failures_with_bad_model(monkeypatch):
    # a "useless" model: returns prose where structure is needed, omits required terms
    def responder(prompt, system):
        if system and "extract" in system.lower():
            return "I think you mentioned a car."   # not JSON
        return "Sure, I can help with that!"          # never a valid plan or required terms
    _mock_model(monkeypatch, responder)
    import asyncio
    r = asyncio.run(B.benchmark_model("mock-bad", measure_cold=False))
    # home_control, find_intent, draft_correspondence, memory_extract should all fail
    by = {t["task"]: t["ok"] for t in r["tasks"] if not t.get("skipped")}
    assert by["home_control"] is False
    assert by["find_intent"] is False
    assert by["draft_correspondence"] is False
    assert by["memory_extract"] is False
    assert r["tasks_passed"] != "6/6"


# --- auto-pull of missing models (mocked HTTP, no real download) -------------
def test_run_benchmark_auto_pulls_missing(monkeypatch):
    pulled = []

    async def fake_installed():
        return set()  # nothing installed -> everything "missing"

    async def fake_pull(model):
        pulled.append(model)
        return True

    async def fake_bench(model, measure_cold=True, **kwargs):
        return {"model": model, "tasks_passed": "6/6", "tasks": []}

    monkeypatch.setattr(B, "installed_models", fake_installed)
    monkeypatch.setattr(B, "pull_model", fake_pull)
    monkeypatch.setattr(B, "benchmark_model", fake_bench)

    import asyncio
    asyncio.run(B.run_benchmark(["modelA", "modelB"], measure_cold=False, auto_pull=True, cooldown_s=0))
    assert pulled == ["modelA", "modelB"]   # both auto-pulled


def test_run_benchmark_no_pull_skips_download(monkeypatch):
    pulled = []

    async def fake_installed():
        return set()

    async def fake_pull(model):
        pulled.append(model)
        return True

    async def fake_bench(model, measure_cold=True, **kwargs):
        return {"model": model, "tasks_passed": "0/6", "tasks": []}

    monkeypatch.setattr(B, "installed_models", fake_installed)
    monkeypatch.setattr(B, "pull_model", fake_pull)
    monkeypatch.setattr(B, "benchmark_model", fake_bench)

    import asyncio
    asyncio.run(B.run_benchmark(["modelA"], measure_cold=False, auto_pull=False, cooldown_s=0))
    assert pulled == []   # --no-pull: nothing downloaded


def test_pull_model_streams_success(monkeypatch):
    # mock the Ollama /api/pull streaming response
    class FakeStream:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def aiter_lines(self):
            for line in [json.dumps({"status": "pulling manifest"}),
                         json.dumps({"status": "downloading"}),
                         json.dumps({"status": "success"})]:
                yield line

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, json=None):
            return FakeStream()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    import asyncio
    assert asyncio.run(B.pull_model("some-model")) is True


def test_pull_model_handles_error(monkeypatch):
    class FakeStream:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def aiter_lines(self):
            yield json.dumps({"error": "model not found"})

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, json=None):
            return FakeStream()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    import asyncio
    assert asyncio.run(B.pull_model("missing-model")) is False


# --- fairness / like-for-like guards -----------------------------------------
def test_warmup_runs_before_timed_calls(monkeypatch):
    """Each phase must issue discarded warm-up calls before timing, so the first
    timed call doesn't pay the cold-start tax."""
    calls = []

    async def fake_timed(model, prompt, *, system=None):
        calls.append(prompt)
        return 0.01, "ok"
    monkeypatch.setattr(B, "_timed_chat", fake_timed)

    import asyncio
    asyncio.run(B._warm_up("m", rounds=2))
    assert calls == [B._WARMUP_PROMPT, B._WARMUP_PROMPT]  # exactly the warm-up calls


def test_repeat_collects_multiple_samples(monkeypatch):
    seen = []

    async def fake_timed(model, prompt, *, system=None):
        seen.append(prompt)
        return 0.01, "ok"
    monkeypatch.setattr(B, "_timed_chat", fake_timed)

    import asyncio
    samples = asyncio.run(B._repeat("m", "do thing", repeats=4))
    assert len(samples) == 4 and all(s >= 0 for s in samples)


def test_spread_and_stats():
    # IQR is zero for identical samples, positive for spread
    assert B._spread([1.0, 1.0, 1.0]) == 0.0
    assert B._spread([1.0, 2.0, 3.0, 4.0]) > 0
    st = B._stats([1.0, 2.0, 3.0])
    assert st["median"] == 2.0 and st["n"] == 3 and "iqr" in st


def test_benchmark_model_reports_variance(monkeypatch):
    async def fake_timed(model, prompt, *, system=None):
        return 0.01, ('["a fact"]' if (system and "extract" in system.lower()) else "ok response")
    monkeypatch.setattr(B, "_timed_chat", fake_timed)

    import asyncio
    r = asyncio.run(B.benchmark_model("m", measure_cold=False, repeats=3, warmup_rounds=1))
    # the stats blocks carry n (samples) and iqr (spread)
    assert r["warm_chat"]["n"] >= 3
    assert "iqr" in r["task_latency"]
    assert r["method"]["repeats"] == 3


def test_cold_load_evicts_each_sample(monkeypatch):
    evictions = []

    async def fake_evict(model):
        evictions.append(model)
    async def fake_timed(model, prompt, *, system=None):
        return 0.01, "ok"
    monkeypatch.setattr(B, "_evict", fake_evict)
    monkeypatch.setattr(B, "_timed_chat", fake_timed)
    # no real sleeping
    import asyncio

    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(B.asyncio, "sleep", no_sleep)

    asyncio.run(B.benchmark_model("m", measure_cold=True, cold_samples=3, repeats=1, warmup_rounds=0))
    assert evictions.count("m") == 3  # re-evicted before each cold sample


def test_rounds_aggregate_majority_pass(monkeypatch):
    # 3 rounds: a task that passes in 2/3 rounds should aggregate to passed
    runs = [
        {"model": "m", "tasks": [{"task": "t1", "ok": True}], "tasks_passed": "1/1",
         "warm_chat": {"median": 1.0, "iqr": 0.1, "n": 3}, "memory_extract_ok": True,
         "tool_calls_valid": "5/5"},
        {"model": "m", "tasks": [{"task": "t1", "ok": False}], "tasks_passed": "0/1",
         "warm_chat": {"median": 1.2, "iqr": 0.2, "n": 3}, "memory_extract_ok": True,
         "tool_calls_valid": "5/5"},
        {"model": "m", "tasks": [{"task": "t1", "ok": True}], "tasks_passed": "1/1",
         "warm_chat": {"median": 1.1, "iqr": 0.1, "n": 3}, "memory_extract_ok": False,
         "tool_calls_valid": "5/5"},
    ]
    agg = B._aggregate_rounds("m", runs)
    assert agg["rounds"] == 3
    assert agg["tasks_passed"] == "1/1"          # passed in 2 of 3 rounds -> pass
    assert agg["warm_chat"]["n"] == 9             # pooled samples across rounds
    assert agg["memory_extract_ok"] is True       # majority (2/3) ok
