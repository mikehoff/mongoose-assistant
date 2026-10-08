from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, ConfigDict


def site_terms(name: str, base: tuple[str, ...]) -> tuple[str, ...]:
    """A knowledge-import word list: the generic `base`, or the site profile's own version of it
    (lexicon knowledge.<name>), which replaces the base so a household's vocabulary is used exactly."""
    from .. import profile as _profile
    own = _profile.lexicon("knowledge." + name, None)
    return base if own is None else tuple(own)


def site_text(name: str, base: str) -> str:
    """A knowledge-import phrase: the generic `base`, or the site profile's own wording (lexicon
    knowledge.<name>)."""
    from .. import profile as _profile
    return str(_profile.lexicon("knowledge." + name, base))


class RecordType(StrEnum):
    stable_user_fact = "stable_user_fact"
    preference = "preference"
    wording_preference = "wording_preference"
    do_not_do = "do_not_do"
    body_signal = "body_signal"
    posture_pattern = "posture_pattern"
    exercise_or_activity = "exercise_or_activity"
    wellness_routine = "wellness_routine"
    health_context = "health_context"
    emotional_context = "emotional_context"
    trigger_or_warning_sign = "trigger_or_warning_sign"
    environment_context = "environment_context"
    schedule_constraint = "schedule_constraint"
    device_or_integration = "device_or_integration"
    project_requirement = "project_requirement"
    architecture_decision = "architecture_decision"
    bug_report = "bug_report"
    fix_or_patch = "fix_or_patch"
    test_expectation = "test_expectation"
    open_question = "open_question"
    todo = "todo"
    legal_or_rtm_context = "legal_or_rtm_context"
    financial_or_accounting_rule = "financial_or_accounting_rule"
    named_entity = "named_entity"
    file_or_artifact = "file_or_artifact"
    quote_or_phrase = "quote_or_phrase"
    relationship_or_contact = "relationship_or_contact"
    other_useful_context = "other_useful_context"
    contradiction = "contradiction"
    implementation_implication = "implementation_implication"


class SourceBasis(StrEnum):
    user_stated = "user_stated"
    user_corrected = "user_corrected"
    user_accepted = "user_accepted"
    assistant_suggested = "assistant_suggested"
    inferred = "inferred"
    mixed = "mixed"


class Sensitivity(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    secret = "secret"


class ImportStatus(StrEnum):
    accepted = "accepted"
    candidate = "candidate"
    review = "review"
    rejected = "rejected"


class NudgeMode(StrEnum):
    silent_memory = "silent_memory"
    contextual_only = "contextual_only"
    ask_first = "ask_first"
    scheduled_candidate = "scheduled_candidate"
    active = "active"


class ThreadMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    dump_version: str = "unknown"
    thread_title: str = "unknown"
    thread_date_range: str = "unknown"
    extracted_at: str = "unknown"
    primary_domains: list[str] = Field(default_factory=list)
    sensitivity_level: str = "mixed"
    overall_confidence: str = "unknown"
    source_quality_notes: list[str] = Field(default_factory=list)


class ThreadRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    record_type: RecordType
    topic: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    source_basis: SourceBasis
    confidence: float = 0.5
    sensitivity: Sensitivity = Sensitivity.medium
    time_context: str = ""
    use_for_mongoose: str = ""
    do_not_overgeneralise: bool = False
    retrieval_tags: list[str] = Field(default_factory=list)
    memory_importance: str | None = None
    acceptance_state: str | None = None

    @field_validator("topic", "summary")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("required text field is empty")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_bounds(cls, v: float) -> float:
        try:
            f = float(v)
        except Exception as exc:
            raise ValueError("confidence must be numeric") from exc
        if f < 0:
            return 0.0
        if f > 1:
            return 1.0
        return f

    @field_validator("retrieval_tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    def stable_record_key(self) -> str:
        payload = {
            "record_type": str(self.record_type),
            "topic": self.topic.strip().lower(),
            "summary": self.summary.strip().lower(),
            "details": self.details,
            "source_basis": str(self.source_basis),
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Entity(BaseModel):
    entity_id: str
    entity_type: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    source_record_key: str | None = None
    confidence: float = 0.7
    sensitivity: Sensitivity = Sensitivity.medium


class Claim(BaseModel):
    claim_id: str
    subject: str = "owner"
    predicate: str
    object: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    source_record_key: str
    source_basis: SourceBasis
    confidence: float
    sensitivity: Sensitivity
    status: ImportStatus = ImportStatus.candidate
    do_not_overgeneralise: bool = False
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MemoryCandidate(BaseModel):
    memory_id: str
    fact: str
    source_claim_id: str
    status: ImportStatus
    reason: str
    scope: str = "owner"
    sensitivity: Sensitivity = Sensitivity.medium


class PersonaRule(BaseModel):
    rule_id: str
    rule_type: str
    text: str
    source_claim_id: str
    priority: int = 50
    sensitivity: Sensitivity = Sensitivity.medium
    status: ImportStatus = ImportStatus.candidate


class CarePolicy(BaseModel):
    nudge_mode: NudgeMode = NudgeMode.contextual_only
    maximum_frequency: str = "contextual only"
    interruptibility: Literal["low", "medium", "high"] = "low"
    sensitivity: Sensitivity = Sensitivity.medium
    needs_consent_before_action: bool = True
    silence_after_ignored_count: int = 2
    research_required: bool = False
    explanation_template: str = "Based on imported owner memory and current context."


class NudgeCandidate(BaseModel):
    nudge_id: str
    domain: str
    title: str
    trigger: str
    suggested_message: str
    source_claim_id: str
    care_policy: CarePolicy
    status: ImportStatus = ImportStatus.candidate


class InsightCandidate(BaseModel):
    insight_id: str
    domain: str
    title: str
    hypothesis: str
    supporting_claim_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    actionability: Literal["low", "medium", "high"] = "medium"
    risk_level: Literal["low", "medium", "high"] = "medium"
    research_status: Literal["not_needed", "needed", "requested", "completed", "failed"] = "not_needed"
    suggested_action: dict[str, Any] = Field(default_factory=dict)
    care_policy: CarePolicy = Field(default_factory=CarePolicy)
    status: ImportStatus = ImportStatus.candidate


class ReviewItem(BaseModel):
    review_id: str
    object_type: str
    object_id: str
    reason: str
    severity: Literal["low", "medium", "high"] = "medium"
    source_record_key: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ResearchRequest(BaseModel):
    topic: str
    question: str
    domain: str
    why_needed: str
    source_policy: Literal["local_first", "official_only", "medical_authoritative", "broad_web", "primary_legal"] = "local_first"
    time_sensitivity: Literal["stable", "current", "volatile"] = "stable"
    max_results: int = 8
    require_citations: bool = True


class EvidenceCard(BaseModel):
    title: str
    url: str
    source: str = "web"
    snippet: str = ""
    published_or_updated: str | None = None
    source_quality: str = "unknown"
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    uncertainty: str = ""


class ImportReport(BaseModel):
    ok: bool
    import_id: str
    package_type: str
    thread_title: str
    records_seen: int = 0
    records_imported: int = 0
    records_needing_review: int = 0
    claims_created: int = 0
    canonical_memories_created: int = 0
    persona_rules_created: int = 0
    scheduler_candidates_created: int = 0
    insight_candidates_created: int = 0
    safety_rules_created: int = 0
    raw_files_preserved: int = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def make_id(prefix: str, *parts: Any) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"
