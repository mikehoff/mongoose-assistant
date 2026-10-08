from __future__ import annotations

import asyncio
import json
import os
import re
from difflib import SequenceMatcher
from typing import Any

from ..db import connect, utc_now_iso
from .entity_fusion import _name_rewrites, normalise_entity_name, run_entity_fusion
from .store import ensure_tables, insert_fusion_suggestion, list_fusion_suggestions

_COMPATIBLE = {
    "food": {"food", "dietary_option"},
    "dietary_option": {"food", "dietary_option", "supplement", "probiotic"},
    "supplement": {"supplement", "probiotic", "dietary_option"},
    "probiotic": {"probiotic", "supplement", "dietary_option"},
    "body_area": {"body_area", "body_signal"},
    "body_signal": {"body_signal", "body_area"},
    "medication_context": {"medication_context", "health_context"},
    "aas_context": {"aas_context", "health_context"},
    "device": {"device", "device_or_integration"},
}

_STOP = {"the", "and", "for", "with", "from", "context", "pattern", "system", "body", "owner"}


def _compatible(a: str, b: str) -> bool:
    return a == b or b in _COMPATIBLE.get(a, set()) or a in _COMPATIBLE.get(b, set())


def _token_set(name: str, rewrites: list[tuple[str, str]] | None = None) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", normalise_entity_name(name, rewrites))
            if len(t) > 2 and t not in _STOP}


def _similarity(a: str, b: str, rewrites: list[tuple[str, str]] | None = None) -> float:
    # `rewrites` (entity_fusion._name_rewrites()) is passed by the pairwise loop so the site profile is
    # read once per pass rather than once per pair.
    if rewrites is None:
        rewrites = _name_rewrites()
    na, nb = normalise_entity_name(a, rewrites), normalise_entity_name(b, rewrites)
    if not na or not nb or na == nb:
        return 1.0 if na == nb else 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = _token_set(a, rewrites), _token_set(b, rewrites)
    jacc = len(ta & tb) / max(1, len(ta | tb))
    containment = 0.0
    if na in nb or nb in na:
        containment = min(len(na), len(nb)) / max(len(na), len(nb))
    return max(seq, jacc, containment)


def _existing_pairs() -> set[tuple[str, str]]:
    with connect() as con:
        rows = con.execute("SELECT canonical_entity_id, duplicate_entity_id FROM knowledge_entity_fusions WHERE status='active'").fetchall()
        sugg = con.execute("SELECT canonical_entity_id, duplicate_entity_id FROM knowledge_entity_fusion_suggestions WHERE status IN ('open','approved')").fetchall()
    return {(str(r[0] or ''), str(r[1] or '')) for r in [*rows, *sugg]}


def suggest_entity_fusions(*, limit: int = 60, use_llm: bool | None = None) -> dict[str, Any]:
    """Create reviewable entity-fusion suggestions.

    Deterministic known aliases still fuse automatically through run_entity_fusion(). This layer is for
    fuzzier equivalences and local-LLM suggestions, so every result stays reviewable until approved.
    """
    ensure_tables()
    active = run_entity_fusion(limit=max(limit * 3, 200))
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_entities ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (max(1, min(limit * 8, 1500)),),
        ).fetchall()]
    pairs = _existing_pairs()
    rewrites = _name_rewrites()
    made: list[dict[str, Any]] = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if len(made) >= limit:
                break
            if a["entity_id"] == b["entity_id"]:
                continue
            if not _compatible(str(a.get("entity_type") or ""), str(b.get("entity_type") or "")):
                continue
            score = _similarity(str(a.get("name") or ""), str(b.get("name") or ""), rewrites)
            if score < 0.82:
                continue
            # Prefer the higher-confidence, more descriptive name as canonical.
            ac = float(a.get("confidence") or 0.5)
            bc = float(b.get("confidence") or 0.5)
            if bc > ac or (bc == ac and len(str(b.get("name") or "")) > len(str(a.get("name") or ""))):
                canonical, duplicate = b, a
            else:
                canonical, duplicate = a, b
            pair = (str(canonical["entity_id"]), str(duplicate["entity_id"]))
            if pair in pairs:
                continue
            evidence = {
                "method": "deterministic_similarity",
                "name_similarity": round(score, 3),
                "canonical_tokens": sorted(_token_set(str(canonical.get("name") or ""), rewrites)),
                "duplicate_tokens": sorted(_token_set(str(duplicate.get("name") or ""), rewrites)),
                "active_fusion_pass": active,
                "safety": "reviewable suggestion only; not active until approved",
            }
            sid = insert_fusion_suggestion(
                canonical_entity_id=str(canonical.get("entity_id") or ""),
                canonical_name=str(canonical.get("name") or ""),
                canonical_type=str(canonical.get("entity_type") or "entity"),
                duplicate_entity_id=str(duplicate.get("entity_id") or ""),
                duplicate_name=str(duplicate.get("name") or ""),
                duplicate_type=str(duplicate.get("entity_type") or "entity"),
                strategy="deterministic_similarity",
                confidence=min(0.9, max(0.55, score)),
                evidence=evidence,
            )
            made.append({"suggestion_id": sid, **evidence, "canonical": canonical.get("name"), "duplicate": duplicate.get("name")})
        if len(made) >= limit:
            break
    llm = []
    if use_llm is None:
        use_llm = os.getenv("MONGOOSE_KNOWLEDGE_LLM_FUSION", "").lower() in {"1", "true", "yes"}
    if use_llm:
        llm = _run_llm_suggestions(rows[:80], limit=max(5, min(20, limit - len(made))))
        made.extend(llm)
    return {
        "ok": True,
        "created": len(made),
        "deterministic_created": len(made) - len(llm),
        "llm_created": len(llm),
        "active_fusion_pass": active,
        "open_suggestions": list_fusion_suggestions(limit=limit),
        "created_at": utc_now_iso(),
    }


