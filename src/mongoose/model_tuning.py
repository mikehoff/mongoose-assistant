from __future__ import annotations

import json
from typing import Any

# Lane 2: the model-tuning lane that VESTS over time.
#
# Lane 1 (install) gets a working assistant into the user's hands fast on SANE DEFAULTS , no
# asking them to choose models they can't yet evaluate. Lane 2 opens later, once the system
# has actually been used and benchmarked under realistic conditions, and only THEN proposes
# keeping or switching models/settings, backed by real evidence.
#
# The whole point is honesty about WHEN the recommendation has earned standing. Lane 2 must
# never claim to be "vested" on thin data. So this module is mostly a careful GATE:
#
#   real usage (the trigger)  ->  background benchmark runs  ->  enough scored coverage
#       ->  VESTED  ->  recommend; auto-apply only if the win is large AND safe.
#
# It reuses what already exists: benchmark.load_results_for_recommend() for coverage +
# results, and benchmark_recommend.recommend() for the verdict. It adds the vesting logic,
# a reversible override layer (apply/revert without touching the user's base .env), and the
# surfacing state (cockpit card + phone nudge), all dismissible.

# --- vesting thresholds (transparent + tunable) -----------------------------
# These are deliberately conservative: a recommendation on thin data is worse than none.
MIN_USAGE_TURNS = 40          # real user turns handled before we even trigger a benchmark
MIN_ACTIVE_DAYS = 3           # spread across at least this many distinct days (not one binge)
MIN_ROUNDS_PER_MODEL = 3      # each candidate model needs this many scored benchmark rounds
MIN_MODELS_COMPARED = 2       # need at least two models to have a meaningful choice

# auto-apply guardrails: only switch on our own if the win is BOTH large and safe.
AUTO_APPLY_MIN_QUALITY_GAIN = 0.15   # >= 15 percentage points better on the real task mix
AUTO_APPLY_MAX_LATENCY_REGRESSION_S = 1.0  # and not meaningfully slower per turn

# app_state keys
_K_OVERRIDES = "model_overrides"          # JSON {role: model} , the reversible apply layer
_K_DISMISSED = "lane2_dismissed_sig"      # the recommendation signature the user dismissed
_K_LAST_APPLY = "lane2_last_autoapply"    # JSON receipt of the last auto-apply (for revert)


# --- the reversible override layer -------------------------------------------
# model_for_role() consults these BEFORE the env base, so apply/revert never rewrites the
# user's .env. An empty/absent override == use the configured base. This is what makes a
# Lane 2 switch safe to undo.

