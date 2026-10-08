from __future__ import annotations

from typing import Any

from ..db import connect, utc_now_iso

# Diary, receipts, and "what changed?".
#
# These three make the system feel solid and honest rather than chatty:
#   - DIARY: a local, readable trace of what Mongoose did (not emotional journalling
#     unless asked). Continuity without sentimentality.
#   - RECEIPTS: a short, truthful summary after a piece of work, so you can trust
#     what happened ("Indexed 318 files, found 12 attachments, skipped 2 unreadable").
#   - WHAT CHANGED: turns noise into meaningful deltas since you last looked.


# --- diary -------------------------------------------------------------------
def log_diary(kind: str, summary: str) -> None:
    """Append one line to the local activity diary. kind is a coarse category
    (ritual|search|whatsapp|brief|notify|file|other)."""
    with connect() as con:
        con.execute("INSERT INTO diary_entries(at, kind, summary) VALUES (?, ?, ?)",
                    (utc_now_iso(), kind, summary))


def diary_today() -> list[dict[str, Any]]:
    from datetime import datetime, timezone
    start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as con:
        rows = con.execute(
            "SELECT at, kind, summary FROM diary_entries WHERE at >= ? ORDER BY at",
            (start,)).fetchall()
        return [dict(r) for r in rows]


def diary_week() -> dict[str, Any]:
    """A weekly roll-up: counts by kind plus ritual completion stats, the
    'this week Mongoose...' view."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with connect() as con:
        rows = con.execute(
            "SELECT kind, COUNT(*) c FROM diary_entries WHERE at >= ? GROUP BY kind",
            (cutoff,)).fetchall()
        by_kind = {r["kind"]: r["c"] for r in rows}
    from .rituals import ritual_stats
    return {"by_kind": by_kind, "rituals": ritual_stats(7)["by_ritual"]}


def render_diary_today() -> str:
    """Human-readable 'Today Mongoose:' list."""
    entries = diary_today()
    if not entries:
        return "Today Mongoose: nothing logged yet."
    lines = [f"- {e['summary']}" for e in entries]
    return "Today Mongoose:\n" + "\n".join(lines)


# --- receipts ----------------------------------------------------------------
def receipt(action: str, parts: dict[str, Any]) -> str:
    """Build a short, honest completion summary and log it to the diary. `parts`
    are the concrete facts of what happened, e.g. {"indexed": 318, "attachments": 12,
    "skipped": 2}. Returns the one-line receipt text."""
    bits = []
    for k, v in parts.items():
        bits.append(f"{v} {k}")
    body = ", ".join(bits) if bits else "no changes"
    text = f"Done. {action}: {body}." if bits else f"Done. {action}."
    log_diary("other", text)
    return text


# --- what changed ------------------------------------------------------------
def _checkpoint(con, domain: str) -> str:
    row = con.execute("SELECT last_seen FROM change_checkpoints WHERE domain = ?",
                      (domain,)).fetchone()
    return row["last_seen"] if row else "1970-01-01T00:00:00+00:00"


def _set_checkpoint(con, domain: str, when: str) -> None:
    con.execute(
        "INSERT INTO change_checkpoints(domain, last_seen) VALUES (?, ?) "
        "ON CONFLICT(domain) DO UPDATE SET last_seen = excluded.last_seen", (domain, when))


def what_changed(*, acknowledge: bool = True) -> dict[str, Any]:
    """Cross-domain delta since the last time you looked: new WhatsApps, new email,
    newly-indexed files, new notifications. Returns a small structured summary and,
    by default, advances the checkpoints so the next call shows only what's new
    after this. Pass acknowledge=False to peek without resetting."""
    now = utc_now_iso()
    changes: list[str] = []
    detail: dict[str, Any] = {}

    empty_states: list[str] = []
    sources_checked: list[str] = []

    with connect() as con:
        # WhatsApp: new inbound messages since checkpoint.
        if _has(con, "whatsapp_messages"):
            sources_checked.append("whatsapp")
            cp = _checkpoint(con, "whatsapp")
            rows = con.execute(
                "SELECT contact, COUNT(*) c FROM whatsapp_messages "
                "WHERE direction='in' AND indexed_at > ? GROUP BY contact", (cp,)).fetchall()
            if rows:
                who = ", ".join(f"{r['contact']} ({r['c']})" for r in rows if r["contact"])
                changes.append(f"WhatsApp: new messages from {who}")
                detail["whatsapp"] = [{"contact": r["contact"], "count": r["c"]} for r in rows]
            if not rows:
                empty_states.append("No WhatsApp signals changed since the last checkpoint.")
            if acknowledge:
                _set_checkpoint(con, "whatsapp", now)
        else:
            empty_states.append("No WhatsApp signals yet: WAHA is not linked or no messages are indexed.")

        # Email since checkpoint.
        if _has(con, "email_messages"):
            sources_checked.append("email")
            cp = _checkpoint(con, "email")
            n = con.execute("SELECT COUNT(*) FROM email_messages WHERE indexed_at > ?", (cp,)).fetchone()[0]
            if n:
                changes.append(f"Email: {n} new message(s) indexed")
                detail["email"] = n
            if not n:
                empty_states.append("No mail deltas since the last checkpoint.")
            if acknowledge:
                _set_checkpoint(con, "email", now)
        else:
            empty_states.append("No mail signals yet: no local email index is available.")

        # Files newly indexed.
        if _has(con, "files"):
            sources_checked.append("files")
            cp = _checkpoint(con, "files")
            n = con.execute("SELECT COUNT(*) FROM files WHERE indexed_at > ?", (cp,)).fetchone()[0]
            if n:
                changes.append(f"Files: {n} newly indexed")
                detail["files"] = n
            if not n:
                empty_states.append("No file changes since the last checkpoint.")
            if acknowledge:
                _set_checkpoint(con, "files", now)
        else:
            empty_states.append("No file signals yet: no indexed roots are available.")

        # Notifications worth knowing about.
        if _has(con, "notifications"):
            sources_checked.append("notifications")
            cp = _checkpoint(con, "notifications")
            try:
                # Same-source rule (register B4): the "N new" count must be derived from
                # the SAME query the view renders, filtered to newer-than-checkpoint. A raw
                # COUNT against the checkpoint alone once said "1 new" for a row too old
                # for the view's window, so the button opened onto "Nothing recent": the
                # counter and the list must be incapable of disagreeing.
                from .notifications import recent_notifications as _rn
                _view_items = _rn(hours=720, limit=200)
                n = sum(1 for it in _view_items if (it.get("received_at") or "") > cp)
            except Exception:
                n = 0
            if n:
                changes.append(f"Notifications: {n} new")
                detail["notifications"] = n
            if not n:
                empty_states.append("No notification changes since the last checkpoint.")
            if acknowledge:
                _set_checkpoint(con, "notifications", now)
        else:
            empty_states.append("No notification signals yet: device/HA notification routing is not feeding Mongoose.")

        # Paired-device state events (future Android Companion / PWA contribution).
        if _has(con, "device_events"):
            sources_checked.append("devices")
            cp = _checkpoint(con, "devices")
            rows = con.execute(
                "SELECT event_type, COUNT(*) c FROM device_events WHERE created_at > ? GROUP BY event_type",
                (cp,)).fetchall()
            if rows:
                changes.append("Devices: " + ", ".join(f"{r['event_type']} ({r['c']})" for r in rows))
                detail["devices"] = [{"event_type": r["event_type"], "count": r["c"]} for r in rows]
            else:
                empty_states.append("No Android/device signals changed since the last checkpoint.")
            if acknowledge:
                _set_checkpoint(con, "devices", now)
        else:
            empty_states.append("No Android signals yet: no paired device events are being recorded.")

    summary = ("Nothing changed since you last looked. Nothing needs your nervous system."
               if not changes else
               f"{len(changes)} thing(s) changed: " + "; ".join(changes) + ".")
    return {
        "changed": bool(changes),
        "count": len(changes),
        "summary": summary,
        "changes": changes,
        "detail": detail,
        "since": {"checkpointed_domains": sources_checked},
        "empty_states": empty_states[:8],
    }


def _has(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
