from __future__ import annotations

import json
import re
from typing import Any

from ..db import connect, utc_now_iso
from .schemas import Entity, Sensitivity, make_id
from .store import ensure_tables, insert_edge, insert_entity, insert_entity_fusion


_CANONICAL_ALIASES: dict[str, tuple[str, str, list[str]]] = {
    "nurofen": ("medication_context", "Nurofen / ibuprofen", ["Nurofen", "ibuprofen"]),
    "ibuprofen": ("medication_context", "Nurofen / ibuprofen", ["Nurofen", "ibuprofen"]),
    "allegra": ("medication_context", "Allegra / fexofenadine", ["Allegra", "fexofenadine"]),
    "fexofenadine": ("medication_context", "Allegra / fexofenadine", ["Allegra", "fexofenadine"]),
    "coenzyme q10": ("supplement", "CoQ10", ["CoQ10", "coenzyme Q10"]),
    "coq10": ("supplement", "CoQ10", ["CoQ10", "coenzyme Q10"]),
    "gi": ("body_area", "gastrointestinal system", ["GI", "gastrointestinal", "stomach/gut"]),
    "gastrointestinal": ("body_area", "gastrointestinal system", ["GI", "gastrointestinal", "stomach/gut"]),
    "stomach": ("body_area", "gastrointestinal system", ["stomach", "gut", "GI"]),
    "gut": ("body_area", "gastrointestinal system", ["stomach", "gut", "GI"]),
    
    
    
    "aas": ("aas_context", "AAS context", ["AAS", "anabolic steroids", "steroid context"]),
    "anabolic steroids": ("aas_context", "AAS context", ["AAS", "anabolic steroids", "steroid context"]),
}

_COMPATIBLE_TYPES = {
    "food": {"food", "dietary_option"},
    "dietary_option": {"food", "dietary_option"},
    "supplement": {"supplement", "probiotic", "dietary_option"},
    "probiotic": {"probiotic", "supplement", "dietary_option"},
    "medication_context": {"medication_context", "health_context"},
    "body_area": {"body_area", "body_signal"},
    "body_signal": {"body_signal", "body_area"},
    "aas_context": {"aas_context", "health_context"},
}


def _canonical_aliases() -> dict[str, tuple[str, str, list[str]]]:
    """Known alias groups: the generic table above, or the site profile's own (lexicon
    knowledge.canonical_aliases, {normalised name: [entity type, canonical name, [aliases]]})."""
    from .. import profile as _profile
    own = _profile.lexicon("knowledge.canonical_aliases", None)
    if own is None:
        return _CANONICAL_ALIASES
    return {k: (str(v[0]), str(v[1]), list(v[2])) for k, v in own.items()}


def _name_rewrites() -> list[tuple[str, str]]:
    """Spelling rewrites applied while normalising a name, in order (lexicon knowledge.entity_name_rewrites,
    [[regex, replacement], ...]), for the household's own entities whose spellings vary."""
    from .. import profile as _profile
    return [(str(p), str(r)) for p, r in (_profile.lexicon("knowledge.entity_name_rewrites", []) or [])]


