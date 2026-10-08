from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# The receipts ledger: one honest answer to "what did you do?".
#
# Receipts are a core trust feature, but until now they were scattered: device pairing
# receipts, the surpriser's suggestion log, scheduler job runs, WhatsApp draft decisions,
# the bus. This composes those EXISTING sources into a single, time-sorted, filterable
# ledger. It adds NO new write path and NO new autonomy, it makes the autonomy that
# already exists legible. Every entry says what happened, when, and which subsystem.
#
# An entry: {at, source, kind, text, status}. Sorted newest-first.


def _parse(ts: str | None):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _entry(at: str | None, source: str, kind: str, text: str, status: str = "") -> dict[str, Any]:
    return {"at": at or "", "source": source, "kind": kind, "text": text, "status": status}


def _device_entries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        from ..devices import device_receipts
        for r in device_receipts(50):
            out.append(_entry(r.get("at"), "devices", r.get("event", "device"),
                              f"{r.get('event')} · {r.get('device_name') or r.get('device_id') or ''}".strip(" ·"),
                              status="ok"))
    except Exception:
        pass
    return out


def _surpriser_entries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        from .surpriser import recent_suggestions
        for s in recent_suggestions(30):
            fb = s.get("feedback")
            out.append(_entry(s.get("created_at"), "surpriser", "suggestion",
                              f"Suggested: {s.get('text', '')}",
                              status=(fb or "surfaced")))
    except Exception:
        pass
    return out


def _scheduler_entries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        from .scheduler import job_status
        for j in job_status():
            if not j.get("last_run"):
                continue
            name = j.get("job") or j.get("name") or "job"
            status = j.get("last_status", "") or ""
            detail = j.get("last_detail", "") or ""
            tail = detail or status or "completed"
            out.append(_entry(j.get("last_run"), "scheduler", name,
                              f"Ran {name}: {tail}".strip(), status=status))
    except Exception:
        pass
    return out


def _model_tuning_entries() -> list[dict[str, Any]]:
    """Model-tuning (Lane 2) receipts: the last apply/revert, read from app_state where
    model_tuning records it (receipts are read from sources, not written separately)."""
    out: list[dict[str, Any]] = []
    try:
        import json as _json
        from ..db import get_app_state
        raw = get_app_state("lane2_last_autoapply", "")
        if not raw:
            return out
        rec = _json.loads(raw)
        at = rec.get("at")
        if "reverted_from" in rec:
            out.append(_entry(at, "model_tuning", "revert", "Reverted to base models"))
        elif rec.get("applied"):
            applied = ", ".join(f"{k}={v}" for k, v in rec["applied"].items())
            out.append(_entry(at, "model_tuning", "apply", f"Switched models: {applied}"))
    except Exception:
        pass
    return out


def _time_alerts_entries() -> list[dict[str, Any]]:
    """Time-alert receipts: what alarms/timers/reminders were set, cancelled or fired. The
    time_alerts table is the source of record, so the ledger reads it directly (consistent with
    how scheduler/device receipts are read)."""
    out: list[dict[str, Any]] = []
    try:
        from ..db import connect
        with connect() as con:
            rows = con.execute(
                "SELECT id, kind, label, fire_at, status, created_at, fired_at "
                "FROM time_alerts ORDER BY created_at DESC LIMIT 60"
            ).fetchall()
    except Exception:
        return out
    for r in rows:
        d = dict(r)
        kind = d.get("kind", "alert")
        label = d.get("label") or kind
        status = d.get("status", "")
        if status == "fired":
            out.append(_entry(d.get("fired_at") or d.get("created_at"), "time_alerts", kind,
                              f"{kind.capitalize()} fired: {label}", status="fired"))
        elif status == "cancelled":
            out.append(_entry(d.get("created_at"), "time_alerts", kind,
                              f"{kind.capitalize()} cancelled: {label}", status="cancelled"))
        else:
            out.append(_entry(d.get("created_at"), "time_alerts", kind,
                              f"{kind.capitalize()} set: {label}", status=status))
    return out


def _whatsapp_entries() -> list[dict[str, Any]]:
    """Draft decisions: drafted / sent / cancelled, the most trust-sensitive receipts
    (Mongoose acting, or correctly NOT acting, on your behalf)."""
    out: list[dict[str, Any]] = []
    try:
        from ..db import connect
        with connect() as con:
            # the table may not exist on a fresh DB
            t = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='whatsapp_drafts'").fetchone()
            if not t:
                return out
            rows = con.execute(
                "SELECT contact, status, created_at, decided_at FROM whatsapp_drafts "
                "ORDER BY id DESC LIMIT 30").fetchall()
        for r in rows:
            d = dict(r)
            status = d.get("status", "")
            when = d.get("decided_at") or d.get("created_at")
            if status == "sent":
                text = f"Sent a WhatsApp to {d.get('contact')} (after approval)."
            elif status == "cancelled":
                text = f"Did NOT send the WhatsApp draft to {d.get('contact')} (cancelled)."
            elif status == "pending":
                text = f"Drafted a WhatsApp to {d.get('contact')}, waiting for your approval."
            else:
                text = f"WhatsApp draft to {d.get('contact')}: {status}."
            out.append(_entry(when, "whatsapp", "draft", text, status=status))
    except Exception:
        pass
    return out


def receipts_ledger(limit: int = 50, source: str | None = None) -> dict[str, Any]:
    """The unified ledger: what Mongoose did, across subsystems, newest first. Optionally
    filter to one source (devices / surpriser / scheduler / whatsapp). Never raises."""
    entries: list[dict[str, Any]] = []
    entries.extend(_device_entries())
    entries.extend(_surpriser_entries())
    entries.extend(_scheduler_entries())
    entries.extend(_whatsapp_entries())
    entries.extend(_time_alerts_entries())
    entries.extend(_model_tuning_entries())

    if source:
        entries = [e for e in entries if e["source"] == source]

    # sort newest first; entries without a parseable timestamp sink to the bottom
    entries.sort(key=lambda e: (_parse(e["at"]) or datetime.min.replace(tzinfo=timezone.utc)),
                 reverse=True)
    entries = entries[:max(1, min(limit, 200))]

    by_source: dict[str, int] = {}
    for e in entries:
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1

    return {
        "entries": entries,
        "count": len(entries),
        "by_source": by_source,
        "sources": ["devices", "surpriser", "scheduler", "whatsapp", "time_alerts", "model_tuning"],
        "note": ("This is what Mongoose has recorded doing. An empty or thin ledger means "
                 "little has happened yet, not that something is wrong."),
    }


def format_ledger(ledger: dict[str, Any]) -> str:
    """Render the ledger as calm text for the CLI."""
    entries = ledger.get("entries", [])
    if not entries:
        return "Mongoose hasn't recorded doing anything yet. As it syncs, runs jobs, and " \
               "drafts or sends on your approval, those actions will show up here."
    out = [f"What Mongoose did ({ledger.get('count', 0)} recent receipts):"]
    for e in entries:
        when = (e.get("at") or "").replace("T", " ")[:16]
        status = f" [{e['status']}]" if e.get("status") else ""
        out.append(f"  {when}  {e['source']}: {e['text']}{status}")
    return "\n".join(out)
