from __future__ import annotations

import json
from typing import Any

from ..db import connect, utc_now_iso
from ..web_search import SearxngError, search_web
from .schemas import EvidenceCard, InsightCandidate, ResearchRequest, make_id
from .store import ensure_tables


def request_for_insight(insight: InsightCandidate) -> ResearchRequest | None:
    if insight.research_status not in {"needed", "requested"} and not insight.care_policy.research_required:
        return None
    policy = "medical_authoritative" if insight.domain == "body_wellness" and insight.risk_level == "high" else "local_first"
    return ResearchRequest(
        topic=insight.title,
        question=f"What cautious, current context is useful for this owner insight: {insight.hypothesis}",
        domain=insight.domain,
        why_needed="Sensitive or externally changing claim imported from a thread dump; research constrains the insight before proactive action.",
        source_policy=policy,
        time_sensitivity="current" if policy in {"medical_authoritative", "primary_legal"} else "stable",
        max_results=6,
        require_citations=True,
    )


def build_queries(req: ResearchRequest) -> list[str]:
    q = req.question.strip()
    if req.source_policy == "medical_authoritative":
        return [
            f"site:nhs.uk {req.topic}",
            f"site:medicines.org.uk {req.topic}",
            f"site:ema.europa.eu {req.topic}",
            q,
        ]
    if req.source_policy == "primary_legal":
        return [
            f"site:legislation.gov.uk {req.topic}",
            f"site:gov.uk {req.topic}",
            q,
        ]
    if req.source_policy == "official_only":
        return [f"official {req.topic}", q]
    return [q]


async def run_research_request(req: ResearchRequest, *, insight_id: str | None = None) -> dict[str, Any]:
    """Run a bounded SearXNG research mission and persist the evidence cards.

    This is intentionally snippets-only. It does not fetch result pages and it does not upgrade a claim
    into advice. It produces evidence cards for later review/synthesis.
    """
    ensure_tables()
    request_id = make_id("research", insight_id or "none", req.topic, req.question)
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO research_requests(request_id, insight_id, topic, question, domain,
                                          source_policy, time_sensitivity, status, result_json,
                                          created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', '{}', ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (request_id, insight_id, req.topic, req.question, req.domain,
             req.source_policy, req.time_sensitivity, now, now),
        )
    cards: list[EvidenceCard] = []
    errors: list[str] = []
    for query in build_queries(req):
        try:
            rows = await search_web(query, limit=max(1, min(req.max_results, 10)),
                                    time_range="year" if req.time_sensitivity in {"current", "volatile"} else None)
        except SearxngError as exc:
            errors.append(str(exc))
            break
        for row in rows:
            cards.append(EvidenceCard(
                title=row.get("title") or row.get("where") or "untitled",
                url=row.get("where") or row.get("open") or "",
                source=row.get("engine") or "searxng",
                snippet=row.get("snippet") or "",
                source_quality=_quality_for_url(row.get("where") or "", req),
                supports=[req.topic],
                uncertainty="Snippet-only evidence; review before converting into advice.",
            ))
        if cards:
            break
    status = "completed" if cards else "failed"
    result = {"ok": bool(cards), "request_id": request_id,
              "evidence": [c.model_dump() for c in cards], "errors": errors}
    with connect() as con:
        con.execute(
            "UPDATE research_requests SET status=?, result_json=?, updated_at=? WHERE request_id=?",
            (status, json.dumps(result, ensure_ascii=False), utc_now_iso(), request_id),
        )
    return result


def _quality_for_url(url: str, req: ResearchRequest) -> str:
    low = (url or "").lower()
    if req.source_policy == "medical_authoritative":
        if any(host in low for host in ("nhs.uk", "medicines.org.uk", "ema.europa.eu", "nice.org.uk")):
            return "authoritative_medical"
        return "general_web_medical_review_needed"
    if req.source_policy == "primary_legal":
        if any(host in low for host in ("legislation.gov.uk", "gov.uk")):
            return "primary_or_official_legal"
        return "legal_review_needed"
    if "gov" in low or "nhs" in low or "edu" in low or "ac.uk" in low:
        return "authoritative_or_institutional"
    return "general_web_snippet"


def list_research_requests(limit: int = 20) -> list[dict[str, Any]]:
    ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM research_requests ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [dict(r) for r in rows]


def pending_research_requests(limit: int = 100) -> list[dict[str, Any]]:
    """Queued research missions that have not run yet, OLDEST first so the backlog actually drains.

    Each row is tagged `sensitive` when its source policy is medical/health, so the scheduler can run
    safe/current missions first and treat sensitive ones purely as source-carded, non-instructional
    review material rather than anything that acts on the owner."""
    ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM research_requests WHERE status='queued' ORDER BY created_at ASC LIMIT ?",
            (max(1, min(int(limit), 300)),),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["sensitive"] = (d.get("source_policy") == "medical_authoritative")
        out.append(d)
    return out


def request_for_stored_insight(row: dict[str, Any]) -> ResearchRequest | None:
    """Build a ResearchRequest from a DB insight row without trusting it as advice."""
    if not row:
        return None
    domain = row.get("domain") or "general"
    risk = row.get("risk_level") or "medium"
    status = row.get("research_status") or "not_needed"
    if status not in {"needed", "failed", "requested"} and risk != "high":
        return None
    policy = "medical_authoritative" if domain == "body_wellness" and risk == "high" else (
        "primary_legal" if domain == "rtm" else "local_first"
    )
    return ResearchRequest(
        topic=row.get("title") or row.get("hypothesis") or "Imported owner insight",
        question=f"What cautious current context constrains this imported insight: {row.get('hypothesis','')}",
        domain=domain,
        why_needed="Imported insight is sensitive, externally changing or marked for review; research creates evidence cards before action.",
        source_policy=policy,
        time_sensitivity="current" if policy in {"medical_authoritative", "primary_legal"} else "stable",
        max_results=6,
        require_citations=True,
    )


def persist_research_request(req: ResearchRequest, *, insight_id: str | None = None) -> dict[str, Any]:
    """Queue a research mission without running web search."""
    ensure_tables()
    request_id = make_id("research", insight_id or "none", req.topic, req.question)
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO research_requests(request_id, insight_id, topic, question, domain,
                                          source_policy, time_sensitivity, status, result_json,
                                          created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', '{}', ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (request_id, insight_id, req.topic, req.question, req.domain,
             req.source_policy, req.time_sensitivity, now, now),
        )
        if insight_id:
            try:
                con.execute("UPDATE insight_candidates SET research_status='requested', updated_at=? WHERE insight_id=?", (now, insight_id))
            except Exception:
                pass
    return {"request_id": request_id, "insight_id": insight_id, "topic": req.topic,
            "domain": req.domain, "source_policy": req.source_policy, "status": "queued"}


def queue_pending_research(limit: int = 10) -> list[dict[str, Any]]:
    from .reflection import queue_research_for_pending
    return queue_research_for_pending(limit=limit)


async def run_research_by_request_id(request_id: str) -> dict[str, Any]:
    """Run a queued research request by id, preserving the snippets as evidence cards."""
    ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM research_requests WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "unknown research request"}
    req = ResearchRequest(
        topic=row["topic"],
        question=row["question"],
        domain=row["domain"],
        why_needed="Running queued research request.",
        source_policy=row["source_policy"],
        time_sensitivity=row["time_sensitivity"],
        max_results=6,
        require_citations=True,
    )
    return await run_research_request(req, insight_id=row["insight_id"])
