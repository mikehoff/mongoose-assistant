from __future__ import annotations

import sqlite3

from test_knowledge_import import _dump_zip, _fresh


def test_graph_edges_dashboard_search_and_unified_find(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    dump = _dump_zip(tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.store import dashboard, search_knowledge
    from mongoose.services.find import find

    report = import_bulk_dump(dump)
    assert report.ok

    dash = dashboard()
    assert dash["ok"] is True
    assert dash["counts"]["edges"] > 0
    assert dash["counts"]["claims"] == 4

    hits = search_knowledge("pizza constipation")
    assert any("Pizza causes bad heartburn" in h.get("snippet", "") for h in hits)

    unified = find("pizza constipation", kinds=("knowledge",), limit=5, persist=False)
    assert unified["count"] >= 1
    assert unified["results"][0]["kind"] == "knowledge"


def test_review_resolution_can_keep_searchable_and_approve_memory(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    dump = _dump_zip(tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.store import list_review_queue, resolve_review_item

    import_bulk_dump(dump)
    queue = list_review_queue(limit=20)
    assert queue

    item = next(r for r in queue if r["object_type"] in {"claim", "memory_candidate", "insight_candidate"})
    res = resolve_review_item(item["review_id"], "keep_searchable", reason="test")
    assert res["ok"] is True

    # Approve a review memory candidate if one exists; it should land in durable memory.
    queue = list_review_queue(limit=50)
    mem = next((r for r in queue if r["object_type"] == "memory_candidate"), None)
    if mem:
        res = resolve_review_item(mem["review_id"], "approve", reason="test approve")
        assert res["ok"] is True


def test_reflection_queues_research_without_running_web(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    dump = _dump_zip(tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.reflection import run_reflection_cycle

    import_bulk_dump(dump)
    out = run_reflection_cycle("test", queue_research=True)
    assert out["ok"] is True
    assert out["body_patterns"]
    assert out["research_queued"]

    con = sqlite3.connect(tmp_path / "m.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM research_requests").fetchall()]
        assert rows
        assert all(r["status"] == "queued" for r in rows)
    finally:
        con.close()


def test_cockpit_knowledge_endpoints(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    dump = _dump_zip(tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    import mongoose.app as A
    from fastapi.testclient import TestClient

    import_bulk_dump(dump)
    c = TestClient(A.app, raise_server_exceptions=False)
    nonce = A._COCKPIT_NONCE
    r = c.get("/cockpit/knowledge/dashboard", headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = c.get("/cockpit/knowledge/search", params={"q": "pizza"}, headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200
    assert r.json()["results"]

    r = c.post("/cockpit/knowledge/reflection/run", json={"kind": "test"}, headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_care_planner_and_persona_use_imported_knowledge(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    dump = _dump_zip(tmp_path)
    from mongoose.knowledge_import import import_bulk_dump
    from mongoose.knowledge_import.care_planner import rank_care_candidates
    from mongoose.persona import persona_card

    import_bulk_dump(dump)
    care = rank_care_candidates({"prompt": "I am coding at my desk and my right side feels weird"}, limit=5, include_inactive=True)
    assert care
    assert any("right" in (c.get("text", "") + c.get("title", "")).lower() or "posture" in (c.get("text", "") + c.get("title", "")).lower() for c in care)

    card = persona_card("default")
    assert "Imported owner-specific coaching rules" in card
    assert "GutCheck" in card or "Pizza" in card or "physically" in card