def normalise_entity_name(name: str, rewrites: list[tuple[str, str]] | None = None) -> str:
    n = str(name or "").strip().lower()
    n = n.replace(".", " ").replace("/", " ").replace("_", " ")
    n = re.sub(r"[()\[\]{}'\"“”‘’]+", " ", n)
    for pattern, replacement in (rewrites if rewrites is not None else _name_rewrites()):
        n = re.sub(pattern, replacement, n)
    n = re.sub(r"[^a-z0-9+\- ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    if n.endswith("s") and len(n) > 4 and n not in {"aas"}:
        n = n[:-1]
    return n


def _canonical_for(kind: str, name: str, *, known: dict[str, tuple[str, str, list[str]]] | None = None,
                   rewrites: list[tuple[str, str]] | None = None) -> tuple[str, str, list[str], str]:
    norm = normalise_entity_name(name, rewrites)
    known = known if known is not None else _canonical_aliases()
    if norm in known:
        ck, cname, aliases = known[norm]
        return ck, cname, sorted(set(aliases + [name])), "known_alias"
    # Simple deterministic canonical key for spelling-only duplicates.
    safe_name = re.sub(r"[^a-z0-9]+", "_", norm).strip("_") or "unknown"
    return kind, str(name or "").strip(), [name], f"normalised:{kind}:{safe_name}"


def _canonical_id(kind: str, name: str, rewrites: list[tuple[str, str]] | None = None) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", normalise_entity_name(name, rewrites)).strip("_") or "unknown"
    return make_id(f"ent_{kind}", kind, safe)


def _merge_aliases(existing_json: str | None, new_aliases: list[str]) -> str:
    try:
        old = json.loads(existing_json or "[]")
        if not isinstance(old, list):
            old = []
    except Exception:
        old = []
    seen = []
    for value in [*old, *new_aliases]:
        text = str(value or "").strip()
        if text and text.lower() not in {x.lower() for x in seen}:
            seen.append(text)
    return json.dumps(seen[:40], ensure_ascii=False)


def _compatible(kind: str, canonical_kind: str) -> bool:
    if kind == canonical_kind:
        return True
    return kind in _COMPATIBLE_TYPES.get(canonical_kind, set()) or canonical_kind in _COMPATIBLE_TYPES.get(kind, set())


def run_entity_fusion(*, limit: int = 1000) -> dict[str, Any]:
    """Fuse imported entities into stable canonical owner graph nodes.

    Source entity rows are never deleted. The fusion table and `same_as`/`canonical_entity`
    edges preserve the audit trail while letting search, insights and care planning talk about one
    thing instead of one spelling per thread.
    """
    ensure_tables()
    with connect() as con:
        source_rows = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_entities ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()]
    groups: dict[str, dict[str, Any]] = {}
    known, rewrites = _canonical_aliases(), _name_rewrites()  # read the site profile once per pass
    for row in source_rows:
        kind = str(row.get("entity_type") or "entity")
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        ckind, cname, aliases, strategy = _canonical_for(kind, name, known=known, rewrites=rewrites)
        if not _compatible(kind, ckind):
            ckind, cname, aliases, strategy = kind, name, [name], f"normalised:{kind}:{normalise_entity_name(name, rewrites)}"
        cid = _canonical_id(ckind, cname, rewrites)
        grp = groups.setdefault(cid, {"kind": ckind, "name": cname, "aliases": [], "rows": [], "strategy": strategy})
        grp["aliases"].extend(aliases)
        grp["rows"].append(row)
        if strategy == "known_alias":
            grp["strategy"] = "known_alias"
    fusions = 0
    canonical_created = 0
    for cid, grp in groups.items():
        rows = grp["rows"]
        aliases = sorted({str(a).strip() for a in grp["aliases"] if str(a).strip()}, key=lambda x: x.lower())
        canonical = Entity(
            entity_id=cid,
            entity_type=grp["kind"],
            name=grp["name"],
            aliases=aliases,
            source_record_key=None,
            confidence=max(float(r.get("confidence") or 0.7) for r in rows),
            sensitivity=Sensitivity.high if any(str(r.get("sensitivity")) in {"high", "Sensitivity.high", "secret", "Sensitivity.secret"} for r in rows) else Sensitivity.medium,
        )
        insert_entity(canonical)
        canonical_created += 1
        for row in rows:
            dup = row["entity_id"]
            if dup == cid:
                # Still make sure aliases are updated on the canonical row.
                continue
            evidence = {
                "canonical_name": grp["name"],
                "duplicate_name": row.get("name"),
                "duplicate_type": row.get("entity_type"),
                "aliases": aliases,
            }
            insert_entity_fusion(
                cid,
                dup,
                strategy=grp["strategy"],
                confidence=min(0.98, max(float(row.get("confidence") or 0.7), 0.72)),
                evidence=evidence,
            )
            insert_edge(make_id("edge", dup, "same_as", cid), dup, "same_as", cid,
                        confidence=0.92 if grp["strategy"] == "known_alias" else 0.78,
                        sensitivity=str(row.get("sensitivity") or "medium"))
            insert_edge(make_id("edge", cid, "has_alias_entity", dup), cid, "has_alias_entity", dup,
                        confidence=0.9, sensitivity=str(row.get("sensitivity") or "medium"))
            fusions += 1
    return {
        "ok": True,
        "entities_seen": len(source_rows),
        "canonical_entities": canonical_created,
        "fusions": fusions,
        "created_at": utc_now_iso(),
    }
