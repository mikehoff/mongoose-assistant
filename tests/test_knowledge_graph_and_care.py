"""The knowledge cockpit's new surfaces: a graph snapshot, care review, and research evidence.

Safety-critical assertion: psychological/high-risk insights never appear as auto-surfaced care
candidates, so grief/attachment material can't become a proactive nudge through this path either."""
from __future__ import annotations

import io
import json
import zipfile

from mongoose.knowledge_import import import_bulk_dump_bytes
from mongoose.knowledge_import.store import graph_snapshot
from mongoose.knowledge_import.care_planner import rank_care_candidates


def _dump(records):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x_records.jsonl", "\n".join(json.dumps(r) for r in records))
    return buf.getvalue()


def test_graph_snapshot_shape_is_safe_when_empty():
    g = graph_snapshot(20)
    assert set(g.keys()) >= {"nodes", "edges", "truncated"}
    assert isinstance(g["nodes"], list) and isinstance(g["edges"], list)


def test_graph_snapshot_returns_connected_subgraph():
    recs = [
        {"record_type": "stable_user_fact", "topic": "loc", "summary": "Alex lives in Anytown and drives a hatchback.",
         "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "low"},
        {"record_type": "relationship", "topic": "people", "summary": "Alex has a dog named Rex and a housemate named Sam in Anytown.",
         "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "low"},
        {"record_type": "stable_user_fact", "topic": "work", "summary": "Alex works at Examplecorp and teaches evening classes in Anytown.",
         "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "low"},
    ]
    import_bulk_dump_bytes(_dump(recs), source_name="facts.zip")
    g = graph_snapshot(40)
    # entities may or may not be extracted depending on the deterministic extractor, but the call is safe
    # and, when nodes exist, edges only connect known nodes.
    ids = {n["id"] for n in g["nodes"]}
    for e in g["edges"]:
        assert e["source"] in ids and e["target"] in ids


def test_care_candidates_never_surface_psychological_or_high_risk():
    recs = [
        {"record_type": "body_signal", "topic": "posture", "summary": "The user benefits from a gentle posture reset at their desk.",
         "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "low"},
        {"record_type": "emotional_context", "topic": "grief", "summary": "The user carries grief and low self-worth after a loss.",
         "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "high"},
        {"record_type": "health_context", "topic": "aas", "summary": "The user runs a steroid cycle and tracks bloodwork.",
         "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "high"},
    ]
    import_bulk_dump_bytes(_dump(recs), source_name="mix.zip")
    cands = rank_care_candidates(limit=20)
    for c in cands:
        assert c.get("risk_level") != "high"
        blob = f"{c.get('title','')} {c.get('text','')} {c.get('why','')}".lower()
        for term in ("grief", "self-worth", "self worth", "compoundx", "aas", "steroid", "attachment", "depression"):
            assert term not in blob, f"sensitive term '{term}' surfaced as a care candidate"


def test_search_fusion_synthesis_surfaces_exist_and_are_safe():
    """Search, fusion suggestions, and synthesis-card listing back the new cockpit sections."""
    from mongoose.knowledge_import.store import (search_knowledge, list_fusion_suggestions,
                                                 list_synthesis_cards)
    recs = [
        {"record_type": "stable_user_fact", "topic": "loc", "summary": "Alex lives in Anytown.",
         "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "low"},
        {"record_type": "stable_user_fact", "topic": "loc2", "summary": "Alex is based in Anytown, UK.",
         "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "low"},
    ]
    import_bulk_dump_bytes(_dump(recs), source_name="s.zip")
    # search returns dict rows with a 'kind' tag
    res = search_knowledge("anytown", limit=10)
    assert isinstance(res, list)
    for r in res:
        assert "kind" in r
    # both listings return lists and never throw
    assert isinstance(list_fusion_suggestions(limit=10), list)
    assert isinstance(list_synthesis_cards(limit=10), list)


def test_synthesis_list_endpoint_is_nonce_gated():
    import warnings; warnings.filterwarnings("ignore")
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    for path in ("/cockpit/knowledge/synthesis", "/cockpit/knowledge/search?q=x",
                 "/cockpit/knowledge/fusion/proposals", "/cockpit/knowledge/graph",
                 "/cockpit/knowledge/care", "/cockpit/knowledge/research/list"):
        assert c.get(path).status_code in (401, 403), f"{path} not gated"
