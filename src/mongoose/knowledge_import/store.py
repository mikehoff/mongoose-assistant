from __future__ import annotations

import json
from typing import Any

from ..db import connect, utc_now_iso

_KNOWLEDGE_TABLES_READY: set[str] = set()
from .schemas import (
    Claim,
    Entity,
    ImportReport,
    InsightCandidate,
    MemoryCandidate,
    NudgeCandidate,
    PersonaRule,
    ReviewItem,
    ThreadMetadata,
    ThreadRecord,
)


def ensure_tables() -> None:
    # The knowledge importer can insert thousands of records from recursive archives. Re-running the
    # full CREATE TABLE script before every row made large imports painfully slow. Cache per DB path,
    # while still respecting tests/installs that switch MONGOOSE_DB_PATH during the same process.
    from ..settings import get_settings
    key = str(get_settings().db_path)
    if key in _KNOWLEDGE_TABLES_READY:
        return
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_imports (
                import_id TEXT PRIMARY KEY,
                package_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                thread_title TEXT NOT NULL DEFAULT '',
                sensitivity TEXT NOT NULL DEFAULT 'mixed',
                status TEXT NOT NULL DEFAULT 'imported',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_import_files (
                id TEXT PRIMARY KEY,
                import_id TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                content_text TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_records (
                record_key TEXT PRIMARY KEY,
                import_id TEXT NOT NULL,
                record_index INTEGER NOT NULL,
                record_type TEXT NOT NULL,
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                source_basis TEXT NOT NULL,
                confidence REAL NOT NULL,
                sensitivity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'accepted',
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_entities (
                entity_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                name TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                source_record_key TEXT,
                confidence REAL NOT NULL DEFAULT 0.7,
                sensitivity TEXT NOT NULL DEFAULT 'medium',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_claims (
                claim_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                summary TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                source_record_key TEXT NOT NULL,
                source_basis TEXT NOT NULL,
                confidence REAL NOT NULL,
                sensitivity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate',
                do_not_overgeneralise INTEGER NOT NULL DEFAULT 0,
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_memory_candidates (
                memory_id TEXT PRIMARY KEY,
                fact TEXT NOT NULL,
                source_claim_id TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'owner',
                sensitivity TEXT NOT NULL DEFAULT 'medium',
                created_at TEXT NOT NULL,
                applied_at TEXT
            );

            CREATE TABLE IF NOT EXISTS knowledge_persona_rules (
                rule_id TEXT PRIMARY KEY,
                rule_type TEXT NOT NULL,
                text TEXT NOT NULL,
                source_claim_id TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 50,
                sensitivity TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_nudge_candidates (
                nudge_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                title TEXT NOT NULL,
                trigger TEXT NOT NULL,
                suggested_message TEXT NOT NULL,
                source_claim_id TEXT NOT NULL,
                care_policy_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS insight_candidates (
                insight_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                title TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                confidence REAL NOT NULL,
                actionability TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                research_status TEXT NOT NULL,
                suggested_action_json TEXT NOT NULL,
                care_policy_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate',
                source_claim_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_safety_rules (
                rule_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                text TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                source_claim_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'accepted',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_review_queue (
                review_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                source_record_key TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS research_requests (
                request_id TEXT PRIMARY KEY,
                insight_id TEXT,
                topic TEXT NOT NULL,
                question TEXT NOT NULL,
                domain TEXT NOT NULL,
                source_policy TEXT NOT NULL,
                time_sensitivity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS insight_feedback (
                feedback_id TEXT PRIMARY KEY,
                insight_id TEXT NOT NULL,
                feedback TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_edges (
                edge_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                target_id TEXT NOT NULL,
                claim_id TEXT,
                confidence REAL NOT NULL DEFAULT 0.7,
                sensitivity TEXT NOT NULL DEFAULT 'medium',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_entity_fusions (
                fusion_id TEXT PRIMARY KEY,
                canonical_entity_id TEXT NOT NULL,
                duplicate_entity_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                status TEXT NOT NULL DEFAULT 'active',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_care_plans (
                plan_id TEXT PRIMARY KEY,
                insight_id TEXT NOT NULL,
                domain TEXT NOT NULL,
                title TEXT NOT NULL,
                tone TEXT NOT NULL DEFAULT 'warm_precise',
                cadence TEXT NOT NULL DEFAULT 'contextual',
                status TEXT NOT NULL DEFAULT 'active',
                activation_level TEXT NOT NULL DEFAULT 'gentle',
                message_template TEXT NOT NULL DEFAULT '',
                last_prompted_at TEXT,
                next_prompt_after TEXT,
                helpful_count INTEGER NOT NULL DEFAULT 0,
                ignored_count INTEGER NOT NULL DEFAULT 0,
                too_much_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_reflection_runs (
                run_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_activation_log (
                event_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT 'system',
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_entity_fusion_suggestions (
                suggestion_id TEXT PRIMARY KEY,
                canonical_entity_id TEXT,
                canonical_name TEXT NOT NULL,
                canonical_type TEXT NOT NULL,
                duplicate_entity_id TEXT NOT NULL,
                duplicate_name TEXT NOT NULL,
                duplicate_type TEXT NOT NULL,
                strategy TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'open',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS knowledge_synthesis_cards (
                synthesis_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                actionability TEXT NOT NULL DEFAULT 'medium',
                risk_level TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'candidate',
                connected_claim_ids_json TEXT NOT NULL DEFAULT '[]',
                connected_entity_ids_json TEXT NOT NULL DEFAULT '[]',
                care_angle TEXT NOT NULL DEFAULT '',
                suggested_action_json TEXT NOT NULL DEFAULT '{}',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_archive_items (
                item_id TEXT PRIMARY KEY,
                archive_import_id TEXT NOT NULL,
                path TEXT NOT NULL,
                item_type TEXT NOT NULL,
                status TEXT NOT NULL,
                child_import_id TEXT,
                records INTEGER NOT NULL DEFAULT 0,
                claims INTEGER NOT NULL DEFAULT 0,
                review_items INTEGER NOT NULL DEFAULT 0,
                raw_files INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_knowledge_claims_pred ON knowledge_claims(predicate, object);
            CREATE INDEX IF NOT EXISTS idx_knowledge_claims_record ON knowledge_claims(source_record_key);
            CREATE INDEX IF NOT EXISTS idx_knowledge_records_type ON knowledge_records(record_type);
            CREATE INDEX IF NOT EXISTS idx_insight_candidates_domain ON insight_candidates(domain, status);
            CREATE INDEX IF NOT EXISTS idx_review_queue_status ON knowledge_review_queue(status, severity);
            CREATE INDEX IF NOT EXISTS idx_knowledge_edges_src ON knowledge_edges(source_id, relation);
            CREATE INDEX IF NOT EXISTS idx_knowledge_edges_target ON knowledge_edges(target_id, relation);
            CREATE INDEX IF NOT EXISTS idx_entity_fusions_canonical ON knowledge_entity_fusions(canonical_entity_id, status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_fusions_pair ON knowledge_entity_fusions(canonical_entity_id, duplicate_entity_id);
            CREATE INDEX IF NOT EXISTS idx_care_plans_status ON knowledge_care_plans(status, domain, updated_at);
            CREATE INDEX IF NOT EXISTS idx_knowledge_reflection_kind ON knowledge_reflection_runs(kind, created_at);
            CREATE INDEX IF NOT EXISTS idx_activation_object ON knowledge_activation_log(object_type, object_id);
            CREATE INDEX IF NOT EXISTS idx_fusion_suggestion_status ON knowledge_entity_fusion_suggestions(status, confidence);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fusion_suggestion_pair ON knowledge_entity_fusion_suggestions(canonical_name, duplicate_entity_id, strategy);
            CREATE INDEX IF NOT EXISTS idx_synthesis_cards_status ON knowledge_synthesis_cards(status, domain, confidence);
            CREATE INDEX IF NOT EXISTS idx_archive_items_archive ON knowledge_archive_items(archive_import_id, status, item_type);
            """
        )
    _KNOWLEDGE_TABLES_READY.add(key)


def insert_import(import_id: str, *, package_type: str, source_name: str,
                  source_sha256: str, metadata: ThreadMetadata) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_imports(import_id, package_type, source_name, source_sha256,
                                          metadata_json, thread_title, sensitivity, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'imported', ?)
            ON CONFLICT(import_id) DO UPDATE SET status='imported', metadata_json=excluded.metadata_json
            """,
            (
                import_id,
                package_type,
                source_name,
                source_sha256,
                metadata.model_dump_json(),
                metadata.thread_title,
                metadata.sensitivity_level,
                now,
            ),
        )


def insert_raw_file(file_id: str, import_id: str, path: str, sha256: str,
                    size_bytes: int, content_text: str | None) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO knowledge_import_files(id, import_id, path, sha256,
                                                          size_bytes, content_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (file_id, import_id, path, sha256, size_bytes, content_text, now),
        )


def insert_archive_item(archive_import_id: str, item: dict[str, Any]) -> None:
    """Record how a recursive archive member was classified.

    This keeps code/build artifacts, strict dumps, markdown-normalised dumps and skipped items visible
    without polluting owner memory.
    """
    ensure_tables()
    from .schemas import make_id
    path = str(item.get("path") or "")
    item_type = str(item.get("item_type") or "unknown")
    status = str(item.get("status") or "unknown")
    item_id = make_id("archiveitem", archive_import_id, path, item_type)
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_archive_items(
                item_id, archive_import_id, path, item_type, status, child_import_id,
                records, claims, review_items, raw_files, message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                status=excluded.status,
                child_import_id=excluded.child_import_id,
                records=excluded.records,
                claims=excluded.claims,
                review_items=excluded.review_items,
                raw_files=excluded.raw_files,
                message=excluded.message,
                updated_at=excluded.updated_at
            """,
            (
                item_id, archive_import_id, path, item_type, status, item.get("import_id"),
                int(item.get("records") or 0), int(item.get("claims") or 0),
                int(item.get("review") or item.get("review_items") or 0),
                int(item.get("raw_files") or 0), str(item.get("message") or "")[:1000], now, now,
            ),
        )


def list_archive_items(archive_import_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    ensure_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if archive_import_id:
        clauses.append("archive_import_id=?")
        params.append(archive_import_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM knowledge_archive_items{where} ORDER BY created_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_record(record: ThreadRecord, *, import_id: str, index: int,
                  status: str = "accepted") -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO knowledge_records(record_key, import_id, record_index, record_type,
                                                     topic, summary, source_basis, confidence,
                                                     sensitivity, status, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.stable_record_key(),
                import_id,
                index,
                str(record.record_type),
                record.topic,
                record.summary,
                str(record.source_basis),
                record.confidence,
                str(record.sensitivity),
                status,
                record.model_dump_json(),
                now,
            ),
        )


def insert_entity(entity: Entity) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
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
            (
                entity.entity_id,
                entity.entity_type,
                entity.name,
                json.dumps(entity.aliases, ensure_ascii=False),
                entity.source_record_key,
                entity.confidence,
                str(entity.sensitivity),
                now,
                now,
            ),
        )


def insert_claim(claim: Claim) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
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
            (
                claim.claim_id,
                claim.subject,
                claim.predicate,
                claim.object,
                claim.summary,
                json.dumps(claim.details, ensure_ascii=False, sort_keys=True, default=str),
                claim.source_record_key,
                str(claim.source_basis),
                claim.confidence,
                str(claim.sensitivity),
                str(claim.status),
                1 if claim.do_not_overgeneralise else 0,
                json.dumps(claim.tags, ensure_ascii=False),
                claim.created_at,
                now,
            ),
        )


def insert_memory_candidate(candidate: MemoryCandidate, *, applied_at: str | None = None) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_memory_candidates(memory_id, fact, source_claim_id, status,
                                                    reason, scope, sensitivity, created_at, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET status=excluded.status, applied_at=COALESCE(excluded.applied_at, knowledge_memory_candidates.applied_at)
            """,
            (
                candidate.memory_id,
                candidate.fact,
                candidate.source_claim_id,
                str(candidate.status),
                candidate.reason,
                candidate.scope,
                str(candidate.sensitivity),
                now,
                applied_at,
            ),
        )


def insert_persona_rule(rule: PersonaRule) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO knowledge_persona_rules(rule_id, rule_type, text, source_claim_id,
                                                           priority, sensitivity, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rule.rule_id, rule.rule_type, rule.text, rule.source_claim_id, rule.priority,
             str(rule.sensitivity), str(rule.status), now),
        )


def insert_nudge(candidate: NudgeCandidate) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_nudge_candidates(nudge_id, domain, title, trigger,
                                                   suggested_message, source_claim_id,
                                                   care_policy_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nudge_id) DO UPDATE SET suggested_message=excluded.suggested_message,
                care_policy_json=excluded.care_policy_json, updated_at=excluded.updated_at
            """,
            (
                candidate.nudge_id,
                candidate.domain,
                candidate.title,
                candidate.trigger,
                candidate.suggested_message,
                candidate.source_claim_id,
                candidate.care_policy.model_dump_json(),
                str(candidate.status),
                now,
                now,
            ),
        )


def insert_insight(candidate: InsightCandidate) -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO insight_candidates(insight_id, domain, title, hypothesis, confidence,
                                           actionability, risk_level, research_status,
                                           suggested_action_json, care_policy_json, status,
                                           source_claim_ids_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(insight_id) DO UPDATE SET confidence=max(insight_candidates.confidence, excluded.confidence),
                research_status=excluded.research_status, updated_at=excluded.updated_at
            """,
            (
                candidate.insight_id,
                candidate.domain,
                candidate.title,
                candidate.hypothesis,
                candidate.confidence,
                candidate.actionability,
                candidate.risk_level,
                candidate.research_status,
                json.dumps(candidate.suggested_action, ensure_ascii=False, sort_keys=True, default=str),
                candidate.care_policy.model_dump_json(),
                str(candidate.status),
                json.dumps(candidate.supporting_claim_ids, ensure_ascii=False),
                now,
                now,
            ),
        )


def insert_safety_rule(rule_id: str, domain: str, text: str, severity: str,
                       source_claim_id: str, status: str = "accepted") -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO knowledge_safety_rules(rule_id, domain, text, severity,
                                                          source_claim_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (rule_id, domain, text, severity, source_claim_id, status, now),
        )


def insert_review(item: ReviewItem) -> None:
    ensure_tables()
    with connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO knowledge_review_queue(review_id, object_type, object_id,
                                                         reason, severity, source_record_key,
                                                         status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (item.review_id, item.object_type, item.object_id, item.reason, item.severity,
             item.source_record_key, item.created_at),
        )


def recent_imports(limit: int = 20) -> list[dict[str, Any]]:
    ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM knowledge_imports ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(r) for r in rows]


def import_summary(import_id: str) -> dict[str, Any]:
    ensure_tables()
    with connect() as con:
        imp = con.execute("SELECT * FROM knowledge_imports WHERE import_id=?", (import_id,)).fetchone()
        if not imp:
            return {"ok": False, "error": "unknown import"}
        record_keys = [r["record_key"] for r in con.execute("SELECT record_key FROM knowledge_records WHERE import_id=?", (import_id,)).fetchall()]
        claim_ids: list[str] = []
        if record_keys:
            marks = ",".join("?" for _ in record_keys)
            claim_ids = [r["claim_id"] for r in con.execute(f"SELECT claim_id FROM knowledge_claims WHERE source_record_key IN ({marks})", record_keys).fetchall()]
        counts = {
            "records": len(record_keys),
            "claims": len(claim_ids),
            "raw_files": con.execute("SELECT COUNT(*) c FROM knowledge_import_files WHERE import_id=?", (import_id,)).fetchone()["c"],
        }
        if claim_ids:
            marks = ",".join("?" for _ in claim_ids)
            counts["memories"] = con.execute(f"SELECT COUNT(*) c FROM knowledge_memory_candidates WHERE source_claim_id IN ({marks})", claim_ids).fetchone()["c"]
            counts["persona_rules"] = con.execute(f"SELECT COUNT(*) c FROM knowledge_persona_rules WHERE source_claim_id IN ({marks})", claim_ids).fetchone()["c"]
            counts["nudges"] = con.execute(f"SELECT COUNT(*) c FROM knowledge_nudge_candidates WHERE source_claim_id IN ({marks})", claim_ids).fetchone()["c"]
            counts["safety_rules"] = con.execute(f"SELECT COUNT(*) c FROM knowledge_safety_rules WHERE source_claim_id IN ({marks})", claim_ids).fetchone()["c"]
            counts["reviews"] = con.execute(f"SELECT COUNT(*) c FROM knowledge_review_queue WHERE source_record_key IN ({','.join('?' for _ in record_keys)})", record_keys).fetchone()["c"]
            # Insight rows hold source ids as JSON. Count deterministically in Python to avoid SQLite JSON dependency.
            insight_rows = con.execute("SELECT source_claim_ids_json FROM insight_candidates").fetchall()
            counts["insights"] = sum(1 for row in insight_rows if any(cid in (row["source_claim_ids_json"] or "") for cid in claim_ids))
            edge_rows = con.execute("SELECT claim_id FROM knowledge_edges WHERE claim_id IS NOT NULL").fetchall()
            counts["edges"] = sum(1 for row in edge_rows if row["claim_id"] in set(claim_ids))
        else:
            counts.update({"memories": 0, "persona_rules": 0, "nudges": 0, "safety_rules": 0, "reviews": 0, "insights": 0, "edges": 0})
    return {"ok": True, "import": dict(imp), "counts": counts}


def build_report(import_id: str, package_type: str, metadata: ThreadMetadata,
                 *, records_seen: int, records_imported: int, raw_files_preserved: int,
                 claims_created: int, memories_created: int, persona_rules: int,
                 nudges: int, insights: int, safety_rules: int,
                 review_items: int, errors: list[str], warnings: list[str]) -> ImportReport:
    return ImportReport(
        ok=not errors,
        import_id=import_id,
        package_type=package_type,
        thread_title=metadata.thread_title,
        records_seen=records_seen,
        records_imported=records_imported,
        records_needing_review=review_items,
        claims_created=claims_created,
        canonical_memories_created=memories_created,
        persona_rules_created=persona_rules,
        scheduler_candidates_created=nudges,
        insight_candidates_created=insights,
        safety_rules_created=safety_rules,
        raw_files_preserved=raw_files_preserved,
        errors=errors,
        warnings=warnings,
    )


def insert_edge(edge_id: str, source_id: str, relation: str, target_id: str, *,
                claim_id: str | None = None, confidence: float = 0.7,
                sensitivity: str = "medium") -> None:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_edges(edge_id, source_id, relation, target_id, claim_id,
                                        confidence, sensitivity, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_id) DO UPDATE SET
                confidence=max(knowledge_edges.confidence, excluded.confidence),
                updated_at=excluded.updated_at
            """,
            (edge_id, source_id, relation, target_id, claim_id, float(confidence), sensitivity, now, now),
        )


def log_activation(object_type: str, object_id: str, action: str, *, reason: str = "",
                   actor: str = "system", context: dict[str, Any] | None = None) -> str:
    ensure_tables()
    from .schemas import make_id
    eid = make_id("act", object_type, object_id, action, utc_now_iso())
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_activation_log(event_id, object_type, object_id, action,
                                                 reason, actor, context_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (eid, object_type, object_id, action, reason, actor,
             json.dumps(context or {}, ensure_ascii=False, sort_keys=True, default=str), utc_now_iso()),
        )
    return eid


def list_claims(*, domain: str | None = None, status: str | None = None,
                limit: int = 50) -> list[dict[str, Any]]:
    ensure_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if domain:
        # Domain is not stored directly on claims; infer from predicate. Keep deterministic.
        if domain == "body_wellness":
            clauses.append("predicate IN ('has_body_signal','has_posture_pattern','uses_activity','uses_wellness_routine','has_health_context')")
        elif domain == "rtm":
            clauses.append("predicate IN ('has_legal_or_rtm_context','has_financial_or_accounting_rule')")
        elif domain == "project":
            clauses.append("predicate IN ('has_project_requirement','has_architecture_decision','reported_bug','has_fix_or_patch')")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM knowledge_claims{where} ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def list_review_queue(*, status: str = "open", severity: str | None = None,
                      limit: int = 50) -> list[dict[str, Any]]:
    ensure_tables()
    clauses = ["status=?"]
    params: list[Any] = [status]
    if severity:
        clauses.append("severity=?")
        params.append(severity)
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM knowledge_review_queue WHERE {' AND '.join(clauses)} ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def dashboard(limit: int = 12) -> dict[str, Any]:
    ensure_tables()
    reviews = list_review_queue(limit=limit)
    with connect() as con:
        def count(table: str, where: str = "1=1") -> int:
            try:
                return int(con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])
            except Exception:
                return 0
        imports = [dict(r) for r in con.execute(
            "SELECT import_id, source_name, thread_title, sensitivity, created_at FROM knowledge_imports ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()]
        insights = [dict(r) for r in con.execute(
            "SELECT insight_id, domain, title, confidence, actionability, risk_level, research_status, status FROM insight_candidates ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()]
        research = [dict(r) for r in con.execute(
            "SELECT request_id, insight_id, topic, domain, source_policy, status, created_at FROM research_requests ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()]
        counts = {
            "imports": count("knowledge_imports"),
            "records": count("knowledge_records"),
            "claims": count("knowledge_claims"),
            "entities": count("knowledge_entities"),
            "edges": count("knowledge_edges"),
            "memories": count("knowledge_memory_candidates"),
            "persona_rules": count("knowledge_persona_rules"),
            "nudges": count("knowledge_nudge_candidates"),
            "insights": count("insight_candidates"),
            "review_open": count("knowledge_review_queue", "status='open'"),
            "research_queued": count("research_requests", "status IN ('queued','failed')"),
            "safety_rules": count("knowledge_safety_rules"),
            "fusions": count("knowledge_entity_fusions", "status='active'"),
            "care_active": count("knowledge_care_plans", "status='active'"),
            "fusion_suggestions_open": count("knowledge_entity_fusion_suggestions", "status='open'"),
            "synthesis_cards": count("knowledge_synthesis_cards"),
            "archive_items": count("knowledge_archive_items"),
            "archive_imported_children": count("knowledge_archive_items", "status='imported'"),
            "archive_build_artifacts": count("knowledge_archive_items", "item_type='mongoose_build_artifact'"),
        }
        fusions = [dict(r) for r in con.execute(
            """
            SELECT f.fusion_id, f.canonical_entity_id, f.duplicate_entity_id, f.strategy,
                   f.confidence, f.created_at, c.name AS canonical_name, d.name AS duplicate_name
            FROM knowledge_entity_fusions f
            LEFT JOIN knowledge_entities c ON c.entity_id=f.canonical_entity_id
            LEFT JOIN knowledge_entities d ON d.entity_id=f.duplicate_entity_id
            WHERE f.status='active'
            ORDER BY f.confidence DESC, f.updated_at DESC LIMIT ?
            """,
            (max(1, min(limit, 50)),),
        ).fetchall()]
        care_plans = [dict(r) for r in con.execute(
            "SELECT plan_id, insight_id, domain, title, tone, cadence, status, activation_level, helpful_count, too_much_count, updated_at FROM knowledge_care_plans ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()]
        fusion_suggestions = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_entity_fusion_suggestions WHERE status='open' ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()]
        synthesis_cards = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_synthesis_cards ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()]
        archive_items = [dict(r) for r in con.execute(
            "SELECT * FROM knowledge_archive_items ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 80)),),
        ).fetchall()]
    return {
        "ok": True,
        "counts": counts,
        "recent_imports": imports,
        "insights": insights,
        "review": reviews,
        "research": research,
        "fusions": fusions,
        "fusion_suggestions": fusion_suggestions,
        "synthesis_cards": synthesis_cards,
        "care_plans": care_plans,
        "archive_items": archive_items,
    }


def search_knowledge(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    ensure_tables()
    q = (query or "").strip()
    if not q:
        return []
    terms = [t.lower() for t in q.split() if len(t) > 1]
    like = f"%{q.lower()}%"
    out: list[dict[str, Any]] = []
    with connect() as con:
        # Broad first pass by phrase; if no hits, a token-scored fallback below still helps.
        for table, kind, id_col, title_col, body_col, extra_cols in [
            ("knowledge_claims", "knowledge_claim", "claim_id", "object", "summary", "predicate, confidence, sensitivity, status, updated_at"),
            ("insight_candidates", "knowledge_insight", "insight_id", "title", "hypothesis", "domain, confidence, risk_level, research_status, status, updated_at"),
            ("knowledge_memory_candidates", "knowledge_memory", "memory_id", "reason", "fact", "source_claim_id, sensitivity, status, created_at"),
            ("knowledge_persona_rules", "knowledge_persona", "rule_id", "rule_type", "text", "source_claim_id, priority, sensitivity, status, created_at"),
            ("knowledge_entities", "knowledge_entity", "entity_id", "name", "entity_type", "confidence, sensitivity, updated_at, created_at"),
            ("knowledge_care_plans", "knowledge_care_plan", "plan_id", "title", "message_template", "domain, status, activation_level, helpful_count, too_much_count, updated_at"),
            ("knowledge_synthesis_cards", "knowledge_synthesis", "synthesis_id", "title", "summary", "domain, confidence, actionability, risk_level, status, updated_at"),
            ("knowledge_entity_fusion_suggestions", "knowledge_fusion_suggestion", "suggestion_id", "canonical_name", "duplicate_name", "canonical_type, duplicate_type, strategy, confidence, status, updated_at"),
            ("knowledge_archive_items", "knowledge_archive_item", "item_id", "path", "message", "item_type, status, child_import_id, records, claims, review_items, created_at, updated_at"),
        ]:
            rows = con.execute(
                f"SELECT {id_col} AS id, {title_col} AS title, {body_col} AS body, {extra_cols} FROM {table} WHERE lower({title_col}) LIKE ? OR lower({body_col}) LIKE ? LIMIT ?",
                (like, like, max(limit * 3, 20)),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["kind"] = kind
                d["native_id"] = d.pop("id")
                d["title"] = str(d.get("title") or kind)
                d["snippet"] = str(d.pop("body") or "")[:300]
                d["where"] = "Mongoose knowledge graph"
                out.append(d)
        if terms:
            fallback_specs = [
                ("knowledge_claims", "knowledge_claim", "claim_id", "object", "summary", "predicate, confidence, sensitivity, status, updated_at"),
                ("knowledge_entities", "knowledge_entity", "entity_id", "name", "aliases_json", "entity_type, confidence, sensitivity, updated_at"),
                ("knowledge_care_plans", "knowledge_care_plan", "plan_id", "title", "message_template", "domain, status, activation_level, updated_at"),
                ("knowledge_synthesis_cards", "knowledge_synthesis", "synthesis_id", "title", "summary", "domain, confidence, status, updated_at"),
                ("knowledge_archive_items", "knowledge_archive_item", "item_id", "path", "message", "item_type, status, records, claims, updated_at"),
            ]
            existing = {(x.get("kind"), x.get("native_id")) for x in out}
            for table, kind, id_col, title_col, body_col, extra_cols in fallback_specs:
                rows = con.execute(
                    f"SELECT {id_col} AS native_id, {title_col} AS title, {body_col} AS snippet, {extra_cols} FROM {table} LIMIT 700"
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    if (kind, d.get("native_id")) in existing:
                        continue
                    hay = f"{d.get('title')} {d.get('snippet')} {d.get('predicate','')} {d.get('entity_type','')} {d.get('domain','')}".lower()
                    hits = sum(1 for t in terms if t in hay)
                    if hits:
                        d["kind"] = kind
                        d["where"] = "Mongoose knowledge graph"
                        d["token_hits"] = hits
                        if kind == "knowledge_entity":
                            try:
                                aliases = json.loads(str(d.get("snippet") or "[]"))
                                d["snippet"] = "aliases: " + ", ".join(str(a) for a in aliases[:8])
                            except Exception:
                                pass
                        out.append(d)
                        existing.add((kind, d.get("native_id")))
    def score(item: dict[str, Any]) -> tuple[float, str]:
        hay_title = str(item.get("title") or "").lower()
        hay = f"{hay_title} {item.get('snippet','')} {item.get('predicate','')} {item.get('domain','')}".lower()
        s = 2.0
        if q.lower() in hay_title:
            s += 50
        if q.lower() in hay:
            s += 20
        for t in terms:
            if t in hay_title:
                s += 8
            elif t in hay:
                s += 3
        try:
            s += float(item.get("confidence") or 0) * 5
        except Exception:
            pass
        return (s, str(item.get("updated_at") or item.get("created_at") or ""))
    out.sort(key=lambda r: score(r), reverse=True)
    return out[:max(1, min(limit, 100))]


def resolve_review_item(review_id: str, action: str, *, reason: str = "", actor: str = "owner") -> dict[str, Any]:
    """Approve/reject a queued imported object. Approval applies safe effects only.

    Approving a memory candidate stores it in durable memory. Approving an insight/nudge/persona/claim
    marks it accepted for later use. Rejecting marks both the review and object rejected when possible.
    """
    ensure_tables()
    action = (action or "").strip().lower()
    if action not in {"approve", "reject", "snooze", "needs_research", "keep_searchable"}:
        return {"ok": False, "error": "action must be approve/reject/snooze/needs_research/keep_searchable"}
    with connect() as con:
        row = con.execute("SELECT * FROM knowledge_review_queue WHERE review_id=?", (review_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown review item"}
        rowd = dict(row)
        obj_type = rowd["object_type"]
        obj_id = rowd["object_id"]
        new_status = "resolved" if action in {"approve", "reject", "keep_searchable"} else "open"
        if action == "approve":
            _approve_object(con, obj_type, obj_id)
        elif action == "reject":
            _reject_object(con, obj_type, obj_id)
        elif action == "needs_research":
            _mark_needs_research(con, obj_type, obj_id)
        elif action == "keep_searchable":
            _keep_searchable(con, obj_type, obj_id)
        con.execute(
            "UPDATE knowledge_review_queue SET status=?, resolved_at=? WHERE review_id=?",
            (new_status, utc_now_iso() if new_status == "resolved" else None, review_id),
        )
    event_id = log_activation("review_item", review_id, action, reason=reason, actor=actor,
                              context={"object_type": obj_type, "object_id": obj_id})
    return {"ok": True, "review_id": review_id, "action": action, "object_type": obj_type,
            "object_id": obj_id, "activation_event_id": event_id}


def _approve_object(con, obj_type: str, obj_id: str) -> None:
    if obj_type == "memory_candidate":
        row = con.execute("SELECT * FROM knowledge_memory_candidates WHERE memory_id=?", (obj_id,)).fetchone()
        if row:
            from ..services.memory import OWNER_SCOPE, remember_fact
            remember_fact(row["fact"], source="knowledge_import_review", scope=OWNER_SCOPE)
            con.execute("UPDATE knowledge_memory_candidates SET status='accepted', applied_at=? WHERE memory_id=?", (utc_now_iso(), obj_id))
    elif obj_type == "insight_candidate":
        con.execute("UPDATE insight_candidates SET status='accepted', updated_at=? WHERE insight_id=?", (utc_now_iso(), obj_id))
    elif obj_type == "claim":
        con.execute("UPDATE knowledge_claims SET status='accepted', updated_at=? WHERE claim_id=?", (utc_now_iso(), obj_id))
    elif obj_type == "nudge_candidate":
        con.execute("UPDATE knowledge_nudge_candidates SET status='accepted', updated_at=? WHERE nudge_id=?", (utc_now_iso(), obj_id))
    elif obj_type == "persona_rule":
        con.execute("UPDATE knowledge_persona_rules SET status='accepted' WHERE rule_id=?", (obj_id,))
    elif obj_type == "fusion_suggestion":
        resolve_fusion_suggestion(obj_id, "approve", actor="review")
    elif obj_type == "synthesis_card":
        con.execute("UPDATE knowledge_synthesis_cards SET status='accepted', updated_at=? WHERE synthesis_id=?", (utc_now_iso(), obj_id))


def _reject_object(con, obj_type: str, obj_id: str) -> None:
    table_key = {
        "memory_candidate": ("knowledge_memory_candidates", "memory_id"),
        "insight_candidate": ("insight_candidates", "insight_id"),
        "claim": ("knowledge_claims", "claim_id"),
        "nudge_candidate": ("knowledge_nudge_candidates", "nudge_id"),
        "persona_rule": ("knowledge_persona_rules", "rule_id"),
        "synthesis_card": ("knowledge_synthesis_cards", "synthesis_id"),
        "fusion_suggestion": ("knowledge_entity_fusion_suggestions", "suggestion_id"),
    }.get(obj_type)
    if table_key:
        table, key = table_key
        con.execute(f"UPDATE {table} SET status='rejected' WHERE {key}=?", (obj_id,))


def _mark_needs_research(con, obj_type: str, obj_id: str) -> None:
    if obj_type == "insight_candidate":
        con.execute("UPDATE insight_candidates SET research_status='needed', updated_at=? WHERE insight_id=?", (utc_now_iso(), obj_id))
    elif obj_type == "claim":
        con.execute("UPDATE knowledge_claims SET status='review', updated_at=? WHERE claim_id=?", (utc_now_iso(), obj_id))


def _keep_searchable(con, obj_type: str, obj_id: str) -> None:
    # Candidate state means searchable/retrievable but not active memory/advice.
    table_key = {
        "memory_candidate": ("knowledge_memory_candidates", "memory_id"),
        "insight_candidate": ("insight_candidates", "insight_id"),
        "claim": ("knowledge_claims", "claim_id"),
        "nudge_candidate": ("knowledge_nudge_candidates", "nudge_id"),
        "persona_rule": ("knowledge_persona_rules", "rule_id"),
        "synthesis_card": ("knowledge_synthesis_cards", "synthesis_id"),
        "fusion_suggestion": ("knowledge_entity_fusion_suggestions", "suggestion_id"),
    }.get(obj_type)
    if table_key:
        table, key = table_key
        con.execute(f"UPDATE {table} SET status='candidate' WHERE {key}=?", (obj_id,))


def insert_reflection_run(run_id: str, kind: str, status: str, summary: dict[str, Any]) -> None:
    ensure_tables()
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO knowledge_reflection_runs(run_id, kind, status, summary_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, kind, status, json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str), utc_now_iso()),
        )


def recent_reflections(limit: int = 20) -> list[dict[str, Any]]:
    ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM knowledge_reflection_runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_entity_fusion(canonical_entity_id: str, duplicate_entity_id: str, *,
                         strategy: str, confidence: float = 0.8,
                         evidence: dict[str, Any] | None = None) -> str:
    """Persist a canonical-entity fusion without deleting the source entity."""
    ensure_tables()
    fusion_id = __import__("mongoose.knowledge_import.schemas", fromlist=["make_id"]).make_id(
        "fusion", canonical_entity_id, duplicate_entity_id, strategy
    )
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_entity_fusions(fusion_id, canonical_entity_id, duplicate_entity_id,
                                                 strategy, confidence, status, evidence_json,
                                                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(fusion_id) DO UPDATE SET
                confidence=max(knowledge_entity_fusions.confidence, excluded.confidence),
                status='active',
                evidence_json=excluded.evidence_json,
                updated_at=excluded.updated_at
            """,
            (fusion_id, canonical_entity_id, duplicate_entity_id, strategy, float(confidence),
             json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True, default=str), now, now),
        )
    return fusion_id


def list_entity_fusions(limit: int = 50) -> list[dict[str, Any]]:
    ensure_tables()
    with connect() as con:
        rows = con.execute(
            """
            SELECT f.*, c.name AS canonical_name, c.entity_type AS canonical_type,
                   d.name AS duplicate_name, d.entity_type AS duplicate_type
            FROM knowledge_entity_fusions f
            LEFT JOIN knowledge_entities c ON c.entity_id=f.canonical_entity_id
            LEFT JOIN knowledge_entities d ON d.entity_id=f.duplicate_entity_id
            WHERE f.status='active'
            ORDER BY f.confidence DESC, f.updated_at DESC LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_care_plan(*, insight_id: str, domain: str, title: str,
                     tone: str = "warm_precise", cadence: str = "contextual",
                     activation_level: str = "gentle", status: str = "active",
                     message_template: str = "") -> dict[str, Any]:
    ensure_tables()
    from .schemas import make_id
    plan_id = make_id("careplan", insight_id, activation_level)
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_care_plans(plan_id, insight_id, domain, title, tone, cadence,
                                             status, activation_level, message_template,
                                             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
                tone=excluded.tone,
                cadence=excluded.cadence,
                status=excluded.status,
                activation_level=excluded.activation_level,
                message_template=excluded.message_template,
                updated_at=excluded.updated_at
            """,
            (plan_id, insight_id, domain, title, tone, cadence, status, activation_level,
             message_template, now, now),
        )
        row = con.execute("SELECT * FROM knowledge_care_plans WHERE plan_id=?", (plan_id,)).fetchone()
    log_activation("care_plan", plan_id, status, reason=f"care plan {status}", actor="owner",
                   context={"insight_id": insight_id, "activation_level": activation_level})
    return dict(row) if row else {"plan_id": plan_id, "insight_id": insight_id, "status": status}


def update_care_plan_status(plan_id: str, status: str, *, actor: str = "owner", reason: str = "") -> dict[str, Any]:
    ensure_tables()
    status = (status or "").strip().lower()
    if status not in {"active", "paused", "rejected", "snoozed"}:
        return {"ok": False, "error": "status must be active/paused/rejected/snoozed"}
    with connect() as con:
        row = con.execute("SELECT * FROM knowledge_care_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown care plan"}
        con.execute("UPDATE knowledge_care_plans SET status=?, updated_at=? WHERE plan_id=?",
                    (status, utc_now_iso(), plan_id))
    log_activation("care_plan", plan_id, status, reason=reason, actor=actor)
    return {"ok": True, "plan_id": plan_id, "status": status}


def graph_snapshot(limit: int = 60) -> dict[str, Any]:
    """A readable slice of the knowledge graph for the cockpit mini-visualisation: the most-connected
    entities plus their immediate neighbours, and the edges among that set. Capped so the SVG stays
    legible on a phone; this is orientation, not the whole graph."""
    ensure_tables()
    limit = max(6, min(int(limit), 120))
    hubs = max(4, limit // 3)
    with connect() as con:
        deg = con.execute(
            """
            SELECT e.entity_id,
                   (SELECT COUNT(*) FROM knowledge_edges k
                     WHERE k.source_id = e.entity_id OR k.target_id = e.entity_id) AS degree
            FROM knowledge_entities e
            ORDER BY degree DESC, e.updated_at DESC
            LIMIT ?
            """,
            (hubs,),
        ).fetchall()
        keep = {r["entity_id"] for r in deg}
        if not keep:
            return {"nodes": [], "edges": [], "truncated": False, "total_entities": 0}
        # Expand one hop around the hubs so the graph is connected, up to the node cap.
        ph = ",".join("?" for _ in keep)
        neigh = con.execute(
            f"""SELECT source_id, target_id FROM knowledge_edges
                WHERE source_id IN ({ph}) OR target_id IN ({ph}) LIMIT 600""",
            (*keep, *keep),
        ).fetchall()
        for r in neigh:
            if len(keep) >= limit:
                break
            keep.add(r["source_id"]); keep.add(r["target_id"])
        keep = set(list(keep)[:limit])
        ph2 = ",".join("?" for _ in keep)
        node_rows = con.execute(
            f"""SELECT e.entity_id, e.name, e.entity_type, e.sensitivity,
                   (SELECT COUNT(*) FROM knowledge_edges k
                     WHERE k.source_id = e.entity_id OR k.target_id = e.entity_id) AS degree
                FROM knowledge_entities e WHERE e.entity_id IN ({ph2})""",
            tuple(keep),
        ).fetchall()
        nodes = [{"id": r["entity_id"], "name": r["name"], "type": r["entity_type"],
                  "degree": r["degree"], "sensitivity": r["sensitivity"]} for r in node_rows]
        edge_rows = con.execute(
            f"""SELECT source_id, relation, target_id, confidence FROM knowledge_edges
                WHERE source_id IN ({ph2}) AND target_id IN ({ph2})
                ORDER BY confidence DESC LIMIT 400""",
            (*keep, *keep),
        ).fetchall()
        edges = [{"source": r["source_id"], "relation": r["relation"], "target": r["target_id"],
                  "confidence": round(float(r["confidence"] or 0), 2)} for r in edge_rows]
        total_nodes = con.execute("SELECT COUNT(*) c FROM knowledge_entities").fetchone()["c"]
    nodes.sort(key=lambda n: -n["degree"])
    return {"nodes": nodes, "edges": edges, "truncated": total_nodes > len(nodes),
            "total_entities": total_nodes}


def list_care_plans(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    ensure_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM knowledge_care_plans{where} ORDER BY updated_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def record_care_prompt(plan_id: str) -> None:
    ensure_tables()
    with connect() as con:
        con.execute("UPDATE knowledge_care_plans SET last_prompted_at=?, updated_at=? WHERE plan_id=?",
                    (utc_now_iso(), utc_now_iso(), plan_id))


def update_care_feedback_counts(insight_id: str, feedback: str) -> None:
    ensure_tables()
    feedback = (feedback or "").strip().lower()
    with connect() as con:
        rows = con.execute("SELECT plan_id FROM knowledge_care_plans WHERE insight_id=?", (insight_id,)).fetchall()
        for r in rows:
            if feedback in {"helpful", "more_like_this"}:
                con.execute("UPDATE knowledge_care_plans SET helpful_count=helpful_count+1, updated_at=? WHERE plan_id=?", (utc_now_iso(), r["plan_id"]))
            elif feedback in {"too_much", "less_like_this"}:
                con.execute("UPDATE knowledge_care_plans SET too_much_count=too_much_count+1, updated_at=? WHERE plan_id=?", (utc_now_iso(), r["plan_id"]))
            elif feedback in {"not_now"}:
                con.execute("UPDATE knowledge_care_plans SET ignored_count=ignored_count+1, updated_at=? WHERE plan_id=?", (utc_now_iso(), r["plan_id"]))
            elif feedback == "wrong":
                con.execute("UPDATE knowledge_care_plans SET status='paused', ignored_count=ignored_count+1, updated_at=? WHERE plan_id=?", (utc_now_iso(), r["plan_id"]))


def insert_fusion_suggestion(*, canonical_entity_id: str | None, canonical_name: str,
                             canonical_type: str, duplicate_entity_id: str,
                             duplicate_name: str, duplicate_type: str, strategy: str,
                             confidence: float, evidence: dict[str, Any] | None = None,
                             status: str = "open") -> str:
    """Store a proposed entity fusion for owner review.

    Suggestions are deliberately separate from active fusions: they do not change graph semantics until
    approved. This is where local-LLM or fuzzy suggestions land.
    """
    ensure_tables()
    from .schemas import make_id
    sid = make_id("fusionsuggest", canonical_name.lower(), duplicate_entity_id, strategy)
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_entity_fusion_suggestions(
                suggestion_id, canonical_entity_id, canonical_name, canonical_type,
                duplicate_entity_id, duplicate_name, duplicate_type, strategy,
                confidence, status, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(suggestion_id) DO UPDATE SET
                confidence=max(knowledge_entity_fusion_suggestions.confidence, excluded.confidence),
                status=CASE WHEN knowledge_entity_fusion_suggestions.status='open' THEN excluded.status ELSE knowledge_entity_fusion_suggestions.status END,
                evidence_json=excluded.evidence_json,
                updated_at=excluded.updated_at
            """,
            (sid, canonical_entity_id, canonical_name, canonical_type, duplicate_entity_id,
             duplicate_name, duplicate_type, strategy, float(confidence), status,
             json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True, default=str), now, now),
        )
    return sid


def list_fusion_suggestions(status: str | None = "open", limit: int = 30) -> list[dict[str, Any]]:
    ensure_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"""
            SELECT * FROM knowledge_entity_fusion_suggestions{where}
            ORDER BY confidence DESC, updated_at DESC LIMIT ?
            """,
            (*params, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_fusion_suggestion(suggestion_id: str, action: str, *, actor: str = "owner") -> dict[str, Any]:
    ensure_tables()
    action = (action or "").strip().lower()
    if action not in {"approve", "reject", "keep_open"}:
        return {"ok": False, "error": "action must be approve/reject/keep_open"}
    with connect() as con:
        row = con.execute("SELECT * FROM knowledge_entity_fusion_suggestions WHERE suggestion_id=?", (suggestion_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown fusion suggestion"}
        d = dict(row)
    if action == "approve":
        # Create or reuse a canonical entity, then make the actual active fusion.
        from .schemas import Entity, Sensitivity, make_id
        canonical_id = d.get("canonical_entity_id") or make_id("ent_suggested", d.get("canonical_type") or "entity", d.get("canonical_name") or "entity")
        insert_entity(Entity(
            entity_id=canonical_id,
            entity_type=str(d.get("canonical_type") or "entity"),
            name=str(d.get("canonical_name") or "entity"),
            aliases=[str(d.get("canonical_name") or "")],
            confidence=float(d.get("confidence") or 0.7),
            sensitivity=Sensitivity.medium,
        ))
        evidence = _json_load(d.get("evidence_json"), {})
        fusion_id = insert_entity_fusion(
            canonical_id,
            str(d.get("duplicate_entity_id") or ""),
            strategy=f"approved_{d.get('strategy') or 'suggestion'}",
            confidence=float(d.get("confidence") or 0.7),
            evidence={**evidence, "approved_suggestion_id": suggestion_id},
        )
        status = "approved"
        result = {"ok": True, "suggestion_id": suggestion_id, "action": action, "fusion_id": fusion_id, "canonical_entity_id": canonical_id}
    elif action == "reject":
        status = "rejected"
        result = {"ok": True, "suggestion_id": suggestion_id, "action": action}
    else:
        status = "open"
        result = {"ok": True, "suggestion_id": suggestion_id, "action": action}
    with connect() as con:
        con.execute(
            "UPDATE knowledge_entity_fusion_suggestions SET status=?, resolved_at=?, updated_at=? WHERE suggestion_id=?",
            (status, utc_now_iso() if status in {"approved", "rejected"} else None, utc_now_iso(), suggestion_id),
        )
    log_activation("fusion_suggestion", suggestion_id, action, actor=actor, reason="entity fusion suggestion resolved", context=result)
    return result


def _json_load(value: str | None, default):
    try:
        parsed = json.loads(value or "")
        return parsed if parsed is not None else default
    except Exception:
        return default


def insert_synthesis_card(*, synthesis_id: str, domain: str, title: str, summary: str,
                          confidence: float, actionability: str, risk_level: str,
                          connected_claim_ids: list[str], connected_entity_ids: list[str] | None = None,
                          care_angle: str = "", suggested_action: dict[str, Any] | None = None,
                          evidence: dict[str, Any] | None = None, status: str = "candidate") -> str:
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO knowledge_synthesis_cards(
                synthesis_id, domain, title, summary, confidence, actionability, risk_level,
                status, connected_claim_ids_json, connected_entity_ids_json, care_angle,
                suggested_action_json, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(synthesis_id) DO UPDATE SET
                confidence=max(knowledge_synthesis_cards.confidence, excluded.confidence),
                summary=excluded.summary,
                care_angle=excluded.care_angle,
                suggested_action_json=excluded.suggested_action_json,
                evidence_json=excluded.evidence_json,
                updated_at=excluded.updated_at
            """,
            (synthesis_id, domain, title, summary, float(confidence), actionability, risk_level,
             status, json.dumps(connected_claim_ids, ensure_ascii=False),
             json.dumps(connected_entity_ids or [], ensure_ascii=False), care_angle,
             json.dumps(suggested_action or {}, ensure_ascii=False, sort_keys=True, default=str),
             json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True, default=str), now, now),
        )
    return synthesis_id


def list_synthesis_cards(status: str | None = None, domain: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    ensure_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if domain:
        clauses.append("domain=?")
        params.append(domain)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM knowledge_synthesis_cards{where} ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]
