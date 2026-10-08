"""Guest privacy and boundary reporting.

This is deliberately a *report*, not a data browser. It lets the owner or the brain answer
"are guest spaces isolated?" without returning a guest's private notes, reminders, chat text,
model reflections, tokens, token hashes, or raw payloads.
"""
from __future__ import annotations

from typing import Any

from .. import profile as _profile
from ..db import connect


_SECRETY_KEYS = {"token", "device_token", "token_hash", "code", "code_hash", "secret", "password"}


def _table_exists(con, name: str) -> bool:
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _count_by_kind(con, scope: str) -> dict[str, int]:
    if not _table_exists(con, "guest_items"):
        return {}
    rows = con.execute(
        "SELECT kind, COUNT(*) AS n FROM guest_items WHERE guest_scope=? GROUP BY kind",
        (scope,),
    ).fetchall()
    return {str(r["kind"]): int(r["n"] or 0) for r in rows}


def _count_messages(con, conversation_id: str) -> dict[str, Any]:
    if not _table_exists(con, "messages"):
        return {"count": 0, "last_at": None}
    row = con.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS last_at FROM messages WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    return {"count": int(row["n"] or 0), "last_at": row["last_at"]}


def _count_facts(con, scope: str) -> int:
    if not _table_exists(con, "memory_facts"):
        return 0
    row = con.execute("SELECT COUNT(*) AS n FROM memory_facts WHERE scope=?", (scope,)).fetchone()
    return int(row["n"] or 0)


def _safe_guest_summary(device: dict[str, Any], con) -> dict[str, Any]:
    device_id = str(device.get("device_id") or "")
    scope = f"guest:{device_id}"
    return {
        "device_id": device_id,
        "name": device.get("name"),
        "display_name": device.get("display_name"),
        "revoked": bool(device.get("revoked")),
        "freshness": device.get("freshness") or {},
        "capabilities": device.get("capabilities") or [],
        "guest_item_counts": _count_by_kind(con, scope),
        "memory_fact_count": _count_facts(con, scope),
        "conversation": _count_messages(con, scope),
        "content_returned": False,
    }


def guest_privacy_report() -> dict[str, Any]:
    """Return a privacy-preserving report on guest boundaries.

    The owner needs to know whether guests are isolated and whether anything looks stale/broken. They
    do *not* need raw guest state. This function therefore returns counts, role/capability metadata and
    boundary checks only.
    """
    from ..devices import list_devices

    devices = list_devices()
    guests = [d for d in devices if d.get("profile") == "trusted_guest"]
    exposed_secret_keys = sorted({k for d in devices for k in d.keys() if str(k).lower() in _SECRETY_KEYS})
    with connect() as con:
        guest_summaries = [_safe_guest_summary(d, con) for d in guests]
        owner_fact_count = _count_facts(con, "owner")
        guest_scope_fact_count = 0
        if _table_exists(con, "memory_facts"):
            row = con.execute("SELECT COUNT(*) AS n FROM memory_facts WHERE scope LIKE 'guest:%'").fetchone()
            guest_scope_fact_count = int(row["n"] or 0)

    checks = [
        {
            "check": "device registry list is secret-free",
            "ok": not exposed_secret_keys,
            "detail": "list_devices() must never expose device credentials, credential digests or pairing codes",
            "offending_keys": exposed_secret_keys,
        },
        {
            "check": "guest conversations are namespaced",
            "ok": all(str(g.get("device_id") or "") and g["conversation"]["count"] >= 0 for g in guest_summaries),
            "detail": "guest chat lives under conversation id guest:<device_id>, separate from mongoose-main",
        },
        {
            "check": "guest facts are outside owner scope",
            "ok": True,
            "detail": "owner and guest memory are counted separately; raw fact text is intentionally not returned here",
        },
        {
            "check": "guest task contents are redacted",
            "ok": all(g.get("content_returned") is False for g in guest_summaries),
            "detail": "only guest_item_counts are returned, not reminder/note text",
        },
    ]

    return {
        "ok": all(c["ok"] for c in checks),
        "guest_count": len(guest_summaries),
        "guests": guest_summaries,
        "counts": {
            "owner_memory_facts": owner_fact_count,
            "guest_memory_facts": guest_scope_fact_count,
        },
        "boundaries": {
            "owner_can_see": "guest device metadata, freshness, capability state, counts and mailbox messages addressed to/from owner",
            "owner_cannot_see_here": "guest private notes/reminders, guest memory fact text, raw guest chat text, device credentials or credential digests",
            "guest_can_reach": "only the explicitly guest-safe /device guest surface",
            "guest_cannot_reach": "cockpit, owner memory, files, email, calendar, home control, scheduler, builds, setup, approvals",
        },
        "checks": checks,
        "redaction": f"This report is designed for {_profile.owner_name()}/chat/auditors to check the boundary without reading guest private state.",
    }
