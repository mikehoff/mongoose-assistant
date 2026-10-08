from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from ..db import connect, utc_now_iso
from .schemas import make_id
from .store import (
    ensure_tables,
    list_care_plans,
    log_activation,
    record_care_prompt,
    update_care_plan_status,
    upsert_care_plan,
)

_CONTEXT_TERMS = {
    "event": ("date", "event", "meeting", "social", "party", "trip", "travel"),
    "food_body": ("bloat", "puffy", "constipation", "stomach", "food", "pizza", "bread", "dairy"),
    "posture": ("desk", "coding", "posture", "back", "neck", "hips", "gym"),
    "low_mood": ("lonely", "low", "heavy", "empty", "tense", "stressed"),
}



def _context_terms() -> dict[str, tuple[str, ...]]:
    """Context tag -> cue words: the generic table above, or the site profile's own (lexicon
    knowledge.care_context_terms, same shape)."""
    from .. import profile as _profile
    own = _profile.lexicon("knowledge.care_context_terms", None)
    if own is None:
        return _CONTEXT_TERMS
    return {k: tuple(v) for k, v in own.items()}


_INVITING_OPENERS = (
    "Tiny care thought",
    "By the way",
    "Small Mongoose nudge",
    "Worth a gentle check",
)


def _text_blob(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    return json.dumps(context, ensure_ascii=False, default=str).lower()


def _feedback_counts(insight_id: str | None = None) -> Counter[str]:
    ensure_tables()
    q = "SELECT feedback FROM insight_feedback"
    params: tuple[Any, ...] = ()
    if insight_id:
        q += " WHERE insight_id=?"
        params = (insight_id,)
    with connect() as con:
        rows = con.execute(q, params).fetchall()
    return Counter(r["feedback"] for r in rows if r["feedback"])


def _safe_json(value: str | None, default):
    try:
        parsed = json.loads(value or "")
        return parsed if parsed is not None else default
    except Exception:
        return default




def _supporting_claim_quality(row: dict[str, Any]) -> dict[str, Any]:
    ids = _safe_json(row.get("source_claim_ids_json"), [])
    if not ids:
        return {"has_user_source": False, "assistant_only": False, "needs_review": False}
    marks = ",".join("?" for _ in ids)
    with connect() as con:
        claims = [dict(r) for r in con.execute(
            f"SELECT claim_id, source_basis, status, sensitivity FROM knowledge_claims WHERE claim_id IN ({marks})", ids
        ).fetchall()]
    user_sources = {"user_stated", "SourceBasis.user_stated", "user_corrected", "SourceBasis.user_corrected", "user_accepted", "SourceBasis.user_accepted"}
    assistant_sources = {"assistant_suggested", "SourceBasis.assistant_suggested", "inferred", "SourceBasis.inferred", "mixed", "SourceBasis.mixed"}
    has_user = any(str(c.get("source_basis")) in user_sources for c in claims)
    assistant_only = bool(claims) and all(str(c.get("source_basis")) in assistant_sources for c in claims)
    needs_review = any(str(c.get("status")) in {"review", "ImportStatus.review"} for c in claims)
    return {"has_user_source": has_user, "assistant_only": assistant_only, "needs_review": needs_review}

def _context_tags(context: dict[str, Any] | None,
                  context_terms: dict[str, tuple[str, ...]] | None = None) -> set[str]:
    blob = _text_blob(context)
    terms_by_tag = context_terms if context_terms is not None else _context_terms()
    return {name for name, terms in terms_by_tag.items() if any(t in blob for t in terms)}


def _tone_from_domain(domain: str, actionability: str) -> str:
    if domain == "body_wellness":
        return "warm_precise_body"
    if domain == "rtm":
        return "calm_project_guardrail"
    if actionability == "high":
        return "lightly_enthusiastic"
    return "warm_precise"


def _moment_hook(matched: list[str] | None) -> str:
    """A short hook so a nudge feels like it belongs to THIS moment rather than a played-back memory
    card. Optional and brief so the voice never over-explains."""
    import datetime as _dt
    tags = set(matched or [])
    if "event" in tags:
        return "before whatever is on today"
    if "posture" in tags:
        return "while you are at the desk"
    if "food_body" in tags:
        return "for how you might feel later"
    if "low_mood" in tags:
        return "if today feels a bit heavy"
    h = _dt.datetime.now().hour
    if h < 11:
        return "to start the day kindly"
    if h >= 20:
        return "as things wind down"
    return ""


# Several phrasings per action type so the same pattern never reads identically twice, and the owner's
# actual fact is woven into a warm sentence rather than announced after a label.
_CARE_VOICE = {
    "posture_reset": (
        "No workout needed {hook}, just a small unspooling: {fact}.",
        "A gentle posture cue {hook}: {fact}.",
        "Worth a soft reset {hook}, nothing heavy: {fact}.",
    ),
    "body_state_food_cue": (
        "Not a rule, just a light heads-up {hook}: {fact}.",
        "Something to hold loosely {hook}: {fact}.",
        "A small body-state note you can take or leave: {fact}.",
    ),
    "contextual_care": (
        "If you are feeling a bit compressed, one of your kinder levers: {fact}.",
        "A gentle option when you have a low-friction window: {fact}.",
        "Worth a soft check-in with yourself {hook}: {fact}.",
    ),
}


def _clean_fact(row: dict[str, Any], action: dict[str, Any]) -> str:
    """The owner's actual pattern, cleaned and kept crisp so a nudge stays a nudge, not a paragraph."""
    fact = (row.get("hypothesis") or "").strip()
    if not fact:
        ex = str(action.get("example") or action.get("message") or action.get("suggested_message") or "")
        fact = ex.split(": ")[-1].strip() if ": " in ex else ex.strip()
    fact = re.sub(r"\s+", " ", fact).strip()
    fact = re.sub(r"^(the owner|owner|user|you)\b[:,]?\s*", "", fact, flags=re.I).strip()
    # First clause only, then a word cap, so the voice reads like a friend and not a memory dump.
    for sep in ("; ", ". "):
        if sep in fact:
            fact = fact.split(sep)[0].strip()
            break
    words = fact.split()
    if len(words) > 22:
        fact = " ".join(words[:22]).rstrip(",;:") + "..."
    return fact.rstrip(".")


def _engaging_message(row: dict[str, Any], action: dict[str, Any], *, tone: str = "warm_precise",
                      matched: list[str] | None = None, opener_index: int = 0) -> str:
    """Compose a warm, specific, non-preachy care nudge in Mongoose's voice.

    The point is that this should feel like a friend noticing something at the right moment, not a
    formatted memory card. Deterministic and always available; an optional LLM pass (refine_care_voice)
    can warm it further when a model is present."""
    fact = _clean_fact(row, action)
    if not fact:
        return (f"By the way, there is an imported pattern about {row.get('title') or 'you'} that "
                "might be useful right now.")
    atype = str(action.get("type") or "contextual_care")
    templates = _CARE_VOICE.get(atype, _CARE_VOICE["contextual_care"])
    hook = _moment_hook(matched)
    msg = templates[opener_index % len(templates)].format(fact=fact, hook=hook)
    # Tidy artefacts when the hook was empty.
    msg = re.sub(r"\s+,", ",", msg)
    msg = re.sub(r"\s+:", ":", msg)
    msg = re.sub(r"\s{2,}", " ", msg).replace(" .", ".").strip()
    if len(msg) > 260:
        msg = msg[:257].rstrip() + "..."
    if tone == "lightly_enthusiastic" and opener_index % 3 == 0:
        msg = msg.rstrip(".") + ", worth a try."
    opener = _INVITING_OPENERS[opener_index % len(_INVITING_OPENERS)]
    msg = f"{opener}, {msg[:1].lower()}{msg[1:]}"
    return msg[:1].upper() + msg[1:]


async def refine_care_voice(text: str, *, tone: str = "warm_precise",
                            context: list[str] | None = None) -> str:
    """Best-effort LLM warming of a single care nudge, in Mongoose's voice.

    Falls back to the deterministic text on any failure or when no model / the flag is off. Strictly a
    gentle cue: the prompt forbids medical, dosing or supplement content and any command tone, and the
    result is discarded (falling back) if it drifts that way or runs long. Only low/medium-risk,
    non-sensitive care candidates ever reach this point, so this only makes safe text warmer."""
    base = (text or "").strip()
    if not base:
        return base
    try:
        from ..settings import get_settings
        if not getattr(get_settings(), "care_voice_llm", True):
            return base
    except Exception:
        pass
    ctx = ", ".join(sorted(context)) if context else ""
    prompt = ("Rewrite this gentle care nudge as ONE warm, natural sentence in your own voice, at most "
              "30 words. Keep it kind and specific, never preachy, never a command. Do NOT add any "
              "medical, dosing or supplement advice. "
              f"Tone: {tone}. {('Current context: ' + ctx + '. ') if ctx else ''}Nudge: {base}")
    try:
        from ..llm import ask_persona
        out = (await ask_persona(prompt, role="chat") or "").strip()
    except Exception:
        return base
    out = re.sub(r"\s+", " ", out).strip().strip('"').strip("'")
    if not out or len(out) > 300:
        return base
    low = out.lower()
    if any(t in low for t in (" mg", "dose", "dosage", "milligram", "supplement", "prescrib", "take a ")):
        return base
    return out


def rank_care_candidates(context: dict[str, Any] | None = None, *, limit: int = 5,
                         include_inactive: bool = False) -> list[dict[str, Any]]:
    """Rank imported insight candidates for the current moment.

    Active care plans get an extra boost, but unactivated low-risk candidates are still visible in
    Cockpit as suggestions to turn on. Sensitive or research-needed material remains suppressed.
    """
    ensure_tables()
    context_terms = _context_terms()
    tags = _context_tags(context, context_terms)
    all_feedback = _feedback_counts()
    active_plans = {p["insight_id"]: p for p in list_care_plans(status="active", limit=200)}
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            """
            SELECT * FROM insight_candidates
            WHERE status IN ('candidate','accepted')
              AND risk_level IN ('low','medium')
              AND research_status IN ('not_needed','completed')
            ORDER BY confidence DESC, updated_at DESC
            LIMIT 200
            """
        ).fetchall()]
    ranked: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        care = _safe_json(row.get("care_policy_json"), {})
        action = _safe_json(row.get("suggested_action_json"), {})
        quality = _supporting_claim_quality(row)
        if care.get("nudge_mode") == "ask_first" or care.get("research_required"):
            continue
        if quality.get("assistant_only") and row.get("status") != "accepted":
            # Keep assistant-suggested health/body ideas searchable and reviewable, not proactively caring.
            continue
        if quality.get("needs_review") and row.get("status") != "accepted":
            continue
        plan = active_plans.get(row.get("insight_id"))
        if not include_inactive and not plan and row.get("status") != "accepted":
            # Surpriser stays conservative. Cockpit can request inactive candidates.
            continue
        hay = f"{row.get('title','')} {row.get('hypothesis','')} {action}".lower()
        matched = sorted(tag for tag in tags if any(t in hay for t in context_terms[tag]))
        local_feedback = _feedback_counts(str(row.get("insight_id") or ""))
        score = 0.32
        if plan:
            score += 0.22
        if row.get("actionability") == "high":
            score += 0.18
        try:
            score += min(float(row.get("confidence") or 0), 1.0) * 0.17
        except Exception:
            pass
        if matched:
            score += 0.16
        if any(t in hay for t in ("hurt", "constipation", "bloat", "puffy", "trigger", "causes", "worsens", "avoid")):
            score += 0.08
        if any(t in hay for t in ("fine in small", "tolerated", "is fine", "okay in small")):
            score -= 0.10
        if local_feedback.get("helpful") or local_feedback.get("more_like_this"):
            score += 0.08
        if all_feedback.get("too_much") or local_feedback.get("too_much") or local_feedback.get("less_like_this"):
            score -= 0.08
        if row.get("risk_level") == "medium":
            score -= 0.05
        if quality.get("has_user_source"):
            score += 0.04
        activation_state = "active" if plan else ("accepted" if row.get("status") == "accepted" else "candidate")
        tone = _tone_from_domain(row.get("domain") or "", row.get("actionability") or "")
        text = _engaging_message(row, action, tone=tone, matched=matched, opener_index=idx)
        why = care.get("explanation_template") or "Imported owner knowledge connected to this moment."
        ranked.append({
            "kind": "knowledge_care",
            "insight_id": row.get("insight_id"),
            "plan_id": plan.get("plan_id") if plan else None,
            "title": row.get("title"),
            "text": text,
            "why": str(why)[:360],
            "score": round(max(0.0, min(score, 1.0)), 3),
            "domain": row.get("domain"),
            "matched_context": matched,
            "risk_level": row.get("risk_level"),
            "actionability": row.get("actionability"),
            "activation_state": activation_state,
            "tone": (plan or {}).get("tone") or _tone_from_domain(str(row.get("domain") or ""), str(row.get("actionability") or "")),
            "signature": make_id("surprise", "knowledge_care", row.get("insight_id")),
            "family": f"knowledge_care:{row.get('domain') or 'general'}",
            "imminent": bool(plan and matched),
        })
    ranked.sort(key=lambda r: (-float(r["score"]), str(r.get("title") or "")))
    return ranked[:max(1, min(limit, 50))]


