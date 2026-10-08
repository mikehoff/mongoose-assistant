from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

from test_knowledge_import import _fresh


def _alias_dump(tmp_path: Path) -> Path:
    records = [
        {
            "record_type": "health_context",
            "topic": "Nurofen context",
            "summary": "User discussed Nurofen in relation to bloating and heartburn.",
            "details": {"medication": "Nurofen"},
            "source_basis": "user_stated",
            "confidence": 0.9,
            "sensitivity": "high",
            "time_context": "thread",
            "use_for_mongoose": "Keep as sensitive context, not dosing advice.",
            "do_not_overgeneralise": True,
        },
        {
            "record_type": "health_context",
            "topic": "ibuprofen context",
            "summary": "The same discussion referred to ibuprofen as the underlying medicine.",
            "details": {"medication": "ibuprofen"},
            "source_basis": "user_stated",
            "confidence": 0.9,
            "sensitivity": "high",
            "time_context": "thread",
            "use_for_mongoose": "Fuse with Nurofen context.",
            "do_not_overgeneralise": True,
        },
        {
            "record_type": "body_signal",
            "topic": "spicy food stomach pain",
            "summary": "User stated that spicy food makes their stomach hurt.",
            "details": {"food_trigger": "spicy food", "symptom_or_pattern": "stomach pain", "body_area": "GI"},
            "source_basis": "user_stated",
            "confidence": 0.96,
            "sensitivity": "medium",
            "time_context": "thread",
            "use_for_mongoose": "Create a contextual care idea for event/body-state moments.",
            "do_not_overgeneralise": False,
        },
    ]
    meta = 'dump_version: "mongoose-thread-dump-v2"\nthread_title: "Alias care test"\nprimary_domains: ["body wellness"]\nsensitivity_level: "high"\n'
    p = tmp_path / "alias_dump.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("metadata.yaml", meta)
        z.writestr("structured_records.jsonl", "\n".join(json.dumps(r) for r in records))
        z.writestr("MONGOOSE_THREAD_KNOWLEDGE_DUMP.md", "# Alias care test\n")
    return p


def test_entity_fusion_creates_canonical_alias_edges(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.store import list_entity_fusions, dashboard, search_knowledge

    report = import_bulk_dump(_alias_dump(tmp_path))
    assert report.ok
    fusions = list_entity_fusions(limit=20)
    assert any("Nurofen" in (f.get("canonical_name") or "") and "ibuprofen" in (f.get("canonical_name") or "") for f in fusions)
    dash = dashboard()
    assert dash["counts"]["fusions"] >= 1
    hits = search_knowledge("Nurofen ibuprofen", limit=10)
    assert any(h.get("kind") == "knowledge_entity" for h in hits)

    con = sqlite3.connect(tmp_path / "m.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        rels = [r["relation"] for r in con.execute("SELECT relation FROM knowledge_edges").fetchall()]
        assert "same_as" in rels
        assert "has_alias_entity" in rels
    finally:
        con.close()


def test_care_plan_activation_is_bounded_and_visible(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.care_planner import activate_care_plan, next_care_prompt, rank_care_candidates
    from mongoose.knowledge_import.store import dashboard, list_care_plans

    import_bulk_dump(_alias_dump(tmp_path))
    care = rank_care_candidates({"prompt": "date tonight and worried about bloat"}, limit=5, include_inactive=True)
    assert care
    insight_id = care[0]["insight_id"]
    res = activate_care_plan(insight_id, activation_level="gentle", cadence="contextual", actor="test")
    assert res["ok"] is True
    assert res["plan"]["status"] == "active"
    assert "Tiny" in res["message"] or "By the way" in res["message"] or "Small" in res["message"]
    assert list_care_plans(status="active")
    dash = dashboard()
    assert dash["counts"]["care_active"] >= 1
    prompt = next_care_prompt({"prompt": "date tonight and worried about bloat"})
    assert prompt["ok"] is True
    assert prompt["prompt"]
    assert prompt["prompt"]["activation_state"] == "active"


def test_care_activation_blocks_sensitive_research_needed_insight(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.care_planner import activate_care_plan
    from mongoose.knowledge_import.insight_engine import list_insights

    import_bulk_dump(_alias_dump(tmp_path))
    high = next(i for i in list_insights(limit=20) if i["risk_level"] == "high")
    res = activate_care_plan(high["insight_id"], activation_level="active")
    assert res["ok"] is False
    assert "cannot activate" in res["error"]
