"""Two delight upgrades: (1) the scheduler auto-executes queued research safe-first within a bounded
budget and then digests new evidence; (2) the care voice reads like a friend noticing something, not a
formatted memory card, with an optional LLM warming that fails safe."""
from __future__ import annotations

import asyncio
import warnings

warnings.filterwarnings("ignore")


# ---------- research auto-execution ----------

def _queue_missions():
    from mongoose.knowledge_import.research_broker import persist_research_request
    from mongoose.knowledge_import.schemas import ResearchRequest
    persist_research_request(ResearchRequest(topic="Medication context", question="cautious current context",
        domain="body_wellness", why_needed="x", source_policy="medical_authoritative",
        time_sensitivity="current", max_results=6, require_citations=True))
    persist_research_request(ResearchRequest(topic="RTM timeline", question="rtm current",
        domain="rtm", why_needed="x", source_policy="primary_legal",
        time_sensitivity="current", max_results=6, require_citations=True))
    persist_research_request(ResearchRequest(topic="Stretching facts", question="stretching recovery",
        domain="general", why_needed="x", source_policy="local_first",
        time_sensitivity="stable", max_results=6, require_citations=True))


def test_pending_requests_are_queued_oldest_first_and_flag_sensitive():
    _queue_missions()
    from mongoose.knowledge_import.research_broker import pending_research_requests
    p = pending_research_requests(limit=50)
    assert len(p) == 3
    assert all(r["status"] == "queued" for r in p)
    by_topic = {r["topic"]: r for r in p}
    assert by_topic["Medication context"]["sensitive"] is True
    assert by_topic["RTM timeline"]["sensitive"] is False
    assert by_topic["Stretching facts"]["sensitive"] is False


def test_scheduler_drains_safe_first_within_budget_then_digests(monkeypatch):
    _queue_missions()
    import mongoose.web_search as ws
    import mongoose.knowledge_import.research_broker as rb
    import mongoose.services.scheduler as sch
    monkeypatch.setattr(ws, "is_enabled", lambda: True)
    order = []

    async def fake_run(rid):
        from mongoose.db import connect
        with connect() as con:
            row = con.execute("SELECT topic FROM research_requests WHERE request_id=?", (rid,)).fetchone()
            order.append(row["topic"])
            con.execute("UPDATE research_requests SET status='completed', result_json='{\"ok\":true}' WHERE request_id=?", (rid,))
        return {"ok": True, "request_id": rid, "evidence": [{"title": "t"}]}

    monkeypatch.setattr(rb, "run_research_by_request_id", fake_run)
    # small budget proves boundedness AND that the sensitive mission is not among the first run
    monkeypatch.setenv("MONGOOSE_KNOWLEDGE_RESEARCH_MAX_PER_CYCLE", "2")
    monkeypatch.setattr(sch, "get_settings", lambda: type("S", (), {"knowledge_research_max_per_cycle": 2})())
    out = asyncio.run(sch._job_knowledge_research())
    assert out["ran"] == 2 and out["failed"] == 0
    assert "Medication context" not in order          # sensitive deferred past the budget
    assert set(order) <= {"RTM timeline", "Stretching facts"}
    assert "digested" in out                        # digest step executed


# ---------- deepened care voice ----------

def test_care_voice_is_woven_not_a_memory_card():
    from mongoose.knowledge_import.care_planner import _engaging_message
    row = {"hypothesis": "The user stretches and takes a warm bath for recovery after exercise",
           "title": "Recovery routine"}
    action = {"type": "contextual_care", "example": "some example"}
    msg = _engaging_message(row, action, tone="warm_precise_body", matched=["posture"], opener_index=1)
    assert msg and not msg.startswith("Tiny care thought:")     # not the old label-prefixed card
    assert "\u2014" not in msg and "\u2013" not in msg          # no em/en dashes
    # different opener_index yields a different phrasing (variation, not one canned line)
    other = _engaging_message(row, action, tone="warm_precise_body", matched=["posture"], opener_index=2)
    assert other != msg


def test_care_voice_crisps_long_facts():
    from mongoose.knowledge_import.care_planner import _clean_fact
    row = {"hypothesis": "Day A focuses on general strength; user later dropped one accessory exercise to reduce redundancy"}
    fact = _clean_fact(row, {})
    assert ";" not in fact and " later dropped" not in fact     # first clause only
    assert len(fact.split()) <= 23


def test_refine_care_voice_falls_back_when_no_model(monkeypatch):
    from mongoose.knowledge_import import care_planner
    base = "A gentle posture cue while you are at the desk: relax your shoulders."

    async def boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr("mongoose.llm.ask_persona", boom)
    out = asyncio.run(care_planner.refine_care_voice(base, tone="warm_precise", context=["posture"]))
    assert out == base


def test_refine_care_voice_rejects_instructional_drift(monkeypatch):
    from mongoose.knowledge_import import care_planner
    base = "A gentle stretch reset when you feel compressed."

    async def drifts(*a, **k):
        return "Take 200 mg of your supplement twice daily for best results."

    monkeypatch.setattr("mongoose.llm.ask_persona", drifts)
    out = asyncio.run(care_planner.refine_care_voice(base, tone="warm_precise_body"))
    assert out == base     # instructional/dosing output discarded, safe fallback