def _run_llm_suggestions(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        return asyncio.run(_llm_suggestions(rows, limit=limit))
    except RuntimeError:
        # Already in an event loop or unavailable in a worker. Keep it deterministic.
        return []
    except Exception:
        return []


async def _llm_suggestions(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    from ..llm import LocalModelError, ask_ollama
    compact = [
        {"entity_id": r.get("entity_id"), "type": r.get("entity_type"), "name": r.get("name"), "aliases": r.get("aliases_json")}
        for r in rows
    ]
    system = (
        "You are an internal knowledge-graph deduplication worker. Return JSON only. "
        "Suggest only obvious alias/equivalence merges for a private personal knowledge graph. "
        "Do not merge merely related concepts. Sensitive health items must remain reviewable."
    )
    prompt = json.dumps({
        "task": "suggest_alias_merges",
        "rules": [
            "Return an array under key suggestions.",
            "Each suggestion needs canonical_entity_id, duplicate_entity_id, confidence 0..1, and reason.",
            "Only suggest true aliases or same concept with spelling/medical-brand variants.",
            "Do not suggest supplement dosing or health advice.",
        ],
        "entities": compact,
    }, ensure_ascii=False)
    try:
        raw = await ask_ollama(prompt, system=system, role="research")
    except LocalModelError:
        return []
    data = _extract_json(raw)
    suggestions = data.get("suggestions", []) if isinstance(data, dict) else []
    by_id = {str(r.get("entity_id")): r for r in rows}
    out = []
    for item in suggestions[:limit]:
        c = by_id.get(str(item.get("canonical_entity_id") or ""))
        d = by_id.get(str(item.get("duplicate_entity_id") or ""))
        if not c or not d or c["entity_id"] == d["entity_id"]:
            continue
        conf = float(item.get("confidence") or 0.0)
        if conf < 0.72:
            continue
        if not _compatible(str(c.get("entity_type") or ""), str(d.get("entity_type") or "")):
            continue
        sid = insert_fusion_suggestion(
            canonical_entity_id=str(c.get("entity_id") or ""),
            canonical_name=str(c.get("name") or ""),
            canonical_type=str(c.get("entity_type") or "entity"),
            duplicate_entity_id=str(d.get("entity_id") or ""),
            duplicate_name=str(d.get("name") or ""),
            duplicate_type=str(d.get("entity_type") or "entity"),
            strategy="llm_reviewable_alias",
            confidence=min(0.96, conf),
            evidence={"method": "local_llm", "reason": str(item.get("reason") or "")[:500], "safety": "reviewable suggestion only"},
        )
        out.append({"suggestion_id": sid, "canonical": c.get("name"), "duplicate": d.get("name"), "confidence": conf})
    return out


def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
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
