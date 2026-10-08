from __future__ import annotations

from typing import Any

from .schemas import (
    CarePolicy,
    Claim,
    ImportStatus,
    InsightCandidate,
    MemoryCandidate,
    NudgeCandidate,
    NudgeMode,
    PersonaRule,
    ReviewItem,
    Sensitivity,
    SourceBasis,
    make_id,
    site_terms,
    site_text,
)

_AUTO_MEMORY_PREDICATES = {
    "has_stable_fact",
    "prefers",
    "prefers_wording",
    "has_guardrail",
    "has_body_signal",
    "has_posture_pattern",
    "uses_wellness_routine",
    "uses_activity",
    "has_schedule_constraint",
}

_PERSONA_PREDICATES = {
    "prefers",
    "prefers_wording",
    "has_guardrail",
    "has_emotional_context",
    "has_body_signal",
    "has_posture_pattern",
}

_HEALTH_RESEARCH_TERMS = (
    "medication", "supplement",
    "aas", "steroid", "dose", "dosage", "cycle",
    "liver", "jaundice", "red flag", "constipation",
)

_LOW_RISK_BODY_TERMS = (
    "pizza", "bread", "bloat", "puffiness", "swim", "yoga",
    "posture", "physio", "breathing", "reset",
)


# Cue words for the body-care suggestion shapes (each replaceable by a site profile lexicon of the same
# name under knowledge.*, see site_terms).
_RECOVERY_TERMS = ("swim", "yoga")
_RECOVERY_CUE = "If you feel a bit puffy or compressed, a short gentle reset may help"
_POSTURE_CUE_TERMS = ("posture", "desk", "physio", "breath", "breathing", "reset", "compression")
_FOOD_STATE_TERMS = ("pizza", "bread", "dairy", "sweets", "constipation", "bloat", "puffy", "puffiness", "stomach")
_AAS_TERMS = ("aas", "steroid", "cycle")
_MEDICAL_RED_FLAG_TERMS = ("vomiting", "jaundice", "severe abdominal", "blood in stool")


def _health_sensitive(claim: Claim) -> bool:
    # GOVERNANCE FIX (external audit, additional findings #6): this used to gate ONLY on a
    # keyword match against the claim's prose, so a high-sensitivity health_context record
    # whose summary happened not to contain one of those exact words (e.g. "white blood cell
    # count" without the word "blood test") became an accepted claim with ZERO review-queue
    # item, for a private assistant that may later surface health context in chat. The
    # predicate is set directly from RecordType.health_context at claim-build time (see
    # claim_builder.py's _PREDICATE map), never inferred from prose, so it can't miss the way
    # a keyword scan can. ALL high-sensitivity health_context claims now require review
    # regardless of wording; the keyword list still upgrades severity/reason text below, it is
    # no longer the only reason health context is gated.
    if claim.predicate == "has_health_context" and claim.sensitivity in {Sensitivity.high, "high"}:
        return True
    blob = f"{claim.object} {claim.summary} {claim.details}".lower()
    return claim.sensitivity in {Sensitivity.high, "high"} and any(
        t in blob for t in site_terms("health_research_terms", _HEALTH_RESEARCH_TERMS))


# Emotional/psychological material is sensitive regardless of how (or whether) a dump labelled it. Unlike
# the health gate this does NOT require a "high" sensitivity label, because legacy/prose dumps about
# grief, attachment or self-worth frequently arrive unlabelled. It gates memory promotion and proactive
# surfacing (including auto-care activation), but deliberately does NOT trigger web research.
_PSYCH_SENSITIVE_TERMS = (
    "grief", "grieving", "self-worth", "self worth", "worthless", "loneliness", "lonely",
    "depression", "depressed", "attachment", "abandon", "trauma", "traumatic", "suicidal",
    "suicide", "self-harm", "self harm", "dissociat", "panic attack", "shame", "abuse",
    "libido", "sexual", "intimacy", "dating", "rejection", "isolation",
)


def _psych_sensitive(claim: Claim) -> bool:
    blob = f"{claim.object} {claim.summary} {claim.details}".lower()
    return any(t in blob for t in _PSYCH_SENSITIVE_TERMS)


def _sensitive(claim: Claim) -> bool:
    return _health_sensitive(claim) or _psych_sensitive(claim)


