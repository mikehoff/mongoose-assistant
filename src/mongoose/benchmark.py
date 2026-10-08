from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable

from .settings import get_settings
from .tools import _PLANNER_SYSTEM, _tool_catalogue, _parse_plan
from .services.memory import looks_fact_bearing

# A reproducible, on-machine benchmark for picking the resident model. It does
# NOT decide for you: it runs the same real Mongoose tasks against whatever model
# tag you pass and prints raw numbers. The point is to replace "Reddit says" with
# "my Mac mini says".
#
# The tasks are REAL use cases, not synthetic toys: the kinds of things this user
# actually does (home control, finding a tribunal document, reviewing an HA
# automation, drafting correspondence, pulling durable facts from a self-
# disclosure, and the cross-domain brief). Each task carries a SCORER that judges
# whether the model's output is actually usable for that task, so the benchmark
# measures usefulness on this user's workload, not generic fluency.
#
# Measured per model:
#   - cold load: time for the first call after the model is evicted
#   - per-task latency and a pass/fail from each task's scorer
#   - aggregate: tasks passed, median latency
#
# Latency is the honest physics number; the scorers are the usefulness number.


# --- realistic task definitions ---------------------------------------------
# Each task: name, the system prompt it runs under, the user prompt (a real one),
# and a scorer(output_text) -> bool that decides if the answer is usable.

def _score_home_control(raw: str) -> bool:
    """A usable home-control answer parses into a control_home plan that identifies
    SOMETHING to act on. The tool accepts several identifiers (device_id, entity_id
    like light.lounge, or room), so any of them counts: a model answering with
    entity_id + turn_off is doing the right thing, not failing. Also accept the
    tool_calls envelope some models emit."""
    try:
        plan = _parse_plan(raw)
    except Exception:
        return False
    # Some models emit {"tool_calls":[{"name":...,"arguments":{...}}]}; normalise.
    if plan.get("tool") is None and isinstance(plan.get("tool_calls"), list) and plan["tool_calls"]:
        call = plan["tool_calls"][0] or {}
        plan = {"tool": call.get("name") or (call.get("function") or {}).get("name"),
                "args": call.get("arguments") or (call.get("function") or {}).get("arguments") or {}}
    if plan.get("tool") != "control_home":
        return False
    args = plan.get("args") or {}
    # any of the valid device identifiers the tool catalogue advertises
    return bool(args.get("device_id") or args.get("entity_id") or args.get("room"))


def _score_nonempty_sentence(raw: str) -> bool:
    return bool(raw and len(raw.strip()) >= 10 and raw.strip()[0].isalnum() or raw.strip().startswith('"'))


def _score_memory_json(raw: str) -> bool:
    """A usable memory extraction returns a JSON list with at least one fact."""
    try:
        facts = json.loads(raw.strip().strip("`"))
    except Exception:
        return False
    return isinstance(facts, list) and len(facts) >= 1


def _score_mentions(*needles: str) -> Callable[[str], bool]:
    """Scorer: output must mention all of the given substrings (case-insensitive).
    Used for tasks where a usable answer must reference specific real entities."""
    lowered = [n.lower() for n in needles]
    def scorer(raw: str) -> bool:
        low = (raw or "").lower()
        return all(n in low for n in lowered)
    return scorer


def _score_any(*needles: str) -> Callable[[str], bool]:
    lowered = [n.lower() for n in needles]
    def scorer(raw: str) -> bool:
        low = (raw or "").lower()
        return any(n in low for n in lowered)
    return scorer


