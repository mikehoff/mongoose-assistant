from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..db import connect, utc_now_iso
from ..services.memory import OWNER_SCOPE, remember_fact
from .claim_builder import build_claims
from .compilers import (
    compile_insight_candidates,
    compile_memory_candidates,
    compile_nudge_candidates,
    compile_persona_rules,
    compile_review_items,
    compile_safety_rules,
)
from .entity_resolver import resolve_entities
from .entity_fusion import run_entity_fusion
from .entity_suggestions import suggest_entity_fusions
from .synthesis import run_synthesis_cycle
from .archive_orchestrator import import_archive_bytes, looks_like_mongoose_archive
from .legacy_normalizer import normalise_legacy_package_bytes
from .package_detector import KnowledgeImportError, looks_like_thread_dump_zip, parse_package_bytes
from .schemas import ImportReport, ImportStatus, make_id
from .store import (
    build_report,
    ensure_tables,
    import_summary,
    insert_import,
    insert_raw_file,
    recent_imports,
)



def _edge_rows_for(claims, entities):
    ents_by_record: dict[str, list[Any]] = defaultdict(list)
    for ent in entities:
        if ent.source_record_key:
            ents_by_record[ent.source_record_key].append(ent)
    now = utc_now_iso()
    rows = []
    for claim in claims:
        object_node = make_id("literal", claim.object.lower().strip())
        rows.append((
            make_id("edge", "owner", claim.predicate, object_node, claim.claim_id),
            "owner", claim.predicate, object_node, claim.claim_id, float(claim.confidence), str(claim.sensitivity), now, now,
        ))
        for ent in ents_by_record.get(claim.source_record_key, []):
            rows.append((
                make_id("edge", "owner", claim.predicate, ent.entity_id, claim.claim_id),
                "owner", claim.predicate, ent.entity_id, claim.claim_id, float(min(claim.confidence, ent.confidence)), str(claim.sensitivity), now, now,
            ))
            rows.append((
                make_id("edge", ent.entity_id, "mentioned_in_claim", claim.claim_id),
                ent.entity_id, "mentioned_in_claim", claim.claim_id, claim.claim_id, float(min(claim.confidence, ent.confidence)), str(claim.sensitivity), now, now,
            ))
    return rows


def _bulk_insert_records_entities_claims_edges(import_id: str, indexed_records, entities, claims) -> int:
    ensure_tables()
    now = utc_now_iso()
    edge_rows = _edge_rows_for(claims, entities)
    with connect() as con:
        con.executemany(
            """
            INSERT OR REPLACE INTO knowledge_records(record_key, import_id, record_index, record_type,
                                                     topic, summary, source_basis, confidence,
                                                     sensitivity, status, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (record.stable_record_key(), import_id, idx, str(record.record_type), record.topic,
                 record.summary, str(record.source_basis), record.confidence, str(record.sensitivity),
                 "accepted", record.model_dump_json(), now)
                for idx, record in indexed_records
            ],
        )
        con.executemany(
            """
            INSERT INTO knowledge_entities(entity_id, entity_type, name, aliases_json,
                                           source_record_key, confidence, sensitivity,
                                           created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id) DO UPDATE SET
                aliases_json=excluded.aliases_json,
                confidence=max(knowledge_entities.confidence, excluded.confidence),
                updated_at=excluded.updated_at
            """,
            [
                (ent.entity_id, ent.entity_type, ent.name, json.dumps(ent.aliases, ensure_ascii=False),
                 ent.source_record_key, ent.confidence, str(ent.sensitivity), now, now)
                for ent in entities
            ],
        )
        con.executemany(
            """
            INSERT INTO knowledge_claims(claim_id, subject, predicate, object, summary,
                                         details_json, source_record_key, source_basis, confidence,
                                         sensitivity, status, do_not_overgeneralise, tags_json,
                                         created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id) DO UPDATE SET
                confidence=max(knowledge_claims.confidence, excluded.confidence),
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            [
                (c.claim_id, c.subject, c.predicate, c.object, c.summary,
                 json.dumps(c.details, ensure_ascii=False, sort_keys=True, default=str), c.source_record_key,
                 str(c.source_basis), c.confidence, str(c.sensitivity), str(c.status),
                 1 if c.do_not_overgeneralise else 0, json.dumps(c.tags, ensure_ascii=False), c.created_at, now)
                for c in claims
            ],
        )
        con.executemany(
            """
            INSERT INTO knowledge_edges(edge_id, source_id, relation, target_id, claim_id,
                                        confidence, sensitivity, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_id) DO UPDATE SET
                confidence=max(knowledge_edges.confidence, excluded.confidence),
                updated_at=excluded.updated_at
            """,
            edge_rows,
        )
    return len(edge_rows)


