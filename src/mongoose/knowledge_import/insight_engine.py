from __future__ import annotations

import json
from typing import Any

from ..db import connect, utc_now_iso
from .schemas import make_id
from .store import ensure_tables


def list_insights(domain: str | None = None, *, status: str | None = None,
                  limit: int = 20) -> list[dict[str, Any]]:
    ensure_tables()
    clauses = []
    params: list[Any] = []
    if domain:
        clauses.append("domain=?")
        params.append(domain)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM insight_candidates{where} ORDER BY updated_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 100))),
        ).fetchall()
    return [dict(r) for r in rows]


def explain_insight(insight_id: str) -> dict[str, Any]:
    ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM insight_candidates WHERE insight_id=?", (insight_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown insight"}
        ids = json.loads(row["source_claim_ids_json"] or "[]")
        claims = []
        for cid in ids:
            c = con.execute("SELECT * FROM knowledge_claims WHERE claim_id=?", (cid,)).fetchone()
            if c:
                claims.append(dict(c))
    return {
        "ok": True,
        "insight": dict(row),
        "supporting_claims": claims,
        "why": "This is a candidate, not a command. It connects imported owner claims and keeps provenance for review.",
    }


def record_feedback(insight_id: str, feedback: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_tables()
    feedback = (feedback or "").strip().lower()
    if feedback not in {"helpful", "not_now", "too_much", "wrong", "more_like_this", "less_like_this"}:
        return {"ok": False, "error": "unknown feedback value"}
    fid = make_id("feedback", insight_id, feedback, utc_now_iso())
    with connect() as con:
        exists = con.execute("SELECT 1 FROM insight_candidates WHERE insight_id=?", (insight_id,)).fetchone()
        if not exists:
            return {"ok": False, "error": "unknown insight"}
        con.execute(
            "INSERT INTO insight_feedback(feedback_id, insight_id, feedback, context_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (fid, insight_id, feedback, json.dumps(context or {}, ensure_ascii=False), utc_now_iso()),
        )
    try:
        from .store import update_care_feedback_counts
        update_care_feedback_counts(insight_id, feedback)
    except Exception:
        pass
    return {"ok": True, "feedback_id": fid}


def care_suggestions(limit: int = 3, *, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Active, low-risk imported care plans that may be surfaced by the Surpriser."""
    try:
        from .care_planner import rank_care_candidates
        return rank_care_candidates(context or {}, limit=limit, include_inactive=False)
    except Exception:
        return []