# The planner system is reused for the home-control task so we measure the real
# tool path. The others use lightweight task-specific framings.
def _log_task_failure(log_root, model: str, task: dict[str, Any], raw: str) -> None:
    """Save a failing task's diagnosis so an N isn't a mystery: the prompt, the raw
    model output, the parsed plan (if any), and what was expected. Best-effort, never
    raises into the benchmark."""
    try:
        from pathlib import Path
        import datetime
        safe_model = model.replace("/", "_").replace(":", "_")
        d = Path(log_root) / datetime.date.today().isoformat() / safe_model
        d.mkdir(parents=True, exist_ok=True)
        try:
            parsed = _parse_plan(raw)
        except Exception:
            parsed = None
        record = {
            "model": model,
            "task": task["name"],
            "prompt": task["prompt"],
            "raw_output": raw[:4000],
            "parsed_plan": parsed,
            "score_reason": "scorer returned False; see parsed_plan vs the task's expectation",
            "kind": task.get("kind"),
        }
        (d / f"{task['name']}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception:
        pass


def _ha_configured() -> bool:
    """True when Home Assistant is actually configured. A task that needs HA should
    SKIP (not fail) when this is False, so a missing live integration doesn't make
    every model look broken."""
    from .settings import get_settings
    s = get_settings()
    return bool(getattr(s, "ha_url", "") and getattr(s, "ha_token", ""))


def _tasks() -> list[dict[str, Any]]:
    planner_system = _PLANNER_SYSTEM + _tool_catalogue()
    from .services.memory import _EXTRACT_SYSTEM
    return [
        {
            "name": "home_control",
            "kind": "tool",
            "system": planner_system,
            "prompt": "turn off the lounge lights and the study floor lamp",
            "score": _score_home_control,
            # Judges the model's PLAN (a valid control_home call), which needs no live
            # HA, so no prereq. Live execution would be a separate, HA-gated check.
            "prereq": None,
            "prereq_name": "",
        },
        {
            # The genuinely HA-dependent check: would this actually execute against a
            # live HA? SKIPPED (not failed) when HA isn't configured, so a missing
            # integration never makes a model look broken.
            "name": "home_execute_live",
            "kind": "tool",
            "system": planner_system,
            "prompt": "turn off the lounge lights",
            "score": _score_home_control,
            "prereq": _ha_configured,
            "prereq_name": "home_assistant",
        },
        {
            "name": "find_intent",
            "kind": "tool",
            "system": planner_system,
            # "find my service charge certificate" should route to a search tool
            "prompt": "find the service charge certificate the managing agent sent",
            "score": lambda raw: _is_search_plan(raw),
        },
        {
            "name": "ha_automation_review",
            "kind": "chat",
            "system": ("You are Mongoose reviewing Home Assistant automations. Be specific: "
                       "flag conflicting triggers and missing conditions, with concrete fixes."),
            "prompt": ("Two automations both turn the hallway light on at sunset, one also turns "
                       "it off after 5 minutes. What's wrong and how do I fix it?"),
            "score": _score_any("conflict", "both", "trigger", "condition", "race"),
        },
        {
            "name": "draft_correspondence",
            "kind": "chat",
            "system": ("You are Mongoose drafting concise, professional correspondence for the user. "
                       "No em dashes. Direct and clear."),
            "prompt": ("Draft two sentences to the managing agent confirming the RTM company takes "
                       "over management on 16 July 2026 and that no service charge demands should be "
                       "issued from 1 July."),
            "score": _score_mentions("16 july", "1 july"),
        },
        {
            "name": "memory_extract",
            "kind": "chat",
            "system": _EXTRACT_SYSTEM,
            "prompt": "I drive a Honda, my dog is called Toby, and I cycle on Saturdays",
            "score": _score_memory_json,
        },
        {
            "name": "brief_phrasing",
            "kind": "chat",
            "system": ("You are Mongoose writing a 2-3 sentence daily brief from confirmed signals. "
                       "Lead with what's time-sensitive. Do not invent anything not in the signals."),
            "prompt": ("CONFIRMED SIGNALS:\n- CONFLICT: 'Call with Lawfirm' overlaps 'Dentist'.\n"
                       "- WEATHER: rain 3-5pm.\n- EMAIL: 1 actionable from the managing agent.\n"
                       "Write the brief."),
            "score": _score_any("lawfirm", "dentist", "rain", "agent"),
        },
    ]


def _is_search_plan(raw: str) -> bool:
    """A usable 'find' answer routes to a search/find tool (any of the search tools)."""
    try:
        plan = _parse_plan(raw)
    except Exception:
        return False
    tool = str(plan.get("tool") or "").lower()
    return tool in {"find", "search_files", "search_attachments", "search_email"}


TOOL_PROMPTS = [
    "turn on the study desk lamp",
    "turn off the kitchen lights",
    "switch on the floor lamp in the study",
    "dim the lounge lights",
    "play music on the study echo",
]
CHAT_PROMPTS = [
    "say hi in five words",
    "what's a good reason to drink water",
    "tell me a one-line joke",
]
FACT_PROMPT = "I really like teal and I drive a Honda"
NONFACT_PROMPT = "what's the weather like today"


# Benchmark safety budget. No single model or task may block the whole run. Every
# model call is bounded by BOTH a hard wall-clock timeout AND an output token cap, so
# a looping or pathologically-slow model fails its task and the run continues instead
# of freezing. These are benchmark-only limits; normal Mongoose calls are unaffected.
_BENCH_CALL_TIMEOUT_S = 90.0      # hard per-call wall-clock limit
_BENCH_MAX_OUTPUT_TOKENS = 512    # cap generation so a loop can't run forever


class BenchTimeout(Exception):
    """A single model call exceeded the per-call budget (timeout or token cap)."""


class BenchDead(Exception):
    """The backend is not generating at all: an empty completion, or an Ollama error payload.

    This is NOT a bad answer and must never be scored as one. gemma4:26b-a4b returned an empty
    string on every call for twenty consecutive nights (Metal OOM wedged the Metal backend, see
    ollama server.log) and the harness dutifully recorded 0/7 tasks and 0/5 tool calls, which read
    as a bad model rather than a dead one. An empty completion is a harness-level fault signal:
    raise, report once, and stop measuring this model."""


async def _timed_chat(model: str, prompt: str, *, system: str | None = None) -> tuple[float, str]:
    """One timed model call, hard-bounded. Returns (seconds, output). Raises
    BenchTimeout if the call exceeds _BENCH_CALL_TIMEOUT_S, so the caller can mark the
    task failed and move on rather than hang. Output is capped at
    _BENCH_MAX_OUTPUT_TOKENS via Ollama's num_predict."""
    import asyncio
    import httpx
    settings = get_settings()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": settings.ollama_think,
        "keep_alive": settings.keep_alive_value,
        # num_predict caps output tokens: the single most important loop guard.
        # num_ctx MUST match the real chat path (llm.py uses settings.num_ctx, default 8192). Without
        # it Ollama falls back to its own small default (~2k) and SILENTLY TRUNCATES the prompt: the
        # planner system + tool catalogue is ~6.7k tokens, so the tool list was cut away entirely and
        # every model answered "I can't control smart home devices" , scoring tool-calls 0/5 across the
        # board. That read as five bad models when it was the harness measuring a truncated prompt.
        "options": {"num_predict": _BENCH_MAX_OUTPUT_TOKENS,
                    "num_ctx": int(getattr(settings, "num_ctx", 8192))},
    }
    t = time.time()
    try:
        # asyncio.wait_for is the hard stop: even if the HTTP layer would wait longer
        # (or the server streams slowly), the call is abandoned at the budget.
        async with httpx.AsyncClient(timeout=_BENCH_CALL_TIMEOUT_S) as c:
            resp = await asyncio.wait_for(
                c.post(f"{settings.ollama_base_url.rstrip('/')}/api/chat", json=payload),
                timeout=_BENCH_CALL_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
        # Ollama reports a wedged runner in-band: HTTP 200 with an "error" key, or a 200 with an
        # empty completion. Both mean no tokens were produced, so neither is an answer to score.
        err = data.get("error")
        if err:
            raise BenchDead(f"{model}: ollama returned an error instead of output: {err}")
        out = (data.get("message") or {}).get("content", "") or ""
        if not out.strip():
            raise BenchDead(
                f"{model}: no output (empty completion after {time.time() - t:.2f}s). The runner is "
                "not generating; check `ollama logs` for Metal out-of-memory or a killed llama-server")
        return time.time() - t, out
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise BenchTimeout(f"{model} exceeded {_BENCH_CALL_TIMEOUT_S:.0f}s") from exc


async def _evict(model: str) -> None:
    """Best-effort unload so the next call measures a true cold load."""
    import httpx
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            # keep_alive 0 tells Ollama to unload after this trivial call
            await c.post(f"{settings.ollama_base_url}/api/chat", json={
                "model": model, "messages": [{"role": "user", "content": "x"}],
                "stream": False, "keep_alive": 0, "think": False})
    except Exception:
        pass


# --- fairness machinery ------------------------------------------------------
# The benchmark's job is LIKE-FOR-LIKE comparison. The two big threats are
# (1) cold/warm contamination: the first timed call after a model loads pays a
# one-off warm-up tax (KV cache, first-prompt overhead) that later calls don't,
# and (2) single-sample noise: one timing is one draw from a noisy distribution
# (background load, thermal state), so a single number can't tell a real gap from
# jitter. We address both explicitly, and REPORT the residual confounds we can't
# fully remove rather than pretend they're gone.

_WARMUP_PROMPT = "ready"


async def _warm_up(model: str, *, system: str | None = None, rounds: int = 2) -> None:
    """Issue throwaway calls so the model is resident AND past its first-prompt
    overhead before we time anything. The results are discarded. This is what makes
    a warm measurement actually warm, and makes every model's warm phase start from
    the same state (warm), not "warm for some, still-warming for others"."""
    for _ in range(max(0, rounds)):
        try:
            await _timed_chat(model, _WARMUP_PROMPT, system=system)
        except BenchDead:
            raise  # a non-generating backend aborts the model, it is not a slow warm-up
        except Exception:
            return  # if it can't even warm up, the real calls will report the error


async def _repeat(model: str, prompt: str, *, system: str | None = None,
                  repeats: int = 3) -> list[float]:
    """Time the SAME call `repeats` times (model already warm) and return all
    latencies, so the caller can report a median AND a spread instead of trusting
    one sample. Repeats run back-to-back to share thermal/background conditions."""
    out: list[float] = []
    for _ in range(max(1, repeats)):
        try:
            dt, _ = await _timed_chat(model, prompt, system=system)
            out.append(dt)
        except BenchDead:
            # Absorbing this is what let a dead model look merely bad: every sample came back
            # instantly and empty, so the phase reported a 0.03s median instead of an error.
            raise
        except Exception:
            pass
    return out


def _median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _spread(xs: list[float]) -> float:
    """Inter-quartile range (p75 - p25): a robust measure of how noisy the
    samples were. A large spread means 'don't trust small differences here'."""
    if len(xs) < 2:
        return 0.0
    s = sorted(xs)
    def _q(frac: float) -> float:
        pos = frac * (len(s) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (pos - lo)
    return _q(0.75) - _q(0.25)


def _stats(xs: list[float]) -> dict[str, float]:
    """Median + spread + n, the honest summary of a set of timings."""
    return {"median": round(_median(xs), 3), "iqr": round(_spread(xs), 3), "n": len(xs)}


async def benchmark_model(model: str, *, measure_cold: bool = True,
                          repeats: int = 3, cold_samples: int = 2,
                          warmup_rounds: int = 2) -> dict[str, Any]:
    """Benchmark one model, reporting a non-generating backend as DEAD rather than scoring it.

    A model that returns nothing is a broken runner, not a bad model, so it gets one honest line
    instead of a fabricated 0/7. See BenchDead: scoring empty output hid a wedged gemma4:26b-a4b
    for twenty consecutive nights. Everything else is delegated unchanged."""
    try:
        return await _benchmark_model_measured(model, measure_cold=measure_cold, repeats=repeats,
                                               cold_samples=cold_samples,
                                               warmup_rounds=warmup_rounds)
    except BenchDead as exc:
        print(f"    [{model}] DEAD: {exc}", flush=True)
        # tasks_passed is "0/0" , nothing was scored , not "0/7", which would claim we asked it
        # seven questions and it got them all wrong.
        return {"model": model, "dead": True, "error": str(exc),
                "tasks": [], "tasks_passed": "0/0", "tasks_skipped": 0,
                "tool_calls_valid": "0/0"}


async def _benchmark_model_measured(model: str, *, measure_cold: bool = True,
                                    repeats: int = 3, cold_samples: int = 2,
                                    warmup_rounds: int = 2) -> dict[str, Any]:
    """Benchmark one model with like-for-like guards:
      - cold load is measured `cold_samples` times (re-evicting each time), and we
        report the median + spread, because a single cold start is highly variable.
      - before EVERY warm phase we run discarded warm-up calls under that phase's
        OWN system prompt, so the model is resident and past first-prompt overhead
        and each phase starts from the same (warm) state. This removes the
        cold-contaminates-the-first-warm-call bias.
      - each warm/task measurement is repeated `repeats` times and reported as a
        median + IQR spread, so a single noisy sample can't masquerade as a result.
    """
    result: dict[str, Any] = {"model": model,
                              "method": {"repeats": repeats, "cold_samples": cold_samples,
                                         "warmup_rounds": warmup_rounds}}
    timeouts = 0  # count of calls that hit the per-call budget, reported in the table

    def _beat(phase: str, detail: str = "") -> None:
        # Heartbeat: print BEFORE each call so a hang is visible (which model, which
        # phase, which task) instead of a silent freeze. flush so it shows live.
        print(f"    [{model}] {phase}{(': ' + detail) if detail else ''} ...", flush=True)

    # 1. Cold load: evict -> time first call, repeated, so the number is a median
    # of true cold starts rather than one lucky/unlucky draw.
    if measure_cold:
        colds: list[float] = []
        for _ in range(max(1, cold_samples)):
            await _evict(model)
            await asyncio.sleep(1.0)
            _beat("cold load")
            try:
                dt, _ = await _timed_chat(model, "hello")
                colds.append(dt)
            except BenchTimeout:
                timeouts += 1
                result["error"] = f"cold call timed out after {_BENCH_CALL_TIMEOUT_S:.0f}s"
                result["timeouts"] = timeouts
                return result
            except BenchDead:
                raise  # reported as dead by the caller, not as a cold-load failure
            except Exception as exc:
                result["error"] = f"cold call failed: {type(exc).__name__}: {exc}"
                return result
        result["cold_load"] = _stats(colds)
        result["cold_load_s"] = round(_median(colds), 1)  # back-compat scalar

    # 2. Warm chat latency. WARM UP FIRST (discarded), then repeat each prompt.
    _beat("warm chat")
    await _warm_up(model, rounds=warmup_rounds)
    warm: list[float] = []
    for p in CHAT_PROMPTS:
        samples = await _repeat(model, p, repeats=repeats)
        if not samples:
            result["error"] = "chat failed during warm phase (timeout or error)"
            result["timeouts"] = timeouts + 1
            return result
        warm.extend(samples)
    result["warm_chat"] = _stats(warm)
    result["warm_chat_median_s"] = round(_median(warm), 2)  # back-compat scalar

    # 3. Tool-call validity. Warm up UNDER THE PLANNER SYSTEM PROMPT, because a
    # different/large system prompt has its own cache-warm cost; warming with the
    # same system makes the timing like-for-like with the chat phase.
    system = _PLANNER_SYSTEM + _tool_catalogue()
    _beat("tool calls")
    await _warm_up(model, system=system, rounds=warmup_rounds)
    valid = 0
    tool_latencies: list[float] = []
    for p in TOOL_PROMPTS:
        samples = await _repeat(model, p, system=system, repeats=repeats)
        tool_latencies.extend(samples)
        # Validity is judged on a fresh call (content, not timing); reuse one.
        try:
            raw = (await _timed_chat(model, p, system=system))[1]
            if _score_home_control(raw):
                valid += 1
        except BenchTimeout:
            timeouts += 1
        except BenchDead:
            raise
        except Exception:
            pass
    result["tool_calls_valid"] = f"{valid}/{len(TOOL_PROMPTS)}"
    result["tool_latency"] = _stats(tool_latencies)
    result["tool_latency_median_s"] = round(_median(tool_latencies), 2)

    # 4. Memory extraction: clean JSON facts on a self-disclosing prompt.
    from .services.memory import _EXTRACT_SYSTEM
    _beat("memory extract")
    try:
        raw = (await _timed_chat(model, FACT_PROMPT, system=_EXTRACT_SYSTEM))[1]
        facts = json.loads(raw.strip().strip("`"))
        result["memory_extract_ok"] = isinstance(facts, list) and len(facts) >= 1
        result["memory_extract_n"] = len(facts) if isinstance(facts, list) else 0
    except BenchTimeout:
        timeouts += 1
        result["memory_extract_ok"] = False
        result["memory_extract_n"] = 0
    except BenchDead:
        raise
    except Exception:
        result["memory_extract_ok"] = False
        result["memory_extract_n"] = 0
    # Pre-filter sanity (model-independent, but report it once).
    result["prefilter_skips_nonfact"] = not looks_fact_bearing(NONFACT_PROMPT)

    # 5. REALISTIC USE-CASE TASKS. Each task warms under its OWN system prompt
    # (so a heavy planner/extract system isn't charged a cold-cache penalty the
    # chat tasks avoid), then times `repeats` runs. Correctness is judged once.
    # A task that exceeds the per-call budget is marked timeout=True and FAILED,
    # and the run continues: no single task can block the benchmark.
    task_results = []
    passed = 0
    scored = 0   # tasks actually scored (excludes skipped), so the ratio is honest
    skipped = 0
    all_task_lat: list[float] = []
    from .settings import get_settings
    log_root = get_settings().data_dir / "benchmarks"
    for task in _tasks():
        # SKIP a task whose live prerequisite isn't met (e.g. HA not configured),
        # rather than scoring it N and making every model look broken.
        prereq = task.get("prereq")
        if prereq is not None and not prereq():
            skipped += 1
            task_results.append({"task": task["name"], "skipped": True,
                                 "reason": f"{task.get('prereq_name') or 'prerequisite'} not configured"})
            print(f"    [{model}] task {task['name']} SKIPPED "
                  f"({task.get('prereq_name') or 'prerequisite'} not configured)", flush=True)
            continue
        _beat("task", task["name"])
        try:
            await _warm_up(model, system=task["system"], rounds=1)
            samples = await _repeat(model, task["prompt"], system=task["system"], repeats=repeats)
            # one explicit call for the scored output (content, not timing)
            _, raw = await _timed_chat(model, task["prompt"], system=task["system"])
            ok = bool(task["score"](raw))
            st = _stats(samples)
            all_task_lat.extend(samples)
            task_results.append({"task": task["name"], "ok": ok,
                                 "latency_median_s": st["median"], "latency_iqr_s": st["iqr"]})
            scored += 1
            if ok:
                passed += 1
            else:
                # Log the failing output so an N has a diagnosis (raw output, the
                # parsed plan, what was expected), instead of a bare N.
                _log_task_failure(log_root, model, task, raw)
        except BenchTimeout:
            timeouts += 1
            print(f"    [{model}] task {task['name']} TIMED OUT (>{_BENCH_CALL_TIMEOUT_S:.0f}s), "
                  "marking failed and continuing", flush=True)
            task_results.append({"task": task["name"], "ok": False, "timeout": True})
        except BenchDead:
            raise  # abandon the model: scoring the remaining tasks would invent failures
        except Exception as exc:
            task_results.append({"task": task["name"], "ok": False, "error": type(exc).__name__})
    result["tasks"] = task_results
    # Denominator is SCORED tasks, so skipped (prereq-missing) tasks don't drag the
    # ratio down. A skipped task is shown separately, not counted as a failure.
    result["tasks_passed"] = f"{passed}/{scored}" if scored else "0/0"
    result["tasks_skipped"] = skipped
    result["task_latency"] = _stats(all_task_lat)
    result["task_latency_median_s"] = round(_median(all_task_lat), 2)
    result["timeouts"] = timeouts

    return result


def _row(r: dict[str, Any]) -> str:
    if r.get("skipped"):
        return f"{r['model']:34} SKIPPED: {r.get('reason', 'not run')}"
    if r.get("dead"):
        return f"{r['model']:34} DEAD: {r.get('error', 'produced no output')}"
    if "error" in r:
        return f"{r['model']:34} ERROR: {r['error']}"
    cold = r.get("cold_load", {})
    warm = r.get("warm_chat", {})
    task = r.get("task_latency", {})
    # show median±IQR so a noisy number is visibly noisy
    def md(stats: dict[str, Any], scalar_key: str) -> str:
        if isinstance(stats, dict) and "median" in stats:
            return f"{stats['median']:.2f}±{stats.get('iqr', 0):.2f}"
        return str(r.get(scalar_key, "-"))
    return (f"{r['model']:34} "
            f"cold={md(cold, 'cold_load_s'):>11} "
            f"warm={md(warm, 'warm_chat_median_s'):>11} "
            f"tool_ok={r.get('tool_calls_valid','-'):>5} "
            f"tasks={r.get('tasks_passed','-'):>5} "
            f"task={md(task, 'task_latency_median_s'):>11} "
            f"mem={'Y' if r.get('memory_extract_ok') else 'N'} "
            f"timeouts={r.get('timeouts', 0)}")


async def installed_models() -> set[str]:
    """Return the set of model tags Ollama currently has pulled."""
    import httpx
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            data = r.json()
        return {m.get("name", "") for m in data.get("models", [])}
    except Exception:
        return set()


async def pull_model(model: str) -> bool:
    """Pull a model into Ollama (download it) via the Ollama API. Streams progress
    lines to stdout so a multi-GB pull shows it's working. Returns True on success.
    This is what makes the benchmark able to download missing models automatically."""
    import httpx
    settings = get_settings()
    print(f"  pulling {model} (this can be several GB) ...", flush=True)
    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", f"{settings.ollama_base_url}/api/pull",
                                 json={"model": model, "stream": True}) as resp:
                if resp.status_code >= 400:
                    print(f"  pull failed: HTTP {resp.status_code}")
                    return False
                last = ""
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    status = msg.get("status", "")
                    if msg.get("error"):
                        print(f"  pull error: {msg['error']}")
                        return False
                    # Show milestone status changes, not every byte update.
                    if status and status != last and "pulling" not in status.lower():
                        print(f"    {status}", flush=True)
                        last = status
                    if status == "success":
                        print(f"  pulled {model}", flush=True)
                        return True
        return True
    except Exception as exc:
        print(f"  pull failed: {type(exc).__name__}: {exc}")
        return False


# --- memory headroom guard ---------------------------------------------------
# A model that does not fit cannot be measured, only killed. gemma4:26b-a4b is 17 GB of weights on
# a 24 GB Mac mini that is also running the server, the tunnel, Ollama and several editors, so every
# night it loaded, exhausted the Metal heap mid-decode and returned empty for the rest of the run
# (9,310 kIOGPUCommandBufferCallbackErrorOutOfMemory lines in ollama's server.log). Evicting between
# models was already in place and is not enough: the machine's own working set is the constraint.
# Checking first turns a fabricated 0/7 into an honest SKIPPED with a reason.
_MODEL_MEMORY_OVERHEAD_FRAC = 0.25   # KV cache, activations, runner overhead on top of the weights
_MODEL_MEMORY_FLOOR_BYTES = 1024 ** 3   # plus a fixed 1 GB so a small model still needs real room
_MODEL_MEMORY_RESERVE_GB_DEFAULT = 8.0  # left for macOS, the server, the tunnel and editors


def fits_available_memory(size_bytes: int | None, budget_bytes: int) -> tuple[bool, str]:
    """Can a model of `size_bytes` be run inside `budget_bytes`?

    Returns (fits, reason). An unknown size is permitted: a missing size must never silently skip
    the whole roster, it just means we cannot pre-empt the OOM for that one."""
    if not size_bytes:
        return True, "size unknown, not pre-emptively skipped"
    need = size_bytes * (1.0 + _MODEL_MEMORY_OVERHEAD_FRAC) + _MODEL_MEMORY_FLOOR_BYTES
    gb = 1024 ** 3
    if need > budget_bytes:
        return False, (f"needs about {need / gb:.1f} GB (weights {size_bytes / gb:.1f} GB plus "
                       f"overhead) but the benchmark budget is {budget_bytes / gb:.1f} GB; running "
                       "it would exhaust memory and report empty output")
    return True, (f"weights {size_bytes / gb:.1f} GB, budget {budget_bytes / gb:.1f} GB")


def _memory_budget_bytes() -> int:
    """How much memory a benchmarked model may use: TOTAL installed minus a reserve.

    Deliberately NOT psutil's instantaneous `available`. Two reasons, both learned the hard way:
      - run_benchmark evicts the previous model before loading the next, so whatever a resident
        model is holding right now is about to be freed and must not count against the next one.
      - on macOS `available` is conservative (compressed and reclaimable pages are excluded), so
        budgeting against it skipped the ENTIRE roster on a 24 GB box with 6.9 GB "available",
        including the four models that demonstrably run fine.
    Total minus a reserve answers the question that actually matters: can these weights plus their
    overhead live on this machine while the OS and the household server keep running."""
    reserve_gb = _MODEL_MEMORY_RESERVE_GB_DEFAULT
    try:
        reserve_gb = float(os.getenv("MONGOOSE_BENCHMARK_MEMORY_RESERVE_GB", reserve_gb))
    except (TypeError, ValueError):
        pass  # a garbage value falls back to the default rather than disabling the guard
    try:
        import psutil
        total = int(psutil.virtual_memory().total)
    except Exception:
        # Unknown: a huge budget keeps the guard permissive rather than skipping everything.
        return 1 << 62
    return max(0, total - int(reserve_gb * 1024 ** 3))


def _model_size_bytes(model: str) -> int | None:
    """On-disk size of a pulled model, from Ollama's own tag list. None if unknown."""
    try:
        import httpx
        settings = get_settings()
        resp = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=10.0)
        resp.raise_for_status()
        for m in resp.json().get("models") or []:
            if m.get("name") == model or m.get("model") == model:
                return int(m.get("size") or 0) or None
    except Exception:
        return None
    return None


