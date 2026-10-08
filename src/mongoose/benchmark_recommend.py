from __future__ import annotations

from typing import Any

# Benchmark recommender.
#
# The benchmark MEASURES (latency, cold-load, per-task pass/fail under real Mongoose tasks)
# but stops at a table you have to read and interpret yourself. This turns those results
# into an actual recommendation: for each role (chat / tool / ...), which model wins, and ,
# the decision that actually matters on a 24GB M4 , whether a per-role COMBO is worth it or
# whether a single resident model is the better real-world choice.
#
# The combo nuance is the whole point. On the base M4 the models are mutually exclusive in
# memory (one at a time), so a "best model per role" combo pays a COLD-LOAD penalty every
# time a turn switches roles (chat -> tool -> chat...). A combo only earns its keep if its
# quality gain over the best single model is big enough to justify that swap cost. This
# recommender makes that tradeoff explicit instead of hiding it.
#
# It decides nothing irreversibly: it prints a recommendation and the exact settings to
# apply it. The user stays in control.

# Map a benchmark task "kind" to a model role.
_KIND_TO_ROLE = {"chat": "chat", "tool": "tool", "code": "code", "research": "research"}

# Roles we report on, in priority order. chat is the resident default; tool is the
# reasoning/routing path; the others appear only if tasks/results cover them.
_ROLE_ORDER = ["chat", "tool", "code", "research"]


def _model_tag(r: dict[str, Any]) -> str:
    return r.get("model", "?")


