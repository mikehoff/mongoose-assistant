from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from ..db import connect, utc_now_iso
from .schemas import (CarePolicy, ImportStatus, InsightCandidate, NudgeMode, ReviewItem, Sensitivity, make_id,
                      site_terms, site_text)
from .store import ensure_tables, insert_insight, insert_review, insert_synthesis_card, log_activation

_FOOD_TERMS = ("pizza", "bread", "crusty", "dairy", "sweet", "sugar", "gluten", "food")
_BODY_TERMS = ("bloat", "puffy", "puffiness", "constipation", "stomach", "gut", "pain", "face", "swollen")
_HELP_TERMS = ("swim", "yoga", "walk", "breathing", "physio", "reset", "sleep", "hydration")
_POSTURE_TERMS = ("posture", "right side", "rib", "pelvis", "compression", "neck", "back", "hip", "desk", "physio")
_SENSITIVE_TERMS = ("aas", "steroid", "dose", "dosage", "cycle", "medication", "supplement")
# The help and sensitive lists above, and the posture-reset example below, are replaceable by the site
# profile (lexicons knowledge.synthesis.help_terms, knowledge.synthesis.sensitive_terms and
# knowledge.synthesis.posture_reset_example).
_POSTURE_RESET_EXAMPLE = ("Worth a gentle check: this might be a good moment for a 90-second posture reset "
                          "rather than pushing through the desk block.")


def _json(value: str | None, default):
    try:
        parsed = json.loads(value or "")
        return parsed if parsed is not None else default
    except Exception:
        return default


def _blob(row: dict[str, Any]) -> str:
    return f"{row.get('object','')} {row.get('summary','')} {row.get('details_json','')} {row.get('tags_json','')}".lower()


def _source_is_user(row: dict[str, Any]) -> bool:
    return str(row.get("source_basis") or "") in {
        "user_stated", "SourceBasis.user_stated", "user_corrected", "SourceBasis.user_corrected", "user_accepted", "SourceBasis.user_accepted",
    }


def _health_sensitive(rows: list[dict[str, Any]]) -> bool:
    sensitive = site_terms("synthesis.sensitive_terms", _SENSITIVE_TERMS)
    return any(str(r.get("sensitivity")) in {"high", "Sensitivity.high", "secret", "Sensitivity.secret"} and any(t in _blob(r) for t in sensitive) for r in rows)


def run_synthesis_cycle(*, use_llm: bool | None = None, limit: int = 50) -> dict[str, Any]:
    """Create multi-claim synthesis cards and insight candidates.

    This is the connecting-dots layer. It keeps sensitive health clusters as research/review material,
    but can create active-care candidates for low-risk user-stated body/posture patterns.
    """
    ensure_tables()
    with connect() as con:
        claims = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_claims ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (max(100, min(int(limit) * 12, 1500)),),
        ).fetchall()]
        entities = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_entities ORDER BY confidence DESC, updated_at DESC LIMIT 500"
        ).fetchall()]
    cards = []
    cards.extend(_food_body_cards(claims))
    cards.extend(_posture_cards(claims))
    cards.extend(_persona_care_cards(claims))
    cards.extend(_rtm_project_cards(claims))
    if use_llm is None:
        use_llm = os.getenv("MONGOOSE_KNOWLEDGE_LLM_SYNTHESIS", "").lower() in {"1", "true", "yes"}
    llm_cards = []
    if use_llm:
        llm_cards = _run_llm_synthesis(claims[:80], entities[:80], limit=max(3, min(12, limit)))
        cards.extend(llm_cards)
    inserted = []
    insight_count = 0
    seen = set()
    for card in cards:
        if len(inserted) >= limit:
            break
        key = (card["domain"], card["title"].lower(), tuple(sorted(card["claim_ids"][:8])))
        if key in seen or not card["claim_ids"]:
            continue
        seen.add(key)
        sid = make_id("synthesis", card["domain"], card["title"], *card["claim_ids"][:8])
        insert_synthesis_card(
            synthesis_id=sid,
            domain=card["domain"],
            title=card["title"],
            summary=card["summary"],
            confidence=card["confidence"],
            actionability=card["actionability"],
            risk_level=card["risk_level"],
            connected_claim_ids=card["claim_ids"],
            connected_entity_ids=card.get("entity_ids", []),
            care_angle=card.get("care_angle", ""),
            suggested_action=card.get("suggested_action", {}),
            evidence=card.get("evidence", {}),
            status=card.get("status", "candidate"),
        )
        inserted.append({"synthesis_id": sid, **card})
        # Promote synthesis cards into normal insight candidates so care planner and search can use them.
        insight = InsightCandidate(
            insight_id=make_id("insight", "synthesis", sid),
            domain=card["domain"],
            title=card["title"],
            hypothesis=card["summary"],
            supporting_claim_ids=card["claim_ids"],
            confidence=card["confidence"],
            actionability=card["actionability"],
            risk_level=card["risk_level"],
            research_status="needed" if card["risk_level"] == "high" or card.get("research_required") else "not_needed",
            suggested_action=card.get("suggested_action", {}),
            care_policy=CarePolicy(
                nudge_mode=NudgeMode.ask_first if card["risk_level"] == "high" else NudgeMode.contextual_only,
                maximum_frequency="contextual only",
                interruptibility="low",
                sensitivity=Sensitivity.high if card["risk_level"] == "high" else Sensitivity.medium,
                needs_consent_before_action=True,
                research_required=bool(card.get("research_required") or card["risk_level"] == "high"),
                explanation_template=card.get("care_angle") or "This connects multiple imported records with provenance.",
            ),
            status=ImportStatus.candidate,
        )
        insert_insight(insight)
        if card["risk_level"] == "high" or card.get("research_required"):
            insert_review(ReviewItem(
                review_id=make_id("review", insight.insight_id, "synthesis"),
                object_type="insight_candidate",
                object_id=insight.insight_id,
                reason="Synthesised insight needs review or research before it becomes active care.",
                severity="high" if card["risk_level"] == "high" else "medium",
            ))
        insight_count += 1
    log_activation("synthesis", "knowledge", "run", reason="knowledge synthesis cycle", context={"inserted": len(inserted), "llm_cards": len(llm_cards)})
    return {"ok": True, "inserted": len(inserted), "insights_created": insight_count, "llm_cards": len(llm_cards), "cards": inserted[:20], "created_at": utc_now_iso()}


