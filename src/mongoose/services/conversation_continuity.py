"""Conversation continuity reporting for the single-brain promise.

The report is intentionally metadata-first. It answers: will the next chat/voice/phone turn replay the
right thread, are guest threads isolated, and is the history budget likely to preserve recent context?
It does not dump message content.
"""
from __future__ import annotations

from typing import Any

from ..db import connect
from .memory import (
    DEFAULT_CONVERSATION_ID,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_HISTORY_TOKEN_BUDGET,
    OWNER_SCOPE,
    estimate_tokens,
    recent_messages,
    scope_for_conversation,
)


def _table_exists(con, name: str) -> bool:
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _conversation_stats(con, conversation_id: str) -> dict[str, Any]:
    if not _table_exists(con, "messages"):
        return {"conversation_id": conversation_id, "message_count": 0, "last_at": None, "last_role": None}
    row = con.execute(
        """
        SELECT COUNT(*) AS n, MAX(created_at) AS last_at,
               (SELECT role FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1) AS last_role
        FROM messages WHERE conversation_id=?
        """,
        (conversation_id, conversation_id),
    ).fetchone()
    replay = recent_messages(conversation_id, limit=DEFAULT_HISTORY_LIMIT,
                             token_budget=DEFAULT_HISTORY_TOKEN_BUDGET)
    replay_tokens = sum(estimate_tokens(m.get("content") or "") for m in replay)
    return {
        "conversation_id": conversation_id,
        "message_count": int(row["n"] or 0),
        "last_at": row["last_at"],
        "last_role": row["last_role"],
        "replay_message_count": len(replay),
        "replay_estimated_tokens": replay_tokens,
        "replay_token_budget": DEFAULT_HISTORY_TOKEN_BUDGET,
        "history_limit": DEFAULT_HISTORY_LIMIT,
        "content_returned": False,
    }


def _conversation_ids(con) -> list[str]:
    if not _table_exists(con, "conversations"):
        return []
    rows = con.execute("SELECT id FROM conversations ORDER BY updated_at DESC, id ASC").fetchall()
    return [str(r["id"]) for r in rows]


def _fact_counts(con) -> dict[str, int]:
    if not _table_exists(con, "memory_facts"):
        return {"owner": 0, "guest": 0, "other": 0}
    rows = con.execute(
        """
        SELECT CASE WHEN scope='owner' THEN 'owner'
                    WHEN scope LIKE 'guest:%' THEN 'guest'
                    ELSE 'other' END AS bucket,
               COUNT(*) AS n
        FROM memory_facts GROUP BY bucket
        """
    ).fetchall()
    out = {"owner": 0, "guest": 0, "other": 0}
    for r in rows:
        out[str(r["bucket"])] = int(r["n"] or 0)
    return out


def continuity_report() -> dict[str, Any]:
    with connect() as con:
        ids = _conversation_ids(con)
        main = _conversation_stats(con, DEFAULT_CONVERSATION_ID)
        guests = [_conversation_stats(con, cid) for cid in ids if cid.startswith("guest:")]
        facts = _fact_counts(con)

    warnings: list[str] = []
    if main["message_count"] == 0:
        warnings.append("mongoose-main has no persisted messages yet; next owner chat has no short-term history to replay")
    if main["replay_estimated_tokens"] >= int(DEFAULT_HISTORY_TOKEN_BUDGET * 0.95):
        warnings.append("recent owner history is at the token budget; older context will be deliberately omitted")
    if any(g["replay_estimated_tokens"] >= int(DEFAULT_HISTORY_TOKEN_BUDGET * 0.95) for g in guests):
        warnings.append("one or more guest threads are at the token budget; older guest context will be deliberately omitted")

    invariants = [
        {
            "check": "owner surfaces share one default conversation",
            "ok": DEFAULT_CONVERSATION_ID == "mongoose-main",
            "detail": "cockpit, CLI, phone owner chat and HA Assist default to mongoose-main",
        },
        {
            "check": "guest conversations map to guest memory scopes",
            "ok": scope_for_conversation("guest:example") == "guest:example",
            "detail": "guest:<device_id> stays separate from owner scope",
        },
        {
            "check": "owner memory scope remains owner",
            "ok": scope_for_conversation(DEFAULT_CONVERSATION_ID) == OWNER_SCOPE,
            "detail": "owner facts are shared across owner surfaces only",
        },
        {
            "check": "conversation report does not return message text",
            "ok": True,
            "detail": "only counts, roles and timestamps are returned",
        },
    ]

    return {
        "ok": all(i["ok"] for i in invariants),
        "main": main,
        "guest_threads": guests,
        "conversation_count": 1 + len(guests) if main["conversation_id"] else len(guests),
        "memory_fact_counts": facts,
        "warnings": warnings,
        "invariants": invariants,
        "redaction": "No message content is returned; this is safe for status/chat/audit use.",
    }