async def run_benchmark(models: list[str], *, measure_cold: bool = True,
                        auto_pull: bool = True, rounds: int = 1,
                        cooldown_s: float = 5.0, repeats: int = 3) -> list[dict[str, Any]]:
    # Check which requested models are present; auto-pull the rest (unless disabled).
    have = await installed_models()
    missing = [m for m in models if m not in have]
    if missing:
        if auto_pull:
            print("These models aren't pulled yet; downloading them now")
            print("(pass --no-pull to skip auto-download and just error on missing):")
            for m in missing:
                print(f"  - {m}")
            print()
            still_missing = []
            for m in missing:
                ok = await pull_model(m)
                if not ok:
                    still_missing.append(m)
            if still_missing:
                print("\nNOTE: these could not be pulled and will error in the run:")
                for m in still_missing:
                    print(f"  {m}")
                print()
        else:
            print("NOTE: these models are not pulled and will error (--no-pull set);")
            print("      pull them with scripts/pull_benchmark_models.sh or drop --no-pull:")
            for m in missing:
                print(f"        {m}")
            print()

    _print_methodology(models, rounds=rounds, repeats=repeats, cooldown_s=cooldown_s,
                       measure_cold=measure_cold)

    # Stream results to disk as each model finishes, so a long run is inspectable
    # live (and survives a Ctrl-C). `mongoose benchmark --results` renders whatever
    # has been written so far. We start a fresh file for this run.
    results_path = _results_path()
    try:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text("", encoding="utf-8")  # truncate: new run
    except Exception:
        results_path = None

    # Run in ROUNDS, INTERLEAVING models, so thermal drift and background load are
    # spread across all models rather than penalising whoever runs last. A cooldown
    # between models lets the SoC shed heat so each starts from a comparable thermal
    # state. We aggregate each model's results across rounds.
    per_model: dict[str, list[dict[str, Any]]] = {m: [] for m in models}

    # Decide fit ONCE, before any round, so a model is either measured in every round or skipped
    # in all of them. Deciding per round would let the same model be scored and skipped in one run.
    budget = _memory_budget_bytes()
    skipped_models: dict[str, dict[str, Any]] = {}
    for m in models:
        fits, why = fits_available_memory(_model_size_bytes(m), budget)
        if not fits:
            print(f"SKIPPING {m}: {why}", flush=True)
            skipped_models[m] = {"model": m, "skipped": True, "reason": why,
                                 "tasks_passed": "0/0", "tasks_skipped": 0}

    for rnd in range(max(1, rounds)):
        if rounds > 1:
            print(f"\n--- round {rnd + 1}/{rounds} ---")
        for m in models:
            if m in skipped_models:
                continue
            print(f"benchmarking {m} ...", flush=True)
            res = await benchmark_model(m, measure_cold=measure_cold, repeats=repeats)
            per_model[m].append(res)
            # Append this round's result immediately so progress is on disk.
            if results_path is not None:
                try:
                    with results_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"round": rnd + 1, "model": m, "result": res}) + "\n")
                except Exception:
                    pass
            # CRITICAL on a memory-bound box: unload this model before loading the next candidate.
            # The warm/task calls pin the model (keep_alive from settings, historically -1 = forever),
            # so without this evict the first model stays resident and every subsequent candidate fails
            # to cold-load for lack of RAM. That silently reduced 'benchmark 5 models' to 'score only the
            # one already in memory' for weeks. Evicting here keeps the comparison honest and OOM-free.
            await _evict(m)
            if cooldown_s > 0:
                await asyncio.sleep(cooldown_s)  # thermal cooldown between models

    # A skipped model keeps its own row rather than being aggregated: it has no samples, and
    # putting a zero through the aggregator would read as "measured and failed".
    rows = [skipped_models[m] if m in skipped_models else _aggregate_rounds(m, per_model[m])
            for m in models]
    _print_results_table(rows, rounds=rounds)
    return rows