def _scored_tasks(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-task results that were actually scored (not skipped)."""
    return [t for t in r.get("tasks", []) if not t.get("skipped")]


def _role_of_task(task_name: str, task_kind_by_name: dict[str, str]) -> str | None:
    kind = task_kind_by_name.get(task_name)
    return _KIND_TO_ROLE.get(kind) if kind else None


def _per_role_scores(result: dict[str, Any], task_kind_by_name: dict[str, str]
                     ) -> dict[str, dict[str, Any]]:
    """For one model, aggregate its scored tasks into per-role {passed, total, median_latency}."""
    buckets: dict[str, dict[str, Any]] = {}
    for t in _scored_tasks(result):
        role = _role_of_task(t.get("task", ""), task_kind_by_name)
        if role is None:
            continue
        b = buckets.setdefault(role, {"passed": 0, "total": 0, "latencies": []})
        b["total"] += 1
        if t.get("ok"):
            b["passed"] += 1
        lat = t.get("latency_median_s")
        if isinstance(lat, (int, float)):
            b["latencies"].append(lat)
    for b in buckets.values():
        lats = b.pop("latencies")
        b["median_latency_s"] = round(sorted(lats)[len(lats) // 2], 2) if lats else None
        b["pass_rate"] = b["passed"] / b["total"] if b["total"] else 0.0
    return buckets


def _better(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Is candidate `a` a better role winner than current best `b`? Quality (pass rate)
    first; on a tie, lower latency wins. (Both already restricted to the same role.)"""
    if a["pass_rate"] != b["pass_rate"]:
        return a["pass_rate"] > b["pass_rate"]
    la = a.get("median_latency_s")
    lb = b.get("median_latency_s")
    if la is None:
        return False
    if lb is None:
        return True
    return la < lb


def recommend(results: list[dict[str, Any]], task_kind_by_name: dict[str, str],
              *, cold_load_by_model: dict[str, float] | None = None) -> dict[str, Any]:
    """Turn a list of per-model benchmark results into a recommendation.

    Returns per-role winners, the best single resident model (the one with the highest
    TOTAL pass rate across all roles, i.e. the best all-rounder), and a single-vs-combo
    verdict that weighs the combo's quality gain against the M4 swap cost.
    """
    usable = [r for r in results if "error" not in r and r.get("tasks")]
    if not usable:
        return {"ok": False, "reason": "no usable benchmark results (all errored or empty)"}

    cold = cold_load_by_model or {}

    # 1) per-role winner
    roles_present: list[str] = []
    per_model_role_scores: dict[str, dict[str, dict[str, Any]]] = {}
    for r in usable:
        per_model_role_scores[_model_tag(r)] = _per_role_scores(r, task_kind_by_name)
    for role in _ROLE_ORDER:
        if any(role in s for s in per_model_role_scores.values()):
            roles_present.append(role)

    role_winners: dict[str, dict[str, Any]] = {}
    for role in roles_present:
        best_model = None
        best_score = None
        for model, scores in per_model_role_scores.items():
            if role not in scores:
                continue
            s = scores[role]
            if best_score is None or _better(s, best_score):
                best_score, best_model = s, model
        if best_model is not None:
            role_winners[role] = {"model": best_model, **best_score}

    # 2) best single all-rounder: highest overall pass rate across ALL scored tasks
    def _overall(r: dict[str, Any]) -> tuple[float, float]:
        scored = _scored_tasks(r)
        passed = sum(1 for t in scored if t.get("ok"))
        rate = passed / len(scored) if scored else 0.0
        lat = r.get("task_latency_median_s") or 999.0
        return rate, lat

    best_single = max(usable, key=lambda r: (_overall(r)[0], -_overall(r)[1]))
    best_single_tag = _model_tag(best_single)
    best_single_rate = _overall(best_single)[0]

    # 3) the combo: best model per role. Compute its average per-role pass rate.
    combo = {role: w["model"] for role, w in role_winners.items()}
    combo_is_uniform = len(set(combo.values())) <= 1

    # combo's quality = mean of its per-role winning pass rates
    combo_rate = (sum(w["pass_rate"] for w in role_winners.values()) / len(role_winners)
                  if role_winners else 0.0)
    # best single's quality on the SAME per-role basis, for a fair comparison
    single_role_scores = per_model_role_scores.get(best_single_tag, {})
    single_rate_per_role = (
        sum(single_role_scores[role]["pass_rate"] for role in role_winners
            if role in single_role_scores) / len(role_winners)
        if role_winners else 0.0)

    quality_gain = combo_rate - single_rate_per_role

    # swap cost signal: how expensive is a role switch on this machine? Use the max cold
    # load among the combo's distinct models as the per-switch penalty.
    distinct_models = set(combo.values())
    swap_penalty_s = max((cold.get(m, 0.0) for m in distinct_models), default=0.0)

    # Decision policy (conservative, explicit):
    #  - if the combo is actually a single model, that IS the recommendation (no swap cost).
    #  - else recommend the combo ONLY if its quality gain is meaningful (>= 10 percentage
    #    points) AND the swap penalty isn't punishing (< 8s), because on a one-model-at-a-time
    #    box a small quality gain isn't worth reloading a multi-GB model every role switch.
    if combo_is_uniform:
        verdict = "single"
        chosen = next(iter(distinct_models))
        rationale = (f"One model wins every role: {chosen}. Use it as the single resident "
                     "model, no model-swapping cost.")
    elif quality_gain >= 0.10 and swap_penalty_s < 8.0:
        verdict = "combo"
        rationale = (f"A per-role combo is worth it: +{quality_gain*100:.0f} percentage points "
                     f"of task quality over the best single model ({best_single_tag}), and the "
                     f"role-switch reload (~{swap_penalty_s:.0f}s) is tolerable on this machine.")
    elif quality_gain >= 0.10:
        verdict = "single"
        rationale = (f"The combo is higher quality (+{quality_gain*100:.0f} pts) BUT each role "
                     f"switch reloads a model (~{swap_penalty_s:.0f}s on this machine, models are "
                     f"mutually exclusive in 24GB). For a responsive assistant, prefer the single "
                     f"best all-rounder: {best_single_tag}. Switch to the combo only if you don't "
                     "mind the reload latency.")
    else:
        verdict = "single"
        rationale = (f"A per-role combo gains little (+{quality_gain*100:.0f} pts) and adds swap "
                     f"cost. The single best all-rounder {best_single_tag} is the better choice.")

    return {
        "ok": True,
        "roles": roles_present,
        "role_winners": role_winners,
        "best_single": {"model": best_single_tag, "overall_pass_rate": round(best_single_rate, 2)},
        "combo": combo,
        "combo_pass_rate": round(combo_rate, 2),
        "single_pass_rate_per_role": round(single_rate_per_role, 2),
        "quality_gain": round(quality_gain, 2),
        "swap_penalty_s": round(swap_penalty_s, 1),
        "verdict": verdict,
        "rationale": rationale,
    }


def _settings_block(rec: dict[str, Any]) -> list[str]:
    """The exact env/settings to APPLY the recommendation. The user runs nothing
    automatically; this is copy-paste."""
    out: list[str] = []
    if rec["verdict"] == "single":
        # one model for everything (or the best all-rounder)
        model = (next(iter(set(rec["combo"].values())))
                 if len(set(rec["combo"].values())) == 1 else rec["best_single"]["model"])
        out.append("To use the single recommended model for every role, set:")
        out.append(f"  MONGOOSE_CHAT_MODEL={model}")
        out.append(f"  MONGOOSE_TOOL_MODEL={model}")
        out.append(f"  MONGOOSE_CODE_MODEL={model}")
    else:
        out.append("To use the per-role combo, set:")
        role_env = {"chat": "MONGOOSE_CHAT_MODEL", "tool": "MONGOOSE_TOOL_MODEL",
                    "code": "MONGOOSE_CODE_MODEL", "research": "MONGOOSE_RESEARCH_MODEL"}
        for role, model in rec["combo"].items():
            env = role_env.get(role)
            if env:
                out.append(f"  {env}={model}")
    return out


def format_recommendation(rec: dict[str, Any]) -> str:
    if not rec.get("ok"):
        return f"No recommendation: {rec.get('reason', 'unknown')}"
    out: list[str] = ["Model recommendation (from your benchmark on this machine):", ""]
    out.append("Best model per role:")
    for role in rec["roles"]:
        w = rec["role_winners"].get(role)
        if w:
            lat = f", ~{w['median_latency_s']}s" if w.get("median_latency_s") is not None else ""
            out.append(f"  {role:9} -> {w['model']}  ({w['passed']}/{w['total']} tasks{lat})")
    out.append("")
    out.append(f"Best single all-rounder: {rec['best_single']['model']} "
               f"({rec['best_single']['overall_pass_rate']*100:.0f}% of all tasks)")
    out.append("")
    verdict_label = "USE A PER-ROLE COMBO" if rec["verdict"] == "combo" else "USE A SINGLE MODEL"
    out.append(f"Recommendation: {verdict_label}")
    out.append(f"  {rec['rationale']}")
    out.append("")
    out.extend(_settings_block(rec))
    return "\n".join(out)


def task_kind_map() -> dict[str, str]:
    """Build {task_name: kind} from the benchmark's own task definitions, so the recommender
    can join per-task results back to roles without duplicating the task list."""
    from .benchmark import _tasks
    return {t["name"]: t.get("kind", "") for t in _tasks()}