def _bulk_insert_compiled(memory_candidates, applied_at_by_id, persona_rules, nudges, insights, safety_rules, reviews) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.executemany(
            """
            INSERT INTO knowledge_memory_candidates(memory_id, fact, source_claim_id, status,
                                                    reason, scope, sensitivity, created_at, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET status=excluded.status, applied_at=COALESCE(excluded.applied_at, knowledge_memory_candidates.applied_at)
            """,
            [(m.memory_id, m.fact, m.source_claim_id, str(m.status), m.reason, m.scope,
              str(m.sensitivity), now, applied_at_by_id.get(m.memory_id)) for m in memory_candidates],
        )
        con.executemany(
            """
            INSERT OR REPLACE INTO knowledge_persona_rules(rule_id, rule_type, text, source_claim_id,
                                                           priority, sensitivity, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(r.rule_id, r.rule_type, r.text, r.source_claim_id, r.priority, str(r.sensitivity), str(r.status), now) for r in persona_rules],
        )
        con.executemany(
            """
            INSERT INTO knowledge_nudge_candidates(nudge_id, domain, title, trigger,
                                                   suggested_message, source_claim_id,
                                                   care_policy_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nudge_id) DO UPDATE SET suggested_message=excluded.suggested_message,
                care_policy_json=excluded.care_policy_json, updated_at=excluded.updated_at
            """,
            [(n.nudge_id, n.domain, n.title, n.trigger, n.suggested_message, n.source_claim_id,
              n.care_policy.model_dump_json(), str(n.status), now, now) for n in nudges],
        )
        con.executemany(
            """
            INSERT INTO insight_candidates(insight_id, domain, title, hypothesis, confidence,
                                           actionability, risk_level, research_status,
                                           suggested_action_json, care_policy_json, status,
                                           source_claim_ids_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(insight_id) DO UPDATE SET confidence=max(insight_candidates.confidence, excluded.confidence),
                research_status=excluded.research_status, updated_at=excluded.updated_at
            """,
            [(i.insight_id, i.domain, i.title, i.hypothesis, i.confidence, i.actionability,
              i.risk_level, i.research_status, json.dumps(i.suggested_action, ensure_ascii=False, sort_keys=True, default=str),
              i.care_policy.model_dump_json(), str(i.status), json.dumps(i.supporting_claim_ids, ensure_ascii=False), now, now)
             for i in insights],
        )
        con.executemany(
            """
            INSERT OR REPLACE INTO knowledge_safety_rules(rule_id, domain, text, severity,
                                                          source_claim_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(rule_id, domain, text, severity, source_claim_id, "accepted", now) for rule_id, domain, text, severity, source_claim_id in safety_rules],
        )
        con.executemany(
            """
            INSERT OR IGNORE INTO knowledge_review_queue(review_id, object_type, object_id,
                                                         reason, severity, source_record_key,
                                                         status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            [(r.review_id, r.object_type, r.object_id, r.reason, r.severity, r.source_record_key, r.created_at) for r in reviews],
        )

def import_bulk_dump(path: str | Path, *, apply_accepted_memory: bool = True) -> ImportReport:
    p = Path(path)
    data = p.read_bytes()
    return import_bulk_dump_bytes(data, source_name=p.name, apply_accepted_memory=apply_accepted_memory)


def import_bulk_dump_bytes(data: bytes, *, source_name: str = "upload.zip",
                           apply_accepted_memory: bool = True) -> ImportReport:
    if looks_like_thread_dump_zip(data, source_name):
        parsed = parse_package_bytes(data, source_name=source_name)
        return _import_parsed(parsed, apply_accepted_memory=apply_accepted_memory)
    if looks_like_mongoose_archive(data, source_name):
        return import_archive_bytes(data, source_name=source_name, apply_accepted_memory=apply_accepted_memory)
    try:
        legacy = normalise_legacy_package_bytes(data, source_name=source_name)
        return _import_parsed(legacy.parsed, apply_accepted_memory=apply_accepted_memory)
    except KnowledgeImportError:
        # Re-raise the strict parser's clearer error for ordinary unsupported files.
        parsed = parse_package_bytes(data, source_name=source_name)
        return _import_parsed(parsed, apply_accepted_memory=apply_accepted_memory)


def _import_parsed(parsed, *, apply_accepted_memory: bool, run_post_import: bool = True) -> ImportReport:
    ensure_tables()
    errors: list[str] = []
    warnings = list(parsed.warnings)
    insert_import(
        parsed.import_id,
        package_type=parsed.package_type,
        source_name=parsed.source_name,
        source_sha256=parsed.source_sha256,
        metadata=parsed.metadata,
    )
    raw_count = 0
    for path, body, text in parsed.raw_files:
        fid = make_id("raw", parsed.import_id, path, hashlib.sha256(body).hexdigest())
        insert_raw_file(fid, parsed.import_id, path, hashlib.sha256(body).hexdigest(), len(body), text)
        raw_count += 1

    all_entities = []
    indexed_records = []
    for i, record in enumerate(parsed.records, start=1):
        indexed_records.append((i, record))
        all_entities.extend(resolve_entities(record))

    claims = build_claims(parsed.records)
    edges_created = _bulk_insert_records_entities_claims_edges(parsed.import_id, indexed_records, all_entities, claims)
    if edges_created:
        warnings.append(f"knowledge graph edges materialised: {edges_created}")
    if run_post_import:
        fusion_summary = run_entity_fusion()
        if fusion_summary.get("fusions"):
            warnings.append(f"entity fusion created {fusion_summary.get('fusions')} canonical links")
        suggestion_summary = suggest_entity_fusions(limit=60, use_llm=False)
        if suggestion_summary.get("created"):
            warnings.append(f"entity fusion suggestions queued for review: {suggestion_summary.get('created')}")

    memory_candidates = compile_memory_candidates(claims)
    applied_memory = 0
    applied_at_by_id: dict[str, str | None] = {}
    for candidate in memory_candidates:
        applied_at = None
        if apply_accepted_memory and candidate.status == ImportStatus.accepted:
            res = remember_fact(candidate.fact, source="knowledge_import", scope=OWNER_SCOPE)
            if res.get("stored") or res.get("reason") == "duplicate":
                applied_at = utc_now_iso()
                applied_memory += 1 if res.get("stored") else 0
        applied_at_by_id[candidate.memory_id] = applied_at

    persona_rules = compile_persona_rules(claims)
    nudges = compile_nudge_candidates(claims)
    insights = compile_insight_candidates(claims)
    safety_rules = compile_safety_rules(claims)
    reviews = compile_review_items(claims, memory_candidates, insights)
    _bulk_insert_compiled(memory_candidates, applied_at_by_id, persona_rules, nudges, insights, safety_rules, reviews)

    if run_post_import:
        synthesis_summary = run_synthesis_cycle(use_llm=False, limit=30)
        if synthesis_summary.get("inserted"):
            warnings.append(f"knowledge synthesis cards created: {synthesis_summary.get('inserted')}")

    if run_post_import:
        try:
            from .reflection import run_reflection_cycle
            refl = run_reflection_cycle("import", queue_research=True, activate_low_risk=True, limit=20)
            warnings.append(
                f"post-import reflection queued {len(refl.get('research_queued') or [])} research requests and activated {len(refl.get('low_risk_activated') or [])} low-risk care plans"
            )
        except Exception as exc:
            warnings.append(f"post-import reflection skipped: {exc}")

    return build_report(
        parsed.import_id,
        parsed.package_type,
        parsed.metadata,
        records_seen=len(parsed.records),
        records_imported=len(parsed.records),
        raw_files_preserved=raw_count,
        claims_created=len(claims),
        memories_created=applied_memory,
        persona_rules=len(persona_rules),
        nudges=len(nudges),
        insights=len(insights),
        safety_rules=len(safety_rules),
        review_items=len(reviews),
        errors=errors,
        warnings=warnings,
    )


def handle_possible_import(filename: str, data: bytes) -> dict[str, Any] | None:
    """Return an import result when the upload is a known knowledge package/archive, otherwise None."""
    try:
        if not looks_like_mongoose_archive(data, filename):
            return None
        report = import_bulk_dump_bytes(data, source_name=filename)
        lane = "knowledge_archive" if report.package_type == "knowledge_archive_bundle" else "knowledge_import"
        return {"ok": report.ok, "lane": lane, **report.model_dump()}
    except KnowledgeImportError as exc:
        return {"ok": False, "lane": "knowledge_import", "error": str(exc)}


def list_imports(limit: int = 20) -> dict[str, Any]:
    return {"ok": True, "imports": recent_imports(limit=limit)}


def describe_import(import_id: str) -> dict[str, Any]:
    return import_summary(import_id)
