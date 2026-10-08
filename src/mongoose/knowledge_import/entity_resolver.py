from __future__ import annotations

import re
from typing import Any

from .schemas import Entity, Sensitivity, ThreadRecord, make_id


_ENTITY_DETAIL_KEYS = {
    "food": "food",
    "food_trigger": "food",
    "supplement": "supplement",
    "exercise_name": "exercise",
    "body_area": "body_area",
    "symptom_or_pattern": "body_signal",
    "device": "device",
    "file_name": "file",
    "building": "rtm_building",
    "case_reference": "legal_case",
    "statutory_reference": "legal_reference",
}

_LIST_KEYS = {
    "supplements_user_listed": "supplement",
    "dietary_options_user_raised": "dietary_option",
    "red_flags_to_escalate": "red_flag",
    "known_exercises": "exercise",
    "top_known_triggers": "food",
    "candidate_triggers": "food",
}

_DOMAIN_TERMS = {
    "pizza": "food",
    "bread": "food",
    "dairy": "food",
    "sweets": "food",
    "nurofen": "medication_context",
    "ibuprofen": "medication_context",
    "allegra": "medication_context",
    "fexofenadine": "medication_context",
    "gutcheck": "data_source",
    "acidophilus": "probiotic",
    "ginger": "supplement",
    "coq10": "supplement",
    "magnesium": "supplement",
    "zinc": "supplement",
}


def _domain_terms() -> dict[str, str]:
    """Domain lexicon (term -> entity type): the generic table above, or the site profile's own
    (lexicon knowledge.domain_terms, same shape)."""
    from .. import profile as _profile
    own = _profile.lexicon("knowledge.domain_terms", None)
    return _DOMAIN_TERMS if own is None else {str(k): str(v) for k, v in own.items()}


def _norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _entity_id(kind: str, name: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unknown"
    return make_id(f"ent_{kind}", kind, safe)


def _entity(kind: str, name: str, record: ThreadRecord, confidence: float = 0.72) -> Entity | None:
    name = _norm_name(name)
    if not name:
        return None
    if len(name) > 160:
        name = name[:160].rstrip()
    return Entity(
        entity_id=_entity_id(kind, name),
        entity_type=kind,
        name=name,
        aliases=[],
        source_record_key=record.stable_record_key(),
        confidence=confidence,
        sensitivity=record.sensitivity if isinstance(record.sensitivity, Sensitivity) else Sensitivity.medium,
    )


def resolve_entities(record: ThreadRecord) -> list[Entity]:
    """Deterministic entity extraction from a structured dump record.

    This is deliberately conservative. It creates graph anchor nodes from explicit fields and a small
    domain lexicon, but it does not invent diagnoses or infer current medical status from a thread.
    """
    out: list[Entity] = []
    details = record.details or {}
    for key, kind in _ENTITY_DETAIL_KEYS.items():
        value = details.get(key)
        if value not in (None, "", []):
            ent = _entity(kind, value, record, 0.85)
            if ent:
                out.append(ent)
    for key, kind in _LIST_KEYS.items():
        values = details.get(key)
        if isinstance(values, list):
            for value in values[:50]:
                ent = _entity(kind, value, record, 0.75)
                if ent:
                    out.append(ent)
    blob = f"{record.topic} {record.summary} " + " ".join(str(x) for x in details.values())
    low = blob.lower()
    for term, kind in _domain_terms().items():
        if term in low:
            ent = _entity(kind, term, record, 0.7)
            if ent:
                out.append(ent)
    # Deduplicate by id, preserving first confidence.
    seen: dict[str, Entity] = {}
    for ent in out:
        seen.setdefault(ent.entity_id, ent)
    return list(seen.values())
