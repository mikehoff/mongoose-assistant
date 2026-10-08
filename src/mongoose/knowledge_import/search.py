from __future__ import annotations

import hashlib
from typing import Any

from .store import search_knowledge as _search_knowledge


def _ref(kind: str, native_id: str) -> str:
    return f"K{hashlib.sha1((kind + ':' + native_id).encode('utf-8')).hexdigest()[:6].upper()}"


def search(q: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return knowledge-graph hits in the unified-search item shape."""
    out: list[dict[str, Any]] = []
    for i, r in enumerate(_search_knowledge(q, limit=limit), start=1):
        kind = r.get("kind") or "knowledge"
        native_id = str(r.get("native_id") or r.get("claim_id") or r.get("insight_id") or "")
        out.append({
            "kind": "knowledge",
            "title": r.get("title") or "Imported knowledge",
            "where": r.get("where") or "Mongoose knowledge graph",
            "device": "mongoose",
            "connector": kind,
            "native_id": native_id,
            "snippet": (r.get("snippet") or "")[:240],
            "privacy": r.get("sensitivity") or "medium",
            "open": native_id,
            "date": r.get("updated_at") or r.get("created_at"),
            "source_rank": i,
            "item_ref": _ref(kind, native_id),
            "knowledge_kind": kind,
            "status": r.get("status"),
            "confidence": r.get("confidence"),
            "rank_reason": ["imported knowledge graph hit"],
        })
    return out