def get_model_overrides() -> dict[str, str]:
    from .db import get_app_state
    raw = get_app_state(_K_OVERRIDES, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {k: v for k, v in data.items() if isinstance(v, str) and v}
    except Exception:
        return {}


def set_model_overrides(overrides: dict[str, str]) -> None:
    from .db import set_app_state
    clean = {k: v for k, v in (overrides or {}).items() if isinstance(v, str) and v}
    set_app_state(_K_OVERRIDES, json.dumps(clean))


def clear_model_overrides() -> None:
    set_model_overrides({})


# --- role provenance (the "what is each role, and where did its model come from?" surface) ---
# Mongoose runs a different local model for different jobs. Each role starts on a sensible
# default; a Lane 2 benchmark override can win over that; and a value in the user's .env wins as
# their explicit choice. This makes that layering legible instead of a bare model name.
_ROLE_META = {
    "chat": ("Chat", "Everyday conversation and your assistant's voice. The default for "
             "anything not routed to another role, so it is the model you talk to most."),
    "tool": ("Tools and actions", "Chooses which tool to run and reasons through your request "
             "(routing, planning, automations). Inherits your Chat model unless you set one."),
    "code": ("Code", "Writes and edits code. Inherits your Chat model unless you set one."),
    "research": ("Research", "Longer multi-step research and synthesis. Inherits your Chat "
                 "model unless you set one."),
}
_ROLE_ENV_VAR = {
    "chat": "OLLAMA_CHAT_MODEL", "tool": "OLLAMA_TOOL_MODEL",
    "code": "OLLAMA_CODE_MODEL", "research": "OLLAMA_RESEARCH_MODEL",
}


def _last_apply_receipt() -> dict[str, Any]:
    from .db import get_app_state
    raw = get_app_state(_K_LAST_APPLY, "")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def model_roles_overview(installed: list[str] | set[str] | None = None) -> list[dict[str, Any]]:
    """Per-role, human-legible view for the cockpit: what the role is for, which model is
    actually in effect, and WHERE that model came from (a benchmark override, the user's own
    .env setting, an inherited Chat model, or the built-in default). Pass the installed model
    set to also flag a role pointed at a model that is not pulled (a real, silent misconfig)."""
    import os
    from .settings import get_settings
    s = get_settings()
    overrides = get_model_overrides()
    receipt = _last_apply_receipt()
    inst = set(installed) if installed is not None else None
    out: list[dict[str, Any]] = []
    for role, (label, purpose) in _ROLE_META.items():
        effective = s.model_for_role(role)
        env_set = bool(os.getenv(_ROLE_ENV_VAR[role]))
        if role in overrides:
            source, source_label = "benchmark", "Benchmark-tuned"
        elif env_set:
            source, source_label = "env", "Your setting"
        elif role in ("tool", "research"):
            source, source_label = "inherited", "Inherits Chat"
        else:
            source, source_label = "default", "Built-in default"
        item: dict[str, Any] = {
            "role": role, "label": label, "purpose": purpose, "model": effective,
            "source": source, "source_label": source_label,
            "env_var": _ROLE_ENV_VAR[role],
            "installed": (effective in inst) if inst is not None else None,
        }
        if source == "benchmark" and isinstance(receipt, dict) and receipt.get("at"):
            item["tuned_at"] = receipt.get("at")
        out.append(item)
    return out


# --- usage signal (the trigger) ----------------------------------------------

def usage_signal() -> dict[str, Any]:
    """Honest real-usage signal: how many user turns Mongoose has actually handled, across
    how many distinct days. This is what TRIGGERS a benchmark, not proof of anything itself."""
    from .db import connect
    turns = 0
    days = 0
    try:
        with connect() as con:
            turns = con.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'").fetchone()[0]
            days = con.execute(
                "SELECT COUNT(DISTINCT substr(created_at, 1, 10)) FROM messages WHERE role = 'user'"
            ).fetchone()[0]
    except Exception:
        pass
    enough = turns >= MIN_USAGE_TURNS and days >= MIN_ACTIVE_DAYS
    return {"turns": turns, "active_days": days, "enough_to_trigger": enough,
            "need_turns": MIN_USAGE_TURNS, "need_days": MIN_ACTIVE_DAYS}


# --- benchmark coverage (the proof) ------------------------------------------

def _benchmark_coverage() -> dict[str, Any]:
    """How much benchmark evidence exists: per-model scored-round counts. Lane 2 only vests
    when enough models have enough rounds, otherwise a 'recommendation' is noise."""
    try:
        from .benchmark import load_results_for_recommend
        results, cold = load_results_for_recommend()
    except Exception:
        return {"models": {}, "models_ready": 0, "enough": False, "results": [], "cold": {}}
    per_model: dict[str, int] = {}
    for r in results:
        rounds = r.get("rounds") or r.get("round_count")
        if rounds is None:
            # aggregated result without an explicit round count: infer from tasks presence.
            rounds = 1 if r.get("tasks") else 0
        per_model[r.get("model", "?")] = int(rounds)
    models_ready = sum(1 for n in per_model.values() if n >= MIN_ROUNDS_PER_MODEL)
    enough = models_ready >= MIN_MODELS_COMPARED
    return {"models": per_model, "models_ready": models_ready, "enough": enough,
            "need_rounds": MIN_ROUNDS_PER_MODEL, "need_models": MIN_MODELS_COMPARED,
            "results": results, "cold": cold}


# --- the vesting state machine ----------------------------------------------

def vesting_status() -> dict[str, Any]:
    """The single source of truth for where Lane 2 is. Never claims 'vested' unless BOTH the
    usage trigger and the benchmark-coverage proof are honestly met. Returns the stage plus
    exactly which gate is outstanding, so the UI can be truthful about why it's not ready."""
    usage = usage_signal()
    coverage = _benchmark_coverage()

    if not usage["enough_to_trigger"]:
        stage = "accruing"          # Lane 1 is enough; not enough real use yet to bother
        reason = (f"Using the starter models. After more real use "
                  f"({usage['turns']}/{usage['need_turns']} turns over "
                  f"{usage['active_days']}/{usage['need_days']} days) Mongoose will benchmark "
                  f"itself and suggest the best models for how you actually use it.")
    elif not coverage["enough"]:
        stage = "benchmarking"      # triggered, but not enough scored coverage to trust a call
        reason = (f"Benchmarking models on your real task mix "
                  f"({coverage['models_ready']}/{coverage['need_models']} models have enough "
                  f"rounds). The tuning suggestion unlocks once there's enough evidence.")
    else:
        stage = "vested"
        reason = "Enough real usage and benchmark evidence to recommend the best setup."

    return {
        "stage": stage,
        "vested": stage == "vested",
        "reason": reason,
        "usage": usage,
        "coverage": {k: coverage[k] for k in ("models", "models_ready", "enough",
                                              "need_rounds", "need_models")},
    }


# --- the recommendation (only meaningful once vested) ------------------------

def _signature(rec: dict[str, Any]) -> str:
    """A stable signature of a recommendation, so a dismissal sticks until the advice changes."""
    combo = rec.get("combo", {})
    return json.dumps({"verdict": rec.get("verdict"), "combo": combo}, sort_keys=True)


def _model_overall_score(results: list[dict[str, Any]], model: str) -> float | None:
    """The overall task pass-rate for a given model from the benchmark results, or None if it
    wasn't benchmarked. Used to compare the recommended model against the one in effect now."""
    for r in results:
        if r.get("model") == model:
            scored = [t for t in r.get("tasks", []) if not t.get("skipped")]
            if not scored:
                return None
            return sum(1 for t in scored if t.get("ok")) / len(scored)
    return None


def _is_large_and_safe(rec: dict[str, Any], results: list[dict[str, Any]] | None = None) -> bool:
    """Auto-apply guardrail: only switch on our own when the win over the CURRENTLY RUNNING
    setup is big and not a latency regression. A combo is never auto-applied (it changes
    runtime swap behaviour materially); everything below the bar is recommend-only.

    The gain is measured as the recommended single model's benchmark score MINUS the currently
    configured model's score on the same benchmark, not the recommender's combo-vs-single
    figure (which is 0 when one model wins every role, exactly the case where switching from a
    worse incumbent is most warranted)."""
    if not rec.get("ok"):
        return False
    if rec.get("verdict") == "combo":
        return False
    proposed_model = rec.get("best_single", {}).get("model")
    if not proposed_model:
        return False
    results = results if results is not None else _benchmark_coverage()["results"]
    proposed_score = _model_overall_score(results, proposed_model)
    if proposed_score is None:
        return False
    # the model currently in effect for chat (the resident default that matters most)
    try:
        from .settings import get_settings
        current_model = get_settings().model_for_role("chat")
    except Exception:
        return False
    if current_model == proposed_model:
        return False  # already running it, nothing to switch
    current_score = _model_overall_score(results, current_model)
    if current_score is None:
        # the incumbent wasn't even benchmarked; don't auto-switch on an unmeasured comparison
        return False
    gain = proposed_score - current_score
    if gain < AUTO_APPLY_MIN_QUALITY_GAIN:
        return False
    # "...and not a latency regression": the promise this guard always made but never checked.
    # Compare per-turn warm chat latency; a big quality win that types twice as slowly is a
    # recommend-only case, not an auto-apply.
    def _warm_latency(model_name: str) -> float | None:
        for r in results:
            if r.get("model") == model_name:
                v = r.get("warm_chat_median_s")           # single-round scalar
                if isinstance(v, (int, float)):
                    return float(v)
                stats = r.get("warm_chat")                # multi-round pooled stats dict
                if isinstance(stats, dict) and isinstance(stats.get("median"), (int, float)):
                    return float(stats["median"])
                return None
        return None
    proposed_lat = _warm_latency(proposed_model)
    current_lat = _warm_latency(current_model)
    if proposed_lat is None or current_lat is None:
        return False  # don't auto-switch on an unmeasured latency comparison
    return (proposed_lat - current_lat) <= AUTO_APPLY_MAX_LATENCY_REGRESSION_S


def evaluate(*, allow_auto_apply: bool = True) -> dict[str, Any]:
    """The Lane 2 evaluation. If vested, compute the recommendation; if the win is large and
    safe, optionally auto-apply (reversibly, with a receipt). Returns a render-ready dict for
    the cockpit card / phone nudge. Safe to call often; only acts when genuinely vested."""
    status = vesting_status()
    if not status["vested"]:
        return {"vested": False, **status}

    coverage = _benchmark_coverage()
    from .benchmark_recommend import recommend, task_kind_map
    rec = recommend(coverage["results"], task_kind_map(), cold_load_by_model=coverage["cold"])
    if not rec.get("ok"):
        return {"vested": True, "actionable": False, "reason": rec.get("reason", "no recommendation"),
                **status}

    sig = _signature(rec)
    from .db import get_app_state
    dismissed = get_app_state(_K_DISMISSED, "") == sig

    current = get_model_overrides()
    # does the recommendation actually differ from what's already in effect?
    proposed = rec.get("combo", {}) if rec.get("verdict") == "combo" else \
        {role: rec["best_single"]["model"] for role in rec.get("roles", [])}
    already_applied = (proposed == current) or (not current and _proposed_matches_base(proposed))

    large_and_safe = _is_large_and_safe(rec, coverage["results"])
    auto_applied = False
    if allow_auto_apply and not already_applied and large_and_safe:
        _apply_and_record(rec, proposed)
        auto_applied = True

    return {
        "vested": True,
        "actionable": not already_applied,
        "auto_applied": auto_applied,
        "dismissed": dismissed,
        "signature": sig,
        "verdict": rec.get("verdict"),
        "recommendation": rec,
        "proposed": proposed,
        "large_and_safe": large_and_safe,
        "reason": status["reason"],
    }


def models_to_benchmark() -> list[str]:
    """The set of models worth benchmarking now: the currently-configured models (so we keep a fresh
    baseline) plus a small set of candidate alternatives to compare against, restricted to models that
    are actually pulled (we don't auto-download in the scheduled run, that would be a big surprise
    use of disk/bandwidth). Returns a de-duplicated list. Empty if nothing comparable is installed,
    in which case the benchmark job records 'nothing to compare' rather than running pointlessly."""
    from .settings import get_settings
    s = get_settings()
    configured = [s.ollama_chat_model, s.ollama_tool_model, s.ollama_code_model, s.ollama_research_model]
    # A preference ORDER for alternatives worth comparing on a 24GB M4 mini, tried first when
    # installed. This is a hint, NOT a filter: whatever is actually pulled on THIS machine is
    # eligible, so the benchmark never goes silent just because the machine's models moved on
    # from a hardcoded list (the old list named models that were never installed here, so the
    # lane skipped every night with 'nothing to compare' despite 9 usable models present).
    preferred = [
        "gemma4:26b-a4b-it-q4_K_M", "qwen3:30b-a3b", "gpt-oss:20b", "qwen3:14b",
        "qwen2.5-coder:14b-instruct-q4_K_M", "qwen2.5:14b-instruct-q4_K_M",
        "llama3.1:8b-instruct-q4_K_M", "mistral-small:24b-instruct-2501-q4_K_M",
    ]
    # Cap the scheduled run so a machine full of models does not benchmark for hours. Cold
    # loading each large model is expensive; a handful is plenty to find a better setup.
    cap = 5
    try:
        import asyncio
        from .benchmark import installed_models
        have = set(asyncio.run(installed_models()))
    except Exception:
        # cannot list installed models: fall back to just the configured ones we were given
        wanted = []
        for m in configured:
            if m and m not in wanted:
                wanted.append(m)
        return wanted if len(wanted) >= 2 else []
    wanted: list[str] = []
    # 1. configured models that are actually installed (keep the live baseline fresh)
    for m in configured:
        if m and m in have and m not in wanted:
            wanted.append(m)
    # 2. preferred alternatives that are installed, in preference order
    for m in preferred:
        if m in have and m not in wanted:
            wanted.append(m)
    # 3. any other installed model, so the comparison reflects THIS machine, not a fixed list
    for m in sorted(have):
        if m not in wanted:
            wanted.append(m)
    wanted = wanted[:cap]
    # need at least 2 distinct models for a comparison to mean anything
    return wanted if len(wanted) >= 2 else []


def _proposed_matches_base(proposed: dict[str, str]) -> bool:
    """True if the proposed models already equal the configured base (so there's nothing to do)."""
    try:
        from .settings import get_settings
        s = get_settings()
        base = {"chat": s.ollama_chat_model, "tool": s.ollama_tool_model,
                "code": s.ollama_code_model, "research": s.ollama_research_model}
        return all(base.get(r) == m for r, m in proposed.items())
    except Exception:
        return False


def _apply_and_record(rec: dict[str, Any], proposed: dict[str, str]) -> None:
    """Apply an override (reversible) and write a receipt so it can be reverted and audited."""
    from .db import set_app_state
    set_model_overrides(proposed)
    receipt = {"applied": proposed, "verdict": rec.get("verdict"),
               "quality_gain": rec.get("quality_gain"), "signature": _signature(rec),
               "at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    set_app_state(_K_LAST_APPLY, json.dumps(receipt))


# --- user actions ------------------------------------------------------------

def apply_recommendation() -> dict[str, Any]:
    """Apply the current vested recommendation on the user's say-so (the 'one-tap apply' path
    for recommend-only verdicts)."""
    ev = evaluate(allow_auto_apply=False)
    if not ev.get("vested") or not ev.get("actionable"):
        return {"ok": False, "reason": "nothing to apply"}
    _apply_and_record(ev["recommendation"], ev["proposed"])
    return {"ok": True, "applied": ev["proposed"]}


def revert() -> dict[str, Any]:
    """Undo a Lane 2 switch, drop the override layer, returning to the configured base."""
    from .db import set_app_state
    had = get_model_overrides()
    clear_model_overrides()
    import datetime as _dt
    set_app_state(_K_LAST_APPLY, json.dumps(
        {"reverted_from": had, "at": _dt.datetime.now(_dt.timezone.utc).isoformat()}))
    return {"ok": True, "reverted_from": had}


def dismiss() -> dict[str, Any]:
    """Dismiss the current recommendation until the advice meaningfully changes."""
    ev = evaluate(allow_auto_apply=False)
    sig = ev.get("signature", "")
    from .db import set_app_state
    set_app_state(_K_DISMISSED, sig)
    return {"ok": True, "dismissed": sig}
