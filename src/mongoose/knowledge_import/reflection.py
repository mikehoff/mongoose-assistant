from __future__ import annotations

import json
from collections import Counter
from typing import Any

from ..db import connect, utc_now_iso
from .schemas import make_id, site_terms
from .store import dashboard, ensure_tables, insert_reflection_run, log_activation


def _json(value: str | None, default):
    try:
        return json.loads(value or "")
    except Exception:
        return default


def run_reflection_cycle(kind: str = "daily", *, queue_research: bool = True,
                         activate_low_risk: bool = False, limit: int = 20) -> dict[str, Any]:
    """Connect the dots across imported claims without hallucinating action.

    Produces a persisted reflection run. It may queue research requests for high-risk insights, but it
    does not run web research unless a caller explicitly invokes the research broker. It may mark low-risk
    insights as accepted only when activate_low_risk=True; default is candidate-only.
    """
    ensure_tables()
    kind = (kind or "daily").strip().lower()[:40] or "daily"
    with connect() as con:
        claims = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_claims ORDER BY confidence DESC, updated_at DESC LIMIT 500"
        ).fetchall()]
        insights = [dict(r) for r in con.execute(
            "SELECT * FROM insight_candidates ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()]
        nudges = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_nudge_candidates ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()]
        feedback = [dict(r) for r in con.execute(
            "SELECT * FROM insight_feedback ORDER BY created_at DESC LIMIT 200"
        ).fetchall()]
    domain_counts = Counter(_domain_for_predicate(c.get("predicate", "")) for c in claims)
    sensitive_count = sum(1 for c in claims if c.get("sensitivity") in {"high", "Sensitivity.high", "secret", "Sensitivity.secret"})
    body = _body_patterns(claims)
    posture = _posture_patterns(claims, nudges)
    review_pressure = _review_pressure()
    research_queued = []
    if queue_research:
        research_queued = queue_research_for_pending(limit=limit)
    activated = []
    if activate_low_risk:
        activated = _activate_low_risk_insights(insights, limit=limit)
    summary = {
        "kind": kind,
        "domain_counts": dict(domain_counts),
        "sensitive_claims": sensitive_count,
        "body_patterns": body,
        "posture_patterns": posture,
        "review_pressure": review_pressure,
        "research_queued": research_queued,
        "low_risk_activated": activated,
        "feedback_summary": dict(Counter(f.get("feedback") for f in feedback if f.get("feedback"))),
        "dashboard_counts": dashboard(limit=3).get("counts", {}),
    }
    run_id = make_id("reflect", kind, utc_now_iso())
    insert_reflection_run(run_id, kind, "ok", summary)
    log_activation("reflection", run_id, "run", reason=f"{kind} knowledge reflection", context=summary)
    return {"ok": True, "run_id": run_id, **summary}


def _domain_for_predicate(pred: str) -> str:
    if pred in {"has_body_signal", "has_posture_pattern", "uses_activity", "uses_wellness_routine", "has_health_context"}:
        return "body_wellness"
    if pred in {"has_legal_or_rtm_context", "has_financial_or_accounting_rule"}:
        return "rtm"
    if pred in {"has_project_requirement", "has_architecture_decision", "reported_bug", "has_fix_or_patch"}:
        return "project"
    return "general"


# Cue words for reflection (each replaceable by the site profile, lexicon knowledge.<name>; see
# schemas.site_terms). _RECOVERY_TERMS shares compilers' knowledge.recovery_terms.
_BODY_PATTERN_TERMS = ("pizza", "bread", "dairy", "sweets", "constipation", "bloat", "puffiness", "swim", "yoga")
_POSTURE_PATTERN_TERMS = ("posture", "desk", "physio", "breathing", "reset", "compression")
_RECOVERY_TERMS = ("swim", "yoga")
_ROUTINE_ACTION_TERMS = ("swim", "yoga", "walk", "breath", "breathing", "reset", "posture", "physio", "desk", "compression")
_FOOD_CUE_TERMS = ("bloat", "puffy", "puffiness", "constipation", "stomach", "pizza", "dairy", "sweets", "trigger", "avoid")


def _body_patterns(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    terms = site_terms("reflection.body_pattern_terms", _BODY_PATTERN_TERMS)
    recovery = site_terms("recovery_terms", _RECOVERY_TERMS)
    for c in claims:
        if c.get("predicate") not in {"has_body_signal", "has_health_context", "uses_wellness_routine"}:
            continue
        blob = f"{c.get('object','')} {c.get('summary','')} {c.get('details_json','')}".lower()
        hits = [t for t in terms if t in blob]
        if not hits:
            continue
        out.append({
            "claim_id": c.get("claim_id"),
            "summary": c.get("summary"),
            "tags": hits,
            "confidence": c.get("confidence"),
            "care_hint": _care_hint_for_terms(hits, recovery),
        })
    return out[:12]


def _posture_patterns(claims: list[dict[str, Any]], nudges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    posture_terms = site_terms("reflection.posture_terms", _POSTURE_PATTERN_TERMS)
    for c in claims:
        if c.get("predicate") not in {"has_posture_pattern", "uses_activity", "uses_wellness_routine"}:
            continue
        blob = f"{c.get('object','')} {c.get('summary','')} {c.get('details_json','')}".lower()
        if not any(t in blob for t in posture_terms):
            continue
        out.append({
            "claim_id": c.get("claim_id"),
            "summary": c.get("summary"),
            "nudge_ready": any(n.get("source_claim_id") == c.get("claim_id") for n in nudges),
            "care_hint": "Use as a tiny contextual body cue, not a moralising wellness instruction.",
        })
    return out[:12]


def _care_hint_for_terms(hits: list[str], recovery: tuple[str, ...] | None = None) -> str:
    if any(t in hits for t in ("pizza", "bread", "dairy", "sweets")):
        return "Personal trigger context: mention only when relevant to food planning, events or symptoms."
    if any(t in hits for t in (recovery if recovery is not None else site_terms("recovery_terms", _RECOVERY_TERMS))):
        return "Potential low-friction recovery action: suggest contextually, not as a rule."
    return "Keep as personal body context and avoid generic medical claims."


def _review_pressure() -> dict[str, int]:
    with connect() as con:
        rows = con.execute("SELECT severity, COUNT(*) c FROM knowledge_review_queue WHERE status='open' GROUP BY severity").fetchall()
    return {r["severity"]: int(r["c"]) for r in rows}


def queue_research_for_pending(limit: int = 10) -> list[dict[str, Any]]:
    from .research_broker import request_for_stored_insight, persist_research_request
    ensure_tables()
    queued = []
    with connect() as con:
        rows = con.execute(
            """
            SELECT * FROM insight_candidates
            WHERE research_status IN ('needed','failed') OR risk_level='high'
            ORDER BY CASE risk_level WHEN 'high' THEN 0 ELSE 1 END, updated_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 50)),),
        ).fetchall()
    for row in rows:
        req = request_for_stored_insight(dict(row))
        if not req:
            continue
        queued.append(persist_research_request(req, insight_id=row["insight_id"]))
    return queued


def _care_activation_reason(row: dict[str, Any]) -> str:
    """Return an activation reason for genuinely useful low-risk care, else ''.

    Automatic activation should feel delightful, not like every imported fact has become a nag.
    So require an actual helpful action pattern, not merely a baseline/profile/tolerance memory.
    """
    blob = f"{row.get('title','')} {row.get('hypothesis','')} {row.get('suggested_action_json','')}".lower()
    dull_terms = ("baseline", "profile", "relevant to this episode", "referenced", "already do", "existing physio", "is fine", "tolerated", "context only")
    if any(t in blob for t in dull_terms):
        # A profile fact can still be searchable and useful in chat, but should not auto-prompt.
        return ""
    if any(t in blob for t in site_terms("reflection.routine_terms", _ROUTINE_ACTION_TERMS)):
        return "low-risk body routine/action pattern"
    if any(t in blob for t in site_terms("reflection.food_cue_terms", _FOOD_CUE_TERMS)):
        return "low-risk contextual body-state/food-planning cue"
    if any(t in blob for t in ("journal", "check-in", "grounding", "friend", "message")):
        return "low-risk supportive ritual"
    return ""


def _activate_low_risk_insights(insights: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    activated = []
    from .care_planner import activate_care_plan
    for row in insights:
        if len(activated) >= limit:
            break
        if row.get("risk_level") != "low" or row.get("actionability") != "high":
            continue
        if row.get("research_status") not in {"not_needed", "completed"}:
            continue
        reason = _care_activation_reason(row)
        if not reason:
            continue
        res = activate_care_plan(row["insight_id"], activation_level="gentle", cadence="contextual", actor="reflection")
        if res.get("ok"):
            activated.append({"insight_id": row["insight_id"], "title": row.get("title"), "reason": reason, "plan_id": (res.get("plan") or {}).get("plan_id")})
            log_activation("insight_candidate", row["insight_id"], "auto_activate_low_risk_care", reason=reason)
    return activated
