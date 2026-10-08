from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

# Time Alerts: a unified alarm / timer / reminder layer.
#
# The honest design (verified against what actually exists in this codebase):
#  - Native Echo/Google alarm creation is NOT a clean API. We attempt a native route where one
#    exists, verify the outcome where we can, and DOWNGRADE to accepted_unverified when we
#    can't , the same discipline v0.30 established for home control.
#  - The GUARANTEED delivery floor is Mongoose-owned: a local ledger + the scheduler firing the
#    alert + phone push (ntfy, which is already real and reports success honestly). Speaker
#    announce (HA tts/notify) is an opt-in UPGRADE on top, not the floor.
#  - Mongoose keeps its OWN ledger even when a native device owns the actual alarm, so
#    "what did you set?" / "what did you cancel?" is always answerable. This table is also the
#    receipt source (receipts.py reads it, consistent with how it reads other domains).
#
# Three kinds in one model:
#  - alarm: wall-clock ("7am tomorrow"). fire_at is the absolute time.
#  - timer: countdown ("20 minutes"). Resolved to an absolute fire_at AT CREATION, so firing
#           logic is identical to alarms; the original duration is kept for display.
#  - reminder: wall-clock + a text payload ("remind me to call mum at 3").

_KINDS = ("alarm", "timer", "reminder")

# statuses mirror the home-control honesty vocabulary
SCHEDULED = "scheduled"            # Mongoose-owned, will fire locally
CONFIRMED = "confirmed"           # native route created AND verified
ACCEPTED_UNVERIFIED = "accepted_unverified"  # native attempted, couldn't confirm
CANCELLED = "cancelled"
FIRED = "fired"
FAILED = "failed"


def _ensure_table() -> None:
    from ..db import connect
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS time_alerts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT,
                fire_at TEXT NOT NULL,          -- ISO8601 absolute time the alert fires
                duration_seconds INTEGER,       -- for timers: original countdown (display)
                recurrence TEXT,                -- null | 'weekdays' | 'daily' | 'weekends'
                targets TEXT,                   -- comma-separated device/group names (announce)
                provider TEXT NOT NULL,         -- mongoose | alexa | google | home_assistant
                native_id TEXT,                 -- the native provider's id, if any
                status TEXT NOT NULL,
                delivery TEXT,                  -- how it will be delivered: push,announce
                created_at TEXT NOT NULL,
                fired_at TEXT,
                detail TEXT
            )
            """
        )
        con.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def create_alert(
    *,
    kind: str,
    fire_at: datetime,
    label: str = "",
    duration_seconds: int | None = None,
    recurrence: str | None = None,
    targets: list[str] | None = None,
    provider: str = "mongoose",
    native_id: str | None = None,
    status: str = SCHEDULED,
    delivery: list[str] | None = None,
) -> dict[str, Any]:
    """Record an alert in the ledger. This is always done, even when a native device owns the
    real alarm, so Mongoose can always answer what it set."""
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {_KINDS}")
    _ensure_table()
    aid = uuid.uuid4().hex[:12]
    from ..db import connect
    with connect() as con:
        con.execute(
            "INSERT INTO time_alerts(id, kind, label, fire_at, duration_seconds, recurrence, "
            "targets, provider, native_id, status, delivery, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, kind, label, _iso(fire_at), duration_seconds, recurrence,
             ",".join(targets or []), provider, native_id, status,
             ",".join(delivery or ["push"]), _iso(_now())),
        )
        con.commit()
    return get_alert(aid)


def get_alert(alert_id: str) -> dict[str, Any] | None:
    _ensure_table()
    from ..db import connect
    with connect() as con:
        row = con.execute("SELECT * FROM time_alerts WHERE id = ?", (alert_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_alerts(*, kind: str | None = None, active_only: bool = True,
                include_fired: bool = False) -> list[dict[str, Any]]:
    """Alerts in the ledger. By default the upcoming/active ones (what the user would ask
    'what alarms do I have?'). Sorted by fire time."""
    _ensure_table()
    from ..db import connect
    q = "SELECT * FROM time_alerts"
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if active_only:
        keep = [SCHEDULED, CONFIRMED, ACCEPTED_UNVERIFIED]
        if include_fired:
            keep.append(FIRED)
        clauses.append(f"status IN ({','.join('?' * len(keep))})")
        params.extend(keep)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY fire_at ASC"
    with connect() as con:
        rows = con.execute(q, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def cancel_alert(alert_id: str, *, detail: str = "") -> dict[str, Any] | None:
    """Cancel a single alert. Returns the updated row, or None if it didn't exist."""
    _ensure_table()
    from ..db import connect
    with connect() as con:
        cur = con.execute(
            "UPDATE time_alerts SET status = ?, detail = ? WHERE id = ? AND status IN (?,?,?)",
            (CANCELLED, detail or "cancelled by user", alert_id,
             SCHEDULED, CONFIRMED, ACCEPTED_UNVERIFIED),
        )
        con.commit()
        if cur.rowcount == 0:
            return None
    return get_alert(alert_id)