def _food_body_cards(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    food = [c for c in claims if c.get("predicate") in {"has_body_signal", "has_health_context"} and any(t in _blob(c) for t in _FOOD_TERMS) and any(t in _blob(c) for t in _BODY_TERMS)]
    help_terms = site_terms("synthesis.help_terms", _HELP_TERMS)
    helpful = [c for c in claims if c.get("predicate") in {"uses_activity", "uses_wellness_routine", "has_body_signal"} and any(t in _blob(c) for t in help_terms)]
    out = []
    if len(food) >= 1:
        chosen = food[:5] + helpful[:3]
        risk = "high" if _health_sensitive(chosen) else "low"
        out.append({
            "domain": "body_wellness",
            "title": "Food and body-state pattern",
            "summary": "Imported records suggest some foods or meal contexts are personally linked with stomach, constipation, bloating or puffiness concerns; use this only as personal pattern memory, not a general diet rule.",
            "confidence": min(0.92, max(float(c.get("confidence") or 0.6) for c in chosen)),
            "actionability": "high" if risk == "low" else "medium",
            "risk_level": risk,
            "claim_ids": [c["claim_id"] for c in chosen],
            "care_angle": "Useful before social/event days or when the owner asks about puffiness, stomach comfort or food choices.",
            "suggested_action": {
                "type": "contextual_body_care",
                "message_style": "light, practical, non-moralising",
                "example": "Tiny care thought: if today is an event/body-confidence day, steer around the foods your own history links with stomach trouble and pick a low-friction reset instead.",
            },
            "research_required": risk == "high",
            "evidence": {"food_claims": len(food), "helpful_routine_claims": len(helpful), "user_sources": sum(1 for c in chosen if _source_is_user(c))},
        })
    return out


def _posture_cards(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    posture = [c for c in claims if c.get("predicate") in {"has_posture_pattern", "uses_activity", "uses_wellness_routine", "has_body_signal"} and any(t in _blob(c) for t in _POSTURE_TERMS)]
    if not posture:
        return []
    return [{
        "domain": "body_wellness",
        "title": "Posture and proprioception care pattern",
        "summary": "Imported records contain posture, right-side, desk, physio or reset cues that can become tiny contextual body-care prompts rather than generic wellness nags.",
        "confidence": min(0.94, max(float(c.get("confidence") or 0.6) for c in posture[:8])),
        "actionability": "high",
        "risk_level": "low",
        "claim_ids": [c["claim_id"] for c in posture[:8]],
        "care_angle": "Best used after long desk blocks, before training, or when the owner mentions tension/posture.",
        "suggested_action": {
            "type": "posture_micro_reset",
            "message_style": "precise, short, body-aware",
            "example": site_text("synthesis.posture_reset_example", _POSTURE_RESET_EXAMPLE),
        },
        "evidence": {"posture_claims": len(posture), "user_sources": sum(1 for c in posture if _source_is_user(c))},
    }]


def _persona_care_cards(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    style = [c for c in claims if c.get("predicate") in {"prefers_wording", "has_guardrail", "prefers"} and any(t in _blob(c) for t in ("body", "wellness", "tone", "nudge", "phrase", "annoy", "cringe", "moral"))]
    if len(style) < 2:
        return []
    return [{
        "domain": "coaching_persona",
        "title": "Body-care tone contract",
        "summary": "Imported preferences define how Mongoose should speak about body, wellness and nudges: practical, precise, low-friction and not moralising.",
        "confidence": min(0.96, max(float(c.get("confidence") or 0.7) for c in style[:8])),
        "actionability": "high",
        "risk_level": "low",
        "claim_ids": [c["claim_id"] for c in style[:8]],
        "care_angle": "Apply as style guidance whenever surfacing body or wellness insight.",
        "suggested_action": {"type": "persona_rule_bundle", "message_style": "internal"},
        "evidence": {"style_claims": len(style)},
    }]


def _rtm_project_cards(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rtm = [c for c in claims if c.get("predicate") in {"has_legal_or_rtm_context", "has_financial_or_accounting_rule"}]
    if not rtm:
        return []
    return [{
        "domain": "rtm",
        "title": "RTM operating-rule bundle",
        "summary": "Imported RTM/legal/accounting records should be treated as implementation guardrails and checked before code changes or demand/accounting workflows.",
        "confidence": min(0.95, max(float(c.get("confidence") or 0.7) for c in rtm[:10])),
        "actionability": "high",
        "risk_level": "medium",
        "claim_ids": [c["claim_id"] for c in rtm[:10]],
        "care_angle": "Use as project guardrail context, not autonomous legal advice.",
        "suggested_action": {"type": "project_guardrail", "review_before_code": True},
        "research_required": True,
        "evidence": {"rtm_claims": len(rtm)},
    }]


def _run_llm_synthesis(claims: list[dict[str, Any]], entities: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    try:
        return asyncio.run(_llm_synthesis(claims, entities, limit=limit))
    except RuntimeError:
        return []
    except Exception:
        return []


async def _llm_synthesis(claims: list[dict[str, Any]], entities: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    from ..llm import LocalModelError, ask_ollama
    system = (
        "You are an internal private knowledge synthesis worker. Return JSON only. "
        "Create cautious, provenance-based care insights from imported owner claims. "
        "Do not create medical dosing, AAS cycling, acquisition or treatment instructions. "
        "Sensitive health claims must be risk_level high and research_required true."
    )
    payload = {
        "task": "synthesise_owner_knowledge",
        "output_schema": {
            "cards": [{
                "domain": "body_wellness|coaching_persona|rtm|project|general",
                "title": "short",
                "summary": "one sentence",
                "claim_ids": ["claim ids"],
                "confidence": 0.0,
                "actionability": "low|medium|high",
                "risk_level": "low|medium|high",
                "care_angle": "why it could help",
                "research_required": False,
                "suggested_action": {"type": "contextual_care", "example": "optional safe wording"}
            }]
        },
        "rules": [
            "Use only claim_ids present in the input.",
            "Prefer personal user-stated patterns over generic claims.",
            "No supplement, medication, dosing or AAS instructions.",
            "Keep risky material reviewable, not active.",
        ],
        "claims": [{"claim_id": c.get("claim_id"), "predicate": c.get("predicate"), "object": c.get("object"), "summary": c.get("summary"), "source_basis": c.get("source_basis"), "confidence": c.get("confidence"), "sensitivity": c.get("sensitivity")} for c in claims],
        "entities": [{"entity_id": e.get("entity_id"), "type": e.get("entity_type"), "name": e.get("name")} for e in entities[:60]],
    }
    try:
        raw = await ask_ollama(json.dumps(payload, ensure_ascii=False), system=system, role="research")
    except LocalModelError:
        return []
    data = _extract_json(raw)
    cards = data.get("cards", []) if isinstance(data, dict) else []
    valid_ids = {str(c.get("claim_id")) for c in claims}
    out = []
    for card in cards[:limit]:
        ids = [str(x) for x in card.get("claim_ids", []) if str(x) in valid_ids]
        if not ids:
            continue
        risk = str(card.get("risk_level") or "medium").lower()
        if risk not in {"low", "medium", "high"}:
            risk = "medium"
        out.append({
            "domain": str(card.get("domain") or "general")[:60],
            "title": str(card.get("title") or "Imported knowledge connection")[:140],
            "summary": str(card.get("summary") or "").strip()[:800],
            "confidence": max(0.0, min(0.95, float(card.get("confidence") or 0.55))),
            "actionability": str(card.get("actionability") or "medium") if str(card.get("actionability") or "medium") in {"low", "medium", "high"} else "medium",
            "risk_level": risk,
            "claim_ids": ids,
            "care_angle": str(card.get("care_angle") or "")[:500],
            "suggested_action": card.get("suggested_action") if isinstance(card.get("suggested_action"), dict) else {},
            "research_required": bool(card.get("research_required") or risk == "high"),
            "evidence": {"method": "local_llm_reviewable_synthesis"},
        })
    return out


def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}