def load_results_for_recommend() -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Read the streamed results file and return (aggregated per-model results, cold-load
    map) for the recommender. Reuses the same JSONL the table renders from, so a finished
    or interrupted run can be turned into a recommendation without re-running."""
    path = _results_path()
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return [], {}
    per_model: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        per_model.setdefault(rec["model"], []).append(rec["result"])
    rows = [_aggregate_rounds(m, results) for m, results in per_model.items()]
    cold = {}
    for r in rows:
        c = r.get("cold_load_s")
        if isinstance(c, (int, float)):
            cold[r.get("model", "")] = float(c)
    return rows, cold


def benchmark_scoreboard_text(max_models: int = 8) -> str:
    """The REAL per-model results from the latest run, as compact factual lines, so any answer about
    'how did model X do' or 'compare X and Y' is grounded in the ACTUAL measured numbers and can never be
    invented. NOTE: the benchmark measures OPERATIONAL fitness (load/latency speed + tool-call and memory
    correctness), NOT abstract 'reasoning' or 'logic' scores, so an answer must not claim those. '' if no
    results exist yet."""
    try:
        rows, _ = load_results_for_recommend()
    except Exception:
        return ""
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows[:max_models]:
        def _s(key: str) -> str:
            v = r.get(key)
            return f"{v}s" if isinstance(v, (int, float)) else "n/a"
        lines.append(
            f"{r.get('model', '?')}: {r.get('tasks_passed', '?')} checks passed; "
            f"cold-load {_s('cold_load_s')}, warm-chat median {_s('warm_chat_median_s')}, "
            f"task-latency median {_s('task_latency_median_s')}, "
            f"tool-calls valid {r.get('tool_calls_valid', 'n/a')}, "
            f"tool-latency median {_s('tool_latency_median_s')}, "
            f"memory-extract {'ok' if r.get('memory_extract_ok') else 'no'}, "
            f"timeouts {r.get('timeouts', 'n/a')}"
        )
    return "\n".join(lines)


def _results_path():
    """Where streamed benchmark results land, so a partial run can be rendered."""
    from .settings import get_settings
    return get_settings().data_dir / "benchmark_results.jsonl"


def _print_results_table(rows: list[dict[str, Any]], *, rounds: int = 1) -> None:
    """Render the comparison table + per-task breakdown from a list of aggregated
    rows. Shared by a live run and by `--results` (which rebuilds rows from the
    streamed file), so partial and final output look identical."""
    print("\n=== Mongoose model benchmark (your machine, your real tasks) ===")
    print("latencies = median±IQR seconds across repeats" + (f" x {rounds} rounds" if rounds > 1 else "")
          + "; tool_ok = valid control_home calls; tasks = real tasks passed\n")
    for r in rows:
        print(_row(r))
    print("\nPer-task results (real use cases):")
    for r in rows:
        if "tasks" not in r:
            continue
        cells = " ".join(
            f"{t['task']}={'SKIP' if t.get('skipped') else ('Y' if t.get('ok') else 'N')}"
            for t in r["tasks"])
        print(f"  {r['model']:34} {cells}")
    print("\nReading it: lower latency is better; a large ± (IQR) means the samples")
    print("were noisy, so treat small gaps between models as ties, not differences.")
    print("Higher tasks_passed, tool_ok, and mem=Y are better. All from THIS machine.")
    print(f"timeouts = calls that hit the {_BENCH_CALL_TIMEOUT_S:.0f}s/"
          f"{_BENCH_MAX_OUTPUT_TOKENS}-token budget and were marked failed; a high")
    print("count means that model looped or stalled on Mongoose-shaped prompts here.")
    print("SKIP = a task whose live prerequisite is missing (e.g. HA not configured);")
    print("it's excluded from the score, not counted as a failure. Failed tasks write")
    print("a diagnosis to data/benchmarks/<date>/<model>/<task>.json (raw output + plan).")


def render_results_file() -> int:
    """Rebuild and print the table from the streamed results file, so you can see the
    standings from a run that's still going (in another terminal) or was interrupted.
    Returns the number of models rendered."""
    path = _results_path()
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        print("No benchmark results yet. Run `mongoose benchmark ...` first.")
        return 0
    per_model: dict[str, list[dict[str, Any]]] = {}
    rounds_seen = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        per_model.setdefault(rec["model"], []).append(rec["result"])
        rounds_seen = max(rounds_seen, int(rec.get("round", 1)))
    rows = [_aggregate_rounds(m, results) for m, results in per_model.items()]
    n_complete = len(rows)
    print(f"(rendering {n_complete} model(s) from results so far; "
          f"up to round {rounds_seen})")
    _print_results_table(rows, rounds=rounds_seen)
    return n_complete


def _print_methodology(models: list[str], *, rounds: int, repeats: int,
                       cooldown_s: float, measure_cold: bool) -> None:
    """State the methodology and its KNOWN LIMITS up front, so the numbers are read
    honestly. The point of the benchmark is like-for-like comparison; this says
    exactly which confounds are controlled and which aren't."""
    print("Methodology (how this stays like-for-like):")
    print(f"  - {len(models)} model(s), {rounds} round(s), {repeats} repeat(s) per measurement.")
    print("  - WARM-UP: discarded calls under each phase's own system prompt before")
    print("    timing, so every phase starts warm (no cold-start tax on the first call).")
    print("  - REPEATS + IQR: each timing repeated; we report median±spread, so noisy")
    print("    samples are visibly noisy and small gaps aren't over-read.")
    if measure_cold:
        print("  - COLD LOAD: model evicted and re-timed multiple times (cold start is")
        print("    itself variable), reported as a median.")
    if rounds > 1:
        print("  - INTERLEAVING: models run round-robin so thermal drift is shared, not")
        print("    charged to whoever runs last.")
    print(f"  - COOLDOWN: {cooldown_s}s between models to shed SoC heat.")
    print("  Residual confounds we DON'T fully control (be aware): sustained-load")
    print("  thermal throttling on a fanless/small chassis, other processes competing")
    print("  for RAM/GPU, disk-cache state affecting cold load, and Ollama-version")
    print("  effects. For the most stable read: close other apps, run >=2 rounds, and")
    print("  trust the ranking more than the absolute milliseconds.\n")