def mark_fired(alert_id: str) -> None:
    _ensure_table()
    from ..db import connect
    with connect() as con:
        con.execute("UPDATE time_alerts SET status = ?, fired_at = ? WHERE id = ?",
                    (FIRED, _iso(_now()), alert_id))
        con.commit()


def mark_status(alert_id: str, status: str, *, native_id: str | None = None,
                detail: str = "") -> None:
    _ensure_table()
    from ..db import connect
    with connect() as con:
        if native_id is not None:
            con.execute("UPDATE time_alerts SET status = ?, native_id = ?, detail = ? WHERE id = ?",
                        (status, native_id, detail, alert_id))
        else:
            con.execute("UPDATE time_alerts SET status = ?, detail = ? WHERE id = ?",
                        (status, detail, alert_id))
        con.commit()


def due_alerts(now: datetime | None = None) -> list[dict[str, Any]]:
    """Alerts whose fire time has arrived and that haven't fired/cancelled yet. The scheduler
    firing job reads this each tick. ~60s precision (the scheduler tick), stated honestly."""
    _ensure_table()
    now = now or _now()
    from ..db import connect
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM time_alerts WHERE status IN (?,?,?) AND fire_at <= ?",
            (SCHEDULED, CONFIRMED, ACCEPTED_UNVERIFIED, _iso(now)),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def reschedule_recurring(alert: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """For a recurring alarm, compute the next occurrence and create a fresh scheduled row.
    Returns the new alert, or None if not recurring. Keeps recurrence as a chain of one-shots so
    firing logic stays a single 'is it due?' check."""
    rec = (alert.get("recurrence") or "").lower()
    if rec not in ("daily", "weekdays", "weekends"):
        return None
    now = now or _now()
    base = _parse_iso(alert["fire_at"]) or now
    nxt = base + timedelta(days=1)
    # advance to the next day that matches the recurrence rule
    for _ in range(8):
        wd = nxt.weekday()  # 0=Mon .. 6=Sun
        if rec == "daily" or (rec == "weekdays" and wd < 5) or (rec == "weekends" and wd >= 5):
            break
        nxt = nxt + timedelta(days=1)
    return create_alert(
        kind=alert["kind"], fire_at=nxt, label=alert.get("label", ""),
        recurrence=alert.get("recurrence"),
        targets=alert.get("targets") or None,
        provider=alert.get("provider", "mongoose"),
        delivery=alert.get("delivery") or ["push"],
    )


# --- helpers -----------------------------------------------------------------

def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["targets"] = d["targets"].split(",") if d.get("targets") else []
    d["delivery"] = d["delivery"].split(",") if d.get("delivery") else ["push"]
    return d


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# --- high-level orchestration (parse -> native attempt -> ledger) ------------

def set_from_text(text: str, *, kind: str | None = None, targets: list[str] | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """The one call the CLI/voice use: parse a natural phrase, attempt a native provider where a
    route exists (best-effort, marked unverified when unconfirmable), and ALWAYS record in the
    ledger with a Mongoose-owned fallback so delivery is guaranteed. Returns the alert + a
    human 'say' line that's honest about what was and wasn't verified."""
    from .time_parse import parse_when
    parsed = parse_when(text, now=now)
    if not parsed:
        return {"ok": False, "say": "I couldn't work out a time from that. Try 'at 7am tomorrow' "
                "or 'in 20 minutes'.", "alert": None}

    if kind is None and re.search(r"\bremind\b|\breminder\b", text, re.IGNORECASE):
        kind = "reminder"
    resolved_kind = kind or parsed["kind_hint"]
    # reminders and alarms are both wall-clock; only timers carry a duration
    label = _extract_label(text, resolved_kind)
    delivery = ["push"]
    if targets:
        delivery.append("announce")

    # attempt native creation where a route exists (currently: none verified -> fall straight to
    # mongoose-owned, marked clearly). The ladder is here so native slots in without a rewrite.
    provider, status, native_id, note = _attempt_native(resolved_kind, parsed, targets)

    alert = create_alert(
        kind=resolved_kind, fire_at=parsed["fire_at"], label=label,
        duration_seconds=parsed.get("duration_seconds"),
        recurrence=parsed.get("recurrence"), targets=targets,
        provider=provider, native_id=native_id, status=status, delivery=delivery,
    )
    return {"ok": True, "alert": alert, "say": _say_for(alert, note)}


def _attempt_native(kind: str, parsed: dict, targets: list[str] | None) -> tuple[str, str, str | None, str]:
    """Best-effort native provider attempt. Returns (provider, status, native_id, note).

    Honest current state: there is no verified native alarm-creation route wired yet (Alexa/
    Google alarm creation isn't a clean API, and the announce path is opt-in). So this returns a
    Mongoose-owned SCHEDULED alert. When a native route is added, it sets provider/native_id and
    either CONFIRMED (verified via sensor) or ACCEPTED_UNVERIFIED (couldn't confirm)."""
    # placeholder ladder rung , intentionally returns mongoose-owned for now.
    return ("mongoose", SCHEDULED, None, "")


def _extract_label(text: str, kind: str) -> str:
    """Pull a label out of the phrase (the 'to call mum' in 'remind me to call mum at 3')."""
    low = text.strip()
    m = None
    import re
    if kind == "reminder":
        m = re.search(r"\bto\s+(.+?)(?:\s+(?:at|in|on|tomorrow|every|by)\b.*)?$", low, re.IGNORECASE)
    if m and m.group(1):
        return m.group(1).strip().rstrip(".")
    # otherwise a generic label by kind
    return {"alarm": "", "timer": "", "reminder": ""}.get(kind, "")


def _recurrence_phrase(rec: str | None) -> str:
    if not rec:
        return ""
    return {"daily": " every day", "weekdays": " on weekdays", "weekends": " on weekends"}.get(rec, f" {rec}")


def _say_for(alert: dict[str, Any], note: str = "") -> str:
    """An honest spoken confirmation: when it'll fire, how it'll reach you, and any caveat."""
    when = _parse_iso(alert["fire_at"])
    kind = alert["kind"]
    delivery = alert.get("delivery") or ["push"]
    chan = "your phone" if delivery == ["push"] else "your phone and the speaker"
    rec = alert.get("recurrence")
    when_s = when.strftime("%-I:%M %p") if when else "the set time"
    if kind == "timer":
        mins = (alert.get("duration_seconds") or 0) // 60
        base = f"Timer set for {mins} minute{'s' if mins != 1 else ''}. I'll alert {chan}."
    elif kind == "reminder":
        lbl = alert.get("label") or "your reminder"
        rec_s = _recurrence_phrase(rec)
        base = f"I'll remind you to {lbl} at {when_s}{rec_s} on {chan}."
    else:
        rec_s = _recurrence_phrase(rec)
        base = f"Alarm set for {when_s}{rec_s}. I'll wake you on {chan}."
    if note:
        base += f" {note}"
    if delivery == ["push"] and alert.get("targets"):
        base += " (Speaker announce is off; it'll come to your phone. Enable announce to also play on the speaker.)"
    return base
