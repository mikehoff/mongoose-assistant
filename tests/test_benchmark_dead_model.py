"""A model whose backend has stopped generating must be reported DEAD, not scored as bad.

Real bug, found 2026-10-08: gemma4:26b-a4b scored 0/7 tasks and 0/5 tool calls every night from
18 September to 7 October, twenty consecutive runs. It was not answering badly, it was not answering
at all: raw_output was an empty string on every task and every latency was 0.03 to 0.05 seconds,
which is physically impossible for real inference on a 17 GB model. Ollama's own log carried 9,310
Metal out-of-memory errors (kIOGPUCommandBufferCallbackErrorOutOfMemory) ending in
"llama-server terminated signal: killed": the model loads, the first decode exhausts the GPU, the
backend wedges, and every later call returns empty.

Two separate defects made that invisible:

1. _timed_chat accepted an empty completion as a valid answer, so a wedged backend was scored as a
   model that gets every task wrong. A reader of the scoreboard saw "0/7, bad model" instead of
   "this model never generated a token".
2. run_benchmark drove a model into the OOM in the first place. A 17 GB model cannot be measured on
   a 24 GB box that is also running the server, the tunnel and several editors, so the honest
   outcome is SKIPPED with a reason, not a fabricated zero.

This is the same class of defect as test_benchmark_context_window.py: the harness reporting its own
failure as the models' failure.
"""
import asyncio
import json

import pytest

import mongoose.benchmark as B


