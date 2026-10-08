from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def _dump_zip(tmp_path: Path) -> Path:
    records = [
        {
            "record_type": "body_signal",
            "topic": "pizza constipation",
            "summary": "Pizza causes bad heartburn for the owner.",
            "details": {"food_trigger": "pizza", "symptom_or_pattern": "extreme constipation", "body_area": "GI"},
            "source_basis": "user_stated",
            "confidence": 0.99,
            "sensitivity": "high",
            "time_context": "thread",
            "use_for_mongoose": "Recall as a personal trigger.",
            "do_not_overgeneralise": False,
            "retrieval_tags": ["pizza", "constipation", "bloat"],
        },
        {
            "record_type": "do_not_do",
            "topic": "old GutCheck data",
            "summary": "Do not let 2019 lab data override current lived symptoms.",
            "details": {"rule": "current symptoms override old GutCheck categories"},
            "source_basis": "user_corrected",
            "confidence": 0.99,
            "sensitivity": "high",
            "time_context": "thread",
            "use_for_mongoose": "Apply as a quality guardrail.",
            "do_not_overgeneralise": True,
            "retrieval_tags": ["GutCheck", "2019"],
        },
        {
            "record_type": "health_context",
            "topic": "CoQ10 supplement hypothesis",
            "summary": "Assistant suggested CoQ10 might be relevant to bile-flow contexts.",
            "details": {"supplement": "CoQ10"},
            "source_basis": "assistant_suggested",
            "confidence": 0.65,
            "sensitivity": "high",
            "time_context": "thread",
            "use_for_mongoose": "Keep searchable, but verify before advice.",
            "do_not_overgeneralise": True,
            "retrieval_tags": ["CoQ10", "supplement"],
        },
        {
            "record_type": "posture_pattern",
            "topic": "right side compression",
            "summary": "Long desk blocks may make the owner's right side feel compressed.",
            "details": {"body_area": "right side", "helpful_intervention": "short posture reset"},
            "source_basis": "user_stated",
            "confidence": 0.9,
            "sensitivity": "medium",
            "time_context": "thread",
            "use_for_mongoose": "Create a posture nudge candidate.",
            "do_not_overgeneralise": False,
            "retrieval_tags": ["posture", "right side"],
        },
    ]
    meta = """
dump_version: "mongoose-thread-dump-v2"
thread_title: "Body test thread"
thread_date_range: "test"
extracted_at: "2026-07-01"
primary_domains:
  - "body wellness"
sensitivity_level: "high"
overall_confidence: "mixed"
source_quality_notes:
  - "test dump"
""".strip()
    p = tmp_path / "thread_dump.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("metadata.yaml", meta)
        z.writestr("structured_records.jsonl", "\n".join(json.dumps(r) for r in records))
        z.writestr("MONGOOSE_THREAD_KNOWLEDGE_DUMP.md", "# Body test thread\n")
        z.writestr("README.txt", "test")
    return p


def test_thread_dump_import_builds_claims_memory_review_and_candidates(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    dump = _dump_zip(tmp_path)
    from mongoose.knowledge_import import import_bulk_dump

    report = import_bulk_dump(dump)

    assert report.ok is True
    assert report.records_seen == 4
    assert report.raw_files_preserved == 4
    assert report.claims_created == 4
    assert report.canonical_memories_created >= 2
    assert report.persona_rules_created >= 2
    assert report.scheduler_candidates_created >= 2
    assert report.insight_candidates_created >= 2
    assert report.records_needing_review >= 1

    con = sqlite3.connect(tmp_path / "m.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        facts = [r["fact"] for r in con.execute("SELECT fact FROM memory_facts").fetchall()]
        assert any("Pizza causes bad heartburn" in f for f in facts)
        assert not any("CoQ10 might be relevant" in f for f in facts)
        review = [dict(r) for r in con.execute("SELECT * FROM knowledge_review_queue").fetchall()]
        assert any("Sensitive health" in r["reason"] or "review" in r["reason"] for r in review)
        nudges = [dict(r) for r in con.execute("SELECT * FROM knowledge_nudge_candidates").fetchall()]
        assert any("posture" in r["suggested_message"].lower() for r in nudges)
        safety = [dict(r) for r in con.execute("SELECT * FROM knowledge_safety_rules").fetchall()]
        assert any("overgeneralise" in r["text"].lower() for r in safety)
    finally:
        con.close()


def test_chat_upload_ingests_thread_dump_instead_of_dead_asset(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    dump = _dump_zip(tmp_path)
    from mongoose.services.uploads import handle_chat_attachment

    res = handle_chat_attachment("thread_dump.zip", dump.read_bytes(), uploaded_by="owner", surface="test")

    assert res["ok"] is True
    assert res["lane"] == "knowledge_import"
    assert res["package_type"] == "chatgpt_thread_dump"


def test_research_request_uses_searxng_but_stays_snippet_and_review_oriented(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.knowledge_import.research_broker import build_queries, request_for_insight
    from mongoose.knowledge_import.schemas import CarePolicy, InsightCandidate, NudgeMode

    insight = InsightCandidate(
        insight_id="i1",
        domain="body_wellness",
        title="Nurofen constipation context",
        hypothesis="Imported dump mentions Nurofen and constipation.",
        confidence=0.7,
        actionability="medium",
        risk_level="high",
        research_status="needed",
        care_policy=CarePolicy(nudge_mode=NudgeMode.ask_first, research_required=True),
    )
    req = request_for_insight(insight)
    assert req is not None
    assert req.source_policy == "medical_authoritative"
    queries = build_queries(req)
    assert any("site:nhs.uk" in q for q in queries)
    assert any("site:medicines.org.uk" in q for q in queries)
