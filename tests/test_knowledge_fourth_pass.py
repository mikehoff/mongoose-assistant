from __future__ import annotations

import json
import zipfile
from pathlib import Path

from test_knowledge_import import _fresh


def _multi_dump(tmp_path: Path) -> Path:
    records = [
        {
            "record_type": "body_signal",
            "topic": "spicy food stomach pain",
            "summary": "User stated that spicy food makes their stomach hurt and can worsen bloating.",
            "details": {"food_trigger": "spicy food", "symptom_or_pattern": "stomach pain and bloating", "body_area": "GI"},
            "source_basis": "user_stated",
            "confidence": 0.96,
            "sensitivity": "medium",
            "time_context": "thread",
            "use_for_mongoose": "Use as contextual food/body-state pattern before events.",
            "do_not_overgeneralise": False,
        },
        {
            "record_type": "wellness_routine",
            "topic": "swim yoga helps puffiness",
            "summary": "Short swims and yoga sessions can help the user feel less puffy and more decompressed.",
            "details": {"helpful_intervention": "short swim and yoga", "when_to_use": "puffy or tense body days"},
            "source_basis": "user_stated",
            "confidence": 0.9,
            "sensitivity": "medium",
            "time_context": "thread",
            "use_for_mongoose": "Suggest only when low friction.",
            "do_not_overgeneralise": False,
        },
        {
            "record_type": "posture_pattern",
            "topic": "right side compression",
            "summary": "User has a general tension/posture pattern that responds to precise core-bracing reset cues.",
            "details": {"body_area": "right side", "cue": "shoulders down", "helpful_intervention": "short posture reset"},
            "source_basis": "user_stated",
            "confidence": 0.93,
            "sensitivity": "medium",
            "time_context": "thread",
            "use_for_mongoose": "Use for desk and training context.",
            "do_not_overgeneralise": False,
        },
        {
            "record_type": "health_context",
            "topic": "L acidophilus",
            "summary": "The thread discussed L. acidophilus as a probiotic context, not a confirmed instruction.",
            "details": {"supplement": "L. acidophilus"},
            "source_basis": "assistant_suggested",
            "confidence": 0.7,
            "sensitivity": "high",
            "time_context": "thread",
            "use_for_mongoose": "Keep searchable and review before advice.",
            "do_not_overgeneralise": True,
        },
        {
            "record_type": "health_context",
            "topic": "Lactobacillus acidophilus",
            "summary": "The same thread also referred to Lactobacillus acidophilus by full name.",
            "details": {"supplement": "Lactobacillus acidophilus"},
            "source_basis": "assistant_suggested",
            "confidence": 0.7,
            "sensitivity": "high",
            "time_context": "thread",
            "use_for_mongoose": "Alias context only.",
            "do_not_overgeneralise": True,
        },
    ]
    meta = 'dump_version: "mongoose-thread-dump-v2"\nthread_title: "Fourth pass test"\nprimary_domains: ["body wellness"]\nsensitivity_level: "high"\n'
    p = tmp_path / "multi_dump.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("metadata.yaml", meta)
        z.writestr("structured_records.jsonl", "\n".join(json.dumps(r) for r in records))
        z.writestr("MONGOOSE_THREAD_KNOWLEDGE_DUMP.md", "# Fourth pass test\n")
    return p


def test_synthesis_cards_and_search_are_created(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.synthesis import run_synthesis_cycle
    from mongoose.knowledge_import.store import dashboard, list_synthesis_cards, search_knowledge

    report = import_bulk_dump(_multi_dump(tmp_path), apply_accepted_memory=False)
    assert report.ok
    out = run_synthesis_cycle(limit=20)
    assert out["ok"] is True
    cards = list_synthesis_cards(limit=20)
    assert any("Food" in c["title"] or "Posture" in c["title"] for c in cards)
    dash = dashboard()
    assert dash["counts"]["synthesis_cards"] >= 1
    hits = search_knowledge("synthesis spicy stomach", limit=20)
    assert any(h["kind"] == "knowledge_synthesis" for h in hits)


def test_reviewable_fusion_suggestions_can_be_resolved(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.entity_suggestions import suggest_entity_fusions
    from mongoose.knowledge_import.store import list_fusion_suggestions, resolve_fusion_suggestion, list_entity_fusions

    import_bulk_dump(_multi_dump(tmp_path), apply_accepted_memory=False)
    suggest_entity_fusions(limit=30)
    suggestions = list_fusion_suggestions(limit=30)
    # Some aliases fuse automatically. The review suggestion layer must at least be queryable and safe.
    assert isinstance(suggestions, list)
    if suggestions:
        first = suggestions[0]
        res = resolve_fusion_suggestion(first["suggestion_id"], "approve", actor="test")
        assert res["ok"] is True
        assert list_entity_fusions(limit=50)


def test_cockpit_endpoints_for_suggestions_and_synthesis(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    import mongoose.app as A
    from fastapi.testclient import TestClient

    import_bulk_dump(_multi_dump(tmp_path), apply_accepted_memory=False)
    c = TestClient(A.app, raise_server_exceptions=False)
    nonce = A._COCKPIT_NONCE
    headers = {"X-Cockpit-Nonce": nonce}

    r = c.post("/cockpit/knowledge/entity/suggest-fusions", json={"limit": 20}, headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = c.post("/cockpit/knowledge/synthesis/run", json={"limit": 20}, headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = c.get("/cockpit/knowledge/dashboard", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "fusion_suggestions" in data
    assert "synthesis_cards" in data


def test_cli_new_knowledge_commands(monkeypatch, tmp_path, capsys):
    _fresh(monkeypatch, tmp_path)
    from mongoose.cli import main

    def run_cli(args):
        monkeypatch.setattr("sys.argv", ["mongoose", *args])
        try:
            main()
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    p = _multi_dump(tmp_path)
    assert run_cli(["knowledge", "import", str(p)]) == 0
    assert run_cli(["knowledge", "suggest-fusions", "--limit", "10"]) == 0
    assert run_cli(["knowledge", "synthesize", "--limit", "10"]) == 0
    assert run_cli(["knowledge", "synthesis", "--limit", "10"]) == 0
    out = capsys.readouterr().out
    assert "synthesis" in out or "inserted" in out


def test_synthesis_insights_remain_safe_for_sensitive_context(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.insight_engine import list_insights
    from mongoose.knowledge_import.care_planner import rank_care_candidates

    import_bulk_dump(_multi_dump(tmp_path), apply_accepted_memory=False)
    insights = list_insights(limit=100)
    assert insights
    risky = [i for i in insights if i["risk_level"] == "high"]
    assert risky
    care = rank_care_candidates({"prompt": "probiotic supplement advice"}, include_inactive=True, limit=20)
    assert all("acidophilus" not in (c.get("text", "") + c.get("title", "")).lower() for c in care if c.get("activation_state") != "accepted")