# --- a wedged backend is an error, not an answer ------------------------------
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for httpx.AsyncClient: returns one canned /api/chat payload."""

    def __init__(self, payload):
        self._payload = payload

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeResp(self._payload)


def _mock_http(monkeypatch, payload):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient(payload))


def test_timed_chat_rejects_an_empty_completion(monkeypatch):
    """The exact shape Ollama returned for the dead 26b: HTTP 200, content ''."""
    _mock_http(monkeypatch, {"message": {"content": ""}})
    with pytest.raises(B.BenchDead):
        asyncio.run(B._timed_chat("mock-dead", "hello"))


def test_timed_chat_rejects_whitespace_only_completion(monkeypatch):
    _mock_http(monkeypatch, {"message": {"content": "   \n  "}})
    with pytest.raises(B.BenchDead):
        asyncio.run(B._timed_chat("mock-dead", "hello"))


def test_timed_chat_rejects_an_ollama_error_payload(monkeypatch):
    """Ollama reports a wedged backend as {"error": "Compute error."} with HTTP 200."""
    _mock_http(monkeypatch, {"error": "Compute error."})
    with pytest.raises(B.BenchDead) as exc:
        asyncio.run(B._timed_chat("mock-dead", "hello"))
    assert "Compute error" in str(exc.value)


def test_timed_chat_still_accepts_a_real_answer(monkeypatch):
    _mock_http(monkeypatch, {"message": {"content": "ok"}})
    dt, out = asyncio.run(B._timed_chat("mock-live", "hello"))
    assert out == "ok"
    assert dt >= 0


# --- the dead signal must not be swallowed on its way up ----------------------
def test_warm_up_does_not_swallow_a_dead_backend(monkeypatch):
    """_warm_up swallows ordinary errors on purpose, but a dead backend must propagate,
    otherwise the phases downstream measure nothing and report it as bad scores."""
    async def dead(model, prompt, *, system=None):
        raise B.BenchDead("mock-dead returned no output")
    monkeypatch.setattr(B, "_timed_chat", dead)
    with pytest.raises(B.BenchDead):
        asyncio.run(B._warm_up("mock-dead", rounds=1))


def test_repeat_does_not_swallow_a_dead_backend(monkeypatch):
    async def dead(model, prompt, *, system=None):
        raise B.BenchDead("mock-dead returned no output")
    monkeypatch.setattr(B, "_timed_chat", dead)
    with pytest.raises(B.BenchDead):
        asyncio.run(B._repeat("mock-dead", "hello", repeats=2))


def test_repeat_still_tolerates_an_ordinary_timeout(monkeypatch):
    """A single slow call is noise to be absorbed; only a dead backend aborts."""
    async def slow(model, prompt, *, system=None):
        raise B.BenchTimeout("too slow")
    monkeypatch.setattr(B, "_timed_chat", slow)
    assert asyncio.run(B._repeat("mock-slow", "hello", repeats=2)) == []


# --- benchmark_model reports dead once, instead of zero out of seven ----------
def test_benchmark_model_reports_dead_rather_than_scoring_zero(monkeypatch):
    async def dead(model, prompt, *, system=None):
        raise B.BenchDead("mock-dead returned no output")
    monkeypatch.setattr(B, "_timed_chat", dead)
    r = asyncio.run(B.benchmark_model("mock-dead", measure_cold=False))

    assert r.get("dead") is True, "a non-generating model must be flagged dead"
    assert "no output" in str(r.get("error", "")).lower()
    # The headline number must not read as "got every task wrong". Nothing was scored.
    assert r["tasks_passed"] == "0/0", (
        f"dead model reported tasks_passed={r['tasks_passed']!r}; a wedged backend scored "
        "0/7 for twenty nights and read as a bad model")
    assert not [t for t in r.get("tasks", []) if t.get("ok") is False], \
        "a dead model must not leave per-task failures that look like wrong answers"
    # And it must not claim a tool-call score either.
    assert r.get("tool_calls_valid") in (None, "0/0"), \
        f"dead model reported tool_calls_valid={r.get('tool_calls_valid')!r}"


def test_a_live_model_is_not_flagged_dead(monkeypatch):
    """Guard against the fix firing on healthy models."""
    def responder(prompt, system):
        if system and "extract" in system.lower():
            return '["drives a Honda", "dog named Toby"]'
        return json.dumps({"tool": "control_home", "args": {"device_id": "light.lounge",
                                                            "action": "off"}})

    async def fake(model, prompt, *, system=None):
        return 0.01, responder(prompt, system)
    monkeypatch.setattr(B, "_timed_chat", fake)
    r = asyncio.run(B.benchmark_model("mock-live", measure_cold=False))
    assert not r.get("dead")
    assert r["tasks_passed"] != "0/0"


# --- do not drive a model that cannot fit into an OOM -------------------------
def test_fits_available_memory_arithmetic():
    gb = 1024 ** 3
    # 17 GB model, 6 GB of budget: cannot fit, and says so.
    ok, why = B.fits_available_memory(17 * gb, 6 * gb)
    assert ok is False
    assert "17" in why and "6" in why, f"reason should name both sizes, got {why!r}"
    # 7 GB model, 20 GB budget: fits.
    ok, why = B.fits_available_memory(7 * gb, 20 * gb)
    assert ok is True


def test_budget_is_total_minus_reserve_not_instantaneous_free(monkeypatch):
    """The budget must NOT be psutil's `available`.

    First cut of this guard used it and skipped the ENTIRE roster on the real 24 GB Mac mini,
    which reported 6.9 GB available because qwen3:14b was resident and the box was busy. The
    benchmark evicts between models, so a resident model's memory is about to be freed and must
    not count against the next candidate."""
    import psutil
    gb = 1024 ** 3

    class _VM:
        total = 24 * gb
        available = 1 * gb   # deliberately tiny: must be ignored

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    monkeypatch.delenv("MONGOOSE_BENCHMARK_MEMORY_RESERVE_GB", raising=False)
    budget = B._memory_budget_bytes()
    assert budget > 10 * gb, f"budget collapsed to instantaneous free memory ({budget / gb:.1f} GB)"
    assert budget == 16 * gb, "24 GB total minus the 8 GB default reserve"


def test_budget_reserve_is_tunable_and_survives_garbage(monkeypatch):
    import psutil
    gb = 1024 ** 3

    class _VM:
        total = 24 * gb
        available = 1 * gb

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    monkeypatch.setenv("MONGOOSE_BENCHMARK_MEMORY_RESERVE_GB", "4")
    assert B._memory_budget_bytes() == 20 * gb
    # A garbage value must fall back to the default, not disable or zero the guard.
    monkeypatch.setenv("MONGOOSE_BENCHMARK_MEMORY_RESERVE_GB", "banana")
    assert B._memory_budget_bytes() == 16 * gb


def test_real_roster_verdicts_on_a_24gb_box(monkeypatch):
    """The whole point: on this machine the 26b must be skipped and the other four must run.

    Sizes are the real `ollama list` figures. If this test ever skips qwen3:14b, the guard has
    become more harmful than the bug it fixes."""
    import psutil
    gb = 1024 ** 3

    class _VM:
        total = 24 * gb
        available = 6 * gb

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    monkeypatch.delenv("MONGOOSE_BENCHMARK_MEMORY_RESERVE_GB", raising=False)
    budget = B._memory_budget_bytes()
    roster = {"qwen3:14b": 8.6, "gemma4:26b-a4b-it-q4_K_M": 16.8,
              "gemma4:12b-it-q4_K_M": 7.0, "gemma4:e2b": 6.7, "gemma4:e4b": 8.9}
    verdicts = {m: B.fits_available_memory(int(sz * gb), budget)[0] for m, sz in roster.items()}
    assert verdicts["gemma4:26b-a4b-it-q4_K_M"] is False, "the 17 GB model must be skipped"
    for m in ("qwen3:14b", "gemma4:12b-it-q4_K_M", "gemma4:e2b", "gemma4:e4b"):
        assert verdicts[m] is True, f"{m} runs fine on this box and must not be skipped"


def test_fits_available_memory_demands_headroom():
    """A model that exactly equals free memory does not fit: the KV cache and the rest of
    the machine still need room. This is what made the 26b load and then die mid-decode."""
    gb = 1024 ** 3
    ok, _ = B.fits_available_memory(17 * gb, 17 * gb)
    assert ok is False, "a model must not be run with zero headroom"


def test_fits_available_memory_is_permissive_when_size_unknown():
    """An unknown size must not silently skip every model."""
    ok, _ = B.fits_available_memory(None, 4 * 1024 ** 3)
    assert ok is True


def test_run_benchmark_skips_a_model_that_cannot_fit(monkeypatch):
    gb = 1024 ** 3
    benched = []

    async def fake_bench(model, **k):
        benched.append(model)
        return {"model": model, "tasks_passed": "6/6"}

    async def fake_installed():
        return {"small", "huge"}

    monkeypatch.setattr(B, "installed_models", fake_installed)
    monkeypatch.setattr(B, "benchmark_model", fake_bench)
    monkeypatch.setattr(B, "_model_size_bytes", lambda m: 17 * gb if m == "huge" else 5 * gb)
    monkeypatch.setattr(B, "_memory_budget_bytes", lambda: 10 * gb)

    rows = asyncio.run(B.run_benchmark(["small", "huge"], measure_cold=False, cooldown_s=0))

    assert benched == ["small"], "the oversized model should never have been run"
    by = {r.get("model"): r for r in rows}
    assert by["huge"].get("skipped") is True
    assert "memory" in str(by["huge"].get("reason", "")).lower()
    # A skip is not a zero. The scoreboard must not imply it was measured and failed.
    assert by["huge"].get("tasks_passed") in (None, "0/0")