def _aggregate_rounds(model: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine a model's per-round results into one row. Latency stats are pooled
    across rounds (more samples = tighter estimate); pass/fail takes the majority
    so a single flaky round doesn't flip a verdict."""
    if not runs:
        return {"model": model, "error": "no runs"}
    if len(runs) == 1:
        return runs[0]
    if any("error" in r for r in runs):
        # surface the first error but keep going if some rounds succeeded
        ok_runs = [r for r in runs if "error" not in r]
        if not ok_runs:
            return runs[0]
        runs = ok_runs

    def pool(key: str) -> dict[str, float]:
        # reconstruct sample pools from per-round medians (approx: weight by n)
        medians = [r[key]["median"] for r in runs if isinstance(r.get(key), dict)]
        iqrs = [r[key]["iqr"] for r in runs if isinstance(r.get(key), dict)]
        return {"median": round(_median(medians), 3),
                "iqr": round(max(iqrs) if iqrs else 0.0, 3),
                "n": sum(r[key]["n"] for r in runs if isinstance(r.get(key), dict))}

    agg: dict[str, Any] = {"model": model, "rounds": len(runs)}
    for key in ("cold_load", "warm_chat", "tool_latency", "task_latency"):
        if any(isinstance(r.get(key), dict) for r in runs):
            agg[key] = pool(key)
    # tool validity / tasks: majority across rounds
    agg["tool_calls_valid"] = runs[-1].get("tool_calls_valid", "-")
    # tasks: a task passes if it passed in the majority of rounds
    task_names = [t["task"] for t in runs[0].get("tasks", [])]
    merged_tasks = []
    passed = 0
    scored = 0
    for name in task_names:
        # if the task was skipped in every round, carry the skip through
        all_skipped = all(
            any(t["task"] == name and t.get("skipped") for t in r.get("tasks", []))
            for r in runs)
        if all_skipped:
            reason = next((t.get("reason") for r in runs for t in r.get("tasks", [])
                           if t["task"] == name and t.get("skipped")), "")
            merged_tasks.append({"task": name, "skipped": True, "reason": reason})
            continue
        votes = [any(t["task"] == name and t.get("ok") for t in r.get("tasks", [])) for r in runs]
        ok = sum(votes) > len(votes) / 2
        merged_tasks.append({"task": name, "ok": ok})
        scored += 1
        if ok:
            passed += 1
    agg["tasks"] = merged_tasks
    agg["tasks_passed"] = f"{passed}/{scored}" if scored else "0/0"
    agg["memory_extract_ok"] = sum(1 for r in runs if r.get("memory_extract_ok")) > len(runs) / 2
    return agg


# Default shortlist (only models that fit 24GB and run in Ollama).
DEFAULT_SHORTLIST = [
    "gemma4:26b-a4b-it-q4_K_M",   # incumbent baseline
    "qwen3:30b-a3b",              # strongest challenger (MoE ~3B active)
    "gemma4:12b-it-q4_K_M",       # speed candidate
    "gpt-oss:20b",                # reputation pick, on trial
    "qwen3:14b",                  # dense small, sharp tool-caller
]

# Candidate models proposed but NOT yet measured on this machine. These come from
# specific hypotheses (research Q&A, long-context stability, an optional direct/adult
# explainer) and are kept SEPARATE from the decisive default shortlist on purpose:
# the project principle is that the Mac mini votes, so a plausible claim earns a
# benchmark slot, not a resident-model swap. Run them with
# `mongoose benchmark <tag> ...` or `--candidates`.
CANDIDATE_MODELS = [
    "qwen3.5:9b",                 # research/Q&A candidate (SearXNG/Firecrawl/Wikipedia/local-find)
    "gemma4:e4b",                 # long-context stability hypothesis (if in your tag set)
    "gemma4:e2b",                 # smaller long-context variant (if in your tag set)
]