def _body_suggested_action(c: Claim, blob: str) -> dict[str, Any]:
    """Convert a body/posture claim into a small, owner-friendly care action.

    The imported fact remains the evidence, but care prompts should say what to *do with it* rather
    than simply repeating a memory back to the owner. The action `type` also lets the reflection layer
    keep only genuinely useful low-risk patterns active (posture reset, recovery, food-state cue) and
    suppress bland baseline/profile facts."""
    summary = c.summary.strip()
    if any(t in blob for t in site_terms("recovery_terms", _RECOVERY_TERMS)):
        cue = site_text("recovery_cue", _RECOVERY_CUE)
        return {
            "type": "contextual_care",
            "message_style": "precise, friendly, not moralising",
            "use_case": "When the owner feels puffy, tense or compressed and has a low-friction window.",
            "example": f"{cue}: {summary}",
        }
    if any(t in blob for t in site_terms("posture_cue_terms", _POSTURE_CUE_TERMS)):
        return {
            "type": "posture_reset",
            "message_style": "warm, precise body cue",
            "use_case": "After desk time, before training, or when the owner mentions tension/posture.",
            "example": f"Tiny posture check: use this as a cue for a short reset rather than a big workout: {summary}",
        }
    if any(t in blob for t in site_terms("food_state_terms", _FOOD_STATE_TERMS)):
        return {
            "type": "body_state_food_cue",
            "message_style": "practical, light, non-moralising",
            "use_case": "Before events, meals, trips, or when the owner asks about bloat/GI comfort.",
            "example": f"Small body-state cue: if today is an event or low-bloat day, factor this in without turning it into a rule: {summary}",
        }
    return {
        "type": "contextual_care",
        "message_style": "precise, friendly, not moralising",
        "use_case": "Use only when current context makes the pattern relevant.",
        "example": f"Potentially useful context, not a nag: {summary}",
    }


def compile_memory_candidates(claims: list[Claim]) -> list[MemoryCandidate]:
    out: list[MemoryCandidate] = []
    for c in claims:
        trusted_source = c.source_basis in {
            SourceBasis.user_stated,
            SourceBasis.user_corrected,
            SourceBasis.user_accepted,
        }
        if not trusted_source:
            status = ImportStatus.review
            reason = "not auto-promoted because source was assistant-suggested, inferred or mixed"
        elif _psych_sensitive(c):
            status = ImportStatus.review
            reason = "sensitive psychological or emotional context requires review before becoming durable memory"
        elif c.predicate not in _AUTO_MEMORY_PREDICATES:
            status = ImportStatus.candidate
            reason = "kept as a candidate because this record type is not always durable memory"
        elif _health_sensitive(c) and c.predicate not in {"has_body_signal", "has_guardrail"}:
            status = ImportStatus.review
            reason = "sensitive health context requires review before becoming durable memory"
        elif c.confidence >= 0.82:
            status = ImportStatus.accepted
            reason = "high-confidence owner-provided record"
        else:
            status = ImportStatus.candidate
            reason = "confidence below automatic memory threshold"
        fact = c.summary.strip()
        out.append(MemoryCandidate(
            memory_id=make_id("mem", c.claim_id, fact),
            fact=fact,
            source_claim_id=c.claim_id,
            status=status,
            reason=reason,
            sensitivity=c.sensitivity,
        ))
    return out


def compile_persona_rules(claims: list[Claim]) -> list[PersonaRule]:
    out: list[PersonaRule] = []
    for c in claims:
        if c.predicate not in _PERSONA_PREDICATES:
            continue
        if c.source_basis in {SourceBasis.assistant_suggested, SourceBasis.inferred} and c.confidence < 0.9:
            continue
        if c.predicate == "prefers_wording":
            text = f"Wording preference: {c.summary}"
            rule_type = "wording"
            priority = 85
        elif c.predicate == "has_guardrail":
            text = f"Do not do this: {c.summary}"
            rule_type = "guardrail"
            priority = 95
        elif c.predicate in {"has_body_signal", "has_posture_pattern"}:
            text = f"When supporting the owner physically, remember: {c.summary}"
            rule_type = "body_support"
            priority = 70
        else:
            text = f"Owner preference: {c.summary}"
            rule_type = "preference"
            priority = 75
        out.append(PersonaRule(
            rule_id=make_id("persona", c.claim_id, rule_type),
            rule_type=rule_type,
            text=text,
            source_claim_id=c.claim_id,
            priority=priority,
            sensitivity=c.sensitivity,
            status=ImportStatus.accepted if c.status == ImportStatus.accepted else ImportStatus.candidate,
        ))
    return out


