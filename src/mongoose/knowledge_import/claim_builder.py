from __future__ import annotations


from .schemas import Claim, ImportStatus, RecordType, ThreadRecord, make_id, site_terms


_PREDICATES: dict[RecordType, str] = {
    RecordType.stable_user_fact: "has_stable_fact",
    RecordType.preference: "prefers",
    RecordType.wording_preference: "prefers_wording",
    RecordType.do_not_do: "has_guardrail",
    RecordType.body_signal: "has_body_signal",
    RecordType.posture_pattern: "has_posture_pattern",
    RecordType.exercise_or_activity: "uses_activity",
    RecordType.wellness_routine: "uses_wellness_routine",
    RecordType.health_context: "has_health_context",
    RecordType.emotional_context: "has_emotional_context",
    RecordType.trigger_or_warning_sign: "has_warning_sign",
    RecordType.environment_context: "has_environment_context",
    RecordType.schedule_constraint: "has_schedule_constraint",
    RecordType.device_or_integration: "has_device_or_integration",
    RecordType.project_requirement: "has_project_requirement",
    RecordType.architecture_decision: "has_architecture_decision",
    RecordType.bug_report: "reported_bug",
    RecordType.fix_or_patch: "has_fix_or_patch",
    RecordType.test_expectation: "has_test_expectation",
    RecordType.open_question: "has_open_question",
    RecordType.todo: "has_todo",
    RecordType.legal_or_rtm_context: "has_legal_or_rtm_context",
    RecordType.financial_or_accounting_rule: "has_financial_or_accounting_rule",
    RecordType.named_entity: "mentions_entity",
    RecordType.file_or_artifact: "has_file_or_artifact",
    RecordType.quote_or_phrase: "uses_phrase",
    RecordType.relationship_or_contact: "has_relationship_or_contact",
    RecordType.other_useful_context: "has_other_context",
    RecordType.contradiction: "has_contradiction",
    RecordType.implementation_implication: "has_implementation_implication",
}

_SENSITIVE_HEALTH_TERMS = (
    "aas", "steroid", "allegra",
    "dose", "dosage", "cycle", "liver", "jaundice", "constipation", "vomiting",
)


def _object_for_record(record: ThreadRecord) -> str:
    details = record.details or {}
    for key in ("food_trigger", "exercise_name", "file_name", "rule", "body_area", "symptom_or_pattern"):
        val = details.get(key)
        if val not in (None, "", []):
            return str(val)[:180]
    return record.topic[:180]


def _risk_status(record: ThreadRecord) -> ImportStatus:
    if record.source_basis in {"assistant_suggested", "inferred"}:
        return ImportStatus.review if record.sensitivity == "high" else ImportStatus.candidate
    if record.record_type in {RecordType.health_context, RecordType.trigger_or_warning_sign}:
        low = f"{record.topic} {record.summary}".lower()
        if any(t in low for t in site_terms("sensitive_health_terms", _SENSITIVE_HEALTH_TERMS)):
            return ImportStatus.review
    return ImportStatus.accepted if record.confidence >= 0.75 else ImportStatus.candidate


def build_claim(record: ThreadRecord) -> Claim:
    predicate = _PREDICATES.get(record.record_type, "has_context")
    obj = _object_for_record(record)
    claim_id = make_id("claim", record.stable_record_key(), predicate, obj)
    return Claim(
        claim_id=claim_id,
        predicate=predicate,
        object=obj,
        summary=record.summary,
        details=record.details,
        source_record_key=record.stable_record_key(),
        source_basis=record.source_basis,
        confidence=record.confidence,
        sensitivity=record.sensitivity,
        status=_risk_status(record),
        do_not_overgeneralise=record.do_not_overgeneralise,
        tags=record.retrieval_tags,
    )


def build_claims(records: list[ThreadRecord]) -> list[Claim]:
    return [build_claim(r) for r in records]