def explain_care_candidate(insight_id: str) -> dict[str, Any]:
    ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM insight_candidates WHERE insight_id=?", (insight_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown insight"}
        ids = json.loads(row["source_claim_ids_json"] or "[]")
        claims = [dict(r) for r in con.execute(
            f"SELECT * FROM knowledge_claims WHERE claim_id IN ({','.join('?' for _ in ids)})", ids
        ).fetchall()] if ids else []
        plan = con.execute("SELECT * FROM knowledge_care_plans WHERE insight_id=? ORDER BY updated_at DESC LIMIT 1", (insight_id,)).fetchone()
    return {
        "ok": True,
        "insight": dict(row),
        "supporting_claims": claims,
        "care_plan": dict(plan) if plan else None,
        "care_note": "This is a care move, not a command. It is suppressed if sensitive, research-needed, stale or repeatedly disliked.",
    }


def activate_care_plan(insight_id: str, *, activation_level: str = "gentle",
                       cadence: str = "contextual", actor: str = "owner") -> dict[str, Any]:
    """Turn a low-risk insight candidate into an active, bounded care plan."""
    ensure_tables()
    activation_level = (activation_level or "gentle").strip().lower()
    if activation_level not in {"gentle", "active", "quiet"}:
        return {"ok": False, "error": "activation_level must be gentle/active/quiet"}
    with connect() as con:
        row = con.execute("SELECT * FROM insight_candidates WHERE insight_id=?", (insight_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown insight"}
        d = dict(row)
        if d.get("risk_level") == "high" or d.get("research_status") not in {"not_needed", "completed"}:
            return {"ok": False, "error": "cannot activate insight until high-risk/research-needed material is reviewed"}
        if d.get("risk_level") == "medium" and activation_level == "active":
            return {"ok": False, "error": "medium-risk insights can only be gentle or quiet"}
        con.execute("UPDATE insight_candidates SET status='accepted', updated_at=? WHERE insight_id=?", (utc_now_iso(), insight_id))
        action = _safe_json(d.get("suggested_action_json"), {})
        message = _engaging_message(d, action)
        tone = _tone_from_domain(str(d.get("domain") or ""), str(d.get("actionability") or ""))
    plan = upsert_care_plan(
        insight_id=insight_id,
        domain=str(d.get("domain") or "general"),
        title=str(d.get("title") or "Care pattern"),
        tone=tone,
        cadence=cadence,
        activation_level=activation_level,
        status="active",
        message_template=message,
    )
    log_activation("insight_candidate", insight_id, "activate_care", actor=actor,
                   reason=f"activated as {activation_level} care plan",
                   context={"plan_id": plan.get("plan_id"), "cadence": cadence})
    return {"ok": True, "plan": plan, "message": message}


def pause_care_plan(plan_id: str, *, actor: str = "owner") -> dict[str, Any]:
    return update_care_plan_status(plan_id, "paused", actor=actor, reason="owner paused care plan")


def next_care_prompt(context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return one active, engaging care prompt for the Surpriser or cockpit preview."""
    candidates = rank_care_candidates(context or {}, limit=10, include_inactive=False)
    active = [c for c in candidates if c.get("activation_state") == "active"]
    if not active:
        return {"ok": True, "prompt": None, "reason": "no active care plan matched safely"}
    chosen = active[0]
    if chosen.get("plan_id"):
        record_care_prompt(chosen["plan_id"])
    return {"ok": True, "prompt": chosen}