def _domain_for_claim(c: Claim) -> str:
    if c.predicate in {"has_body_signal", "has_posture_pattern", "uses_activity", "uses_wellness_routine", "has_health_context"}:
        return "body_wellness"
    if c.predicate in {"has_legal_or_rtm_context", "has_financial_or_accounting_rule"}:
        return "rtm"
    if c.predicate in {"has_project_requirement", "has_architecture_decision", "reported_bug", "has_fix_or_patch"}:
        return "project"
    return "general"


def compile_nudge_candidates(claims: list[Claim]) -> list[NudgeCandidate]:
    out: list[NudgeCandidate] = []
    for c in claims:
        if c.predicate not in {"has_body_signal", "has_posture_pattern", "uses_activity", "uses_wellness_routine", "has_schedule_constraint", "has_todo"}:
            continue
        domain = _domain_for_claim(c)
        if c.predicate == "has_body_signal":
            trigger = "When the owner asks about the related body state, food, event prep or symptom."
            msg = f"Small practical check: {c.summary}"
            mode = NudgeMode.contextual_only
        elif c.predicate == "has_posture_pattern":
            trigger = "After a long desk block, before training, or when the owner mentions tension/posture."
            msg = f"Tiny posture thought: {c.summary}"
            mode = NudgeMode.scheduled_candidate
        elif c.predicate in {"uses_activity", "uses_wellness_routine"}:
            trigger = "When current context matches the routine's use case and it would be low-friction."
            msg = f"This might help now: {c.summary}"
            mode = NudgeMode.scheduled_candidate
        else:
            trigger = "When due or contextually relevant."
            msg = c.summary
            mode = NudgeMode.ask_first
        risk = _sensitive(c)
        policy = CarePolicy(
            nudge_mode=NudgeMode.ask_first if risk else mode,
            maximum_frequency="contextual only" if mode == NudgeMode.contextual_only else "ask before activating",
            interruptibility="low",
            sensitivity=c.sensitivity,
            needs_consent_before_action=True,
            silence_after_ignored_count=2,
            research_required=_health_sensitive(c),
            explanation_template="Based on imported owner knowledge, not a generic rule.",
        )
        out.append(NudgeCandidate(
            nudge_id=make_id("nudge", c.claim_id, trigger),
            domain=domain,
            title=c.object or c.predicate,
            trigger=trigger,
            suggested_message=msg,
            source_claim_id=c.claim_id,
            care_policy=policy,
            status=ImportStatus.candidate,
        ))
    return out


def compile_insight_candidates(claims: list[Claim]) -> list[InsightCandidate]:
    out: list[InsightCandidate] = []
    by_domain: dict[str, list[Claim]] = {}
    for c in claims:
        by_domain.setdefault(_domain_for_claim(c), []).append(c)
    for domain, domain_claims in by_domain.items():
        if domain == "body_wellness":
            body_claims = [c for c in domain_claims if c.predicate in {"has_body_signal", "has_posture_pattern", "uses_activity", "uses_wellness_routine", "has_health_context"}]
            low_risk_terms = site_terms("low_risk_body_terms", _LOW_RISK_BODY_TERMS)
            for c in body_claims:
                blob = f"{c.object} {c.summary}".lower()
                low_risk = any(t in blob for t in low_risk_terms)
                risk = "high" if _sensitive(c) else ("low" if low_risk else "medium")
                research_status = "needed" if _health_sensitive(c) else "not_needed"
                out.append(InsightCandidate(
                    insight_id=make_id("insight", c.claim_id, "body"),
                    domain=domain,
                    title=f"Owner body pattern: {c.object}",
                    hypothesis=c.summary,
                    supporting_claim_ids=[c.claim_id],
                    confidence=c.confidence,
                    actionability="high" if low_risk and c.confidence >= 0.8 else "medium",
                    risk_level=risk,
                    research_status=research_status,
                    suggested_action=_body_suggested_action(c, blob),
                    care_policy=CarePolicy(
                        nudge_mode=NudgeMode.ask_first if risk == "high" else NudgeMode.contextual_only,
                        maximum_frequency="contextual only",
                        interruptibility="low",
                        sensitivity=c.sensitivity,
                        needs_consent_before_action=True,
                        research_required=research_status == "needed",
                        explanation_template="Because this came from an imported owner thread with provenance.",
                    ),
                    status=ImportStatus.candidate,
                ))
        if domain == "rtm":
            ids = [c.claim_id for c in domain_claims]
            if ids:
                out.append(InsightCandidate(
                    insight_id=make_id("insight", domain, *ids[:6]),
                    domain=domain,
                    title="RTM rule cluster from imported knowledge",
                    hypothesis="Imported RTM records contain operating rules or accounting/legal constraints that should be applied as project guardrails.",
                    supporting_claim_ids=ids,
                    confidence=max(c.confidence for c in domain_claims),
                    actionability="high",
                    risk_level="medium",
                    research_status="needed",
                    suggested_action={"type": "project_guardrail", "review_before_code": True},
                    care_policy=CarePolicy(nudge_mode=NudgeMode.ask_first, research_required=True),
                ))
    return out


def compile_safety_rules(claims: list[Claim]) -> list[tuple[str, str, str, str, str]]:
    out: list[tuple[str, str, str, str, str]] = []
    aas_terms = site_terms("aas_terms", _AAS_TERMS)
    red_flag_terms = site_terms("medical_red_flag_terms", _MEDICAL_RED_FLAG_TERMS)
    for c in claims:
        blob = f"{c.object} {c.summary} {c.details}".lower()
        if any(t in blob for t in aas_terms):
            out.append((
                make_id("safety", c.claim_id, "aas"),
                "health",
                "Keep AAS-related records as harm-reduction or retrieval context only; do not generate dosing, cycling or acquisition guidance.",
                "high",
                c.claim_id,
            ))
        if any(t in blob for t in red_flag_terms):
            out.append((
                make_id("safety", c.claim_id, "medical_red_flags"),
                "health",
                "For medication/GI red flags, prioritise medical escalation and safe general context over supplement optimisation.",
                "high",
                c.claim_id,
            ))
        if c.do_not_overgeneralise or c.predicate == "has_guardrail":
            out.append((
                make_id("safety", c.claim_id, "overgeneralise"),
                "memory_quality",
                f"Do not overgeneralise this imported record: {c.summary}",
                "medium",
                c.claim_id,
            ))
    # Deduplicate.
    seen = {}
    for r in out:
        seen.setdefault(r[0], r)
    return list(seen.values())


def compile_review_items(claims: list[Claim], memories: list[MemoryCandidate], insights: list[InsightCandidate]) -> list[ReviewItem]:
    out: list[ReviewItem] = []
    for c in claims:
        if c.status == ImportStatus.review:
            out.append(ReviewItem(
                review_id=make_id("review", c.claim_id, "claim"),
                object_type="claim",
                object_id=c.claim_id,
                reason="Claim is sensitive, inferred, assistant-suggested, or needs human review before acting.",
                severity="high" if c.sensitivity == Sensitivity.high else "medium",
                source_record_key=c.source_record_key,
            ))
        if _health_sensitive(c):
            out.append(ReviewItem(
                review_id=make_id("review", c.claim_id, "health"),
                object_type="claim",
                object_id=c.claim_id,
                reason="Sensitive health or supplement/medication/AAS context requires research or explicit owner review before proactive advice.",
                severity="high",
                source_record_key=c.source_record_key,
            ))
    for m in memories:
        if m.status == ImportStatus.review:
            out.append(ReviewItem(
                review_id=make_id("review", m.memory_id, "memory"),
                object_type="memory_candidate",
                object_id=m.memory_id,
                reason=m.reason,
                severity="medium" if m.sensitivity != Sensitivity.high else "high",
            ))
    for i in insights:
        if i.research_status == "needed" or i.risk_level == "high":
            out.append(ReviewItem(
                review_id=make_id("review", i.insight_id, "insight"),
                object_type="insight_candidate",
                object_id=i.insight_id,
                reason="Insight needs research or explicit approval before it becomes an active nudge.",
                severity="high" if i.risk_level == "high" else "medium",
            ))
    seen: dict[str, ReviewItem] = {}
    for item in out:
        seen.setdefault(item.review_id, item)
    return list(seen.values())
