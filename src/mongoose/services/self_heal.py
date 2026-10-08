"""Self-heal: close the loop from liveness DETECTION to recovery ACTION.

liveness() honestly reports per-subsystem freshness, but on its own it only tells the user to
run a command. The periodic scheduler jobs (sync_email, sync_tasks, reindex_files) keep data
fresh in the normal case. The gap this closes: a subsystem that is configured, whose periodic
job exists, but which has gone stale FAR beyond its sync interval: meaning the job is silently
failing (expired token, dropped IMAP, a wedged provider). We:

  1. detect that specific "stale past its own interval" condition,
  2. attempt ONE immediate recovery run of the relevant job,
  3. record the incident + outcome so the user (and the cockpit) can see what self-healed,

without inventing a parallel scheduler. Recovery reuses the exact same job functions the
scheduler runs, so there is one code path, not two.

Never raises into the scheduler. Cheap when everything is healthy (a few DB reads).
"""
from __future__ import annotations

from typing import Any

from ..db import audit, connect, utc_now_iso

# How many multiples of a subsystem's expected sync interval count as "silently failing".
# 3x interval with no fresh data => the periodic job is not doing its job; try to heal.
_STALE_MULTIPLIER = 3.0

# subsystem -> (expected interval minutes attr on settings, default minutes, recovery job name)
_RECOVERABLE = {
    "email": ("email_sync_minutes", 30, "sync_email"),
    "calendar": ("tasks_sync_minutes", 30, "sync_tasks"),
    "files": ("reindex_minutes", 360, "reindex_files"),
}


def _interval_minutes(attr: str, default: int) -> float:
    try:
        from ..settings import get_settings
        return float(getattr(get_settings(), attr, default) or default)
    except Exception:
        return float(default)


def _ensure_incident_table() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS self_heal_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subsystem TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                stale_hours REAL,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT
            )
            """
        )


def _record_incident(subsystem: str, stale_hours: float | None, action: str,
                     outcome: str, detail: str = "") -> None:
    _ensure_incident_table()
    with connect() as con:
        con.execute(
            "INSERT INTO self_heal_incidents(subsystem, detected_at, stale_hours, action, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (subsystem, utc_now_iso(), stale_hours, action, outcome, detail[:1000]),
        )
    audit("self_heal", action, outcome == "recovered",
          payload={"subsystem": subsystem, "outcome": outcome})


async def _run_recovery_job(job_name: str) -> dict[str, Any]:
    """Run a scheduler job by name, reusing the exact same function the scheduler uses."""
    from . import scheduler as SCH
    reg = SCH._build_registry()
    entry = reg.get(job_name)
    if not entry:
        return {"ok": False, "error": f"no such job: {job_name}"}
    _spec, fn = entry
    try:
        result = await fn()
        if isinstance(result, dict):
            return result
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def run_self_heal() -> dict[str, Any]:
    """Detect silently-failing subsystems and attempt one recovery run each.
    Returns {ok, checked, healed, still_failing, incidents}."""
    try:
        from ..liveness import liveness
        rep = liveness()
    except Exception as exc:
        return {"ok": False, "error": f"liveness probe failed: {exc}", "healed": [], "still_failing": []}

    healed: list[str] = []
    still_failing: list[str] = []
    checked = 0

    for check in rep.get("checks", []):
        subsystem = (check.get("subsystem") or "").split()[0].lower()  # "email", "calendar", etc.
        if subsystem not in _RECOVERABLE:
            continue
        state = check.get("state", "")
        # Only act on a CONFIGURED subsystem that is stale (not "unused"/"not configured").
        if state not in ("stale", "STALE"):
            continue
        attr, default, job = _RECOVERABLE[subsystem]
        interval_min = _interval_minutes(attr, default)
        stale_hours = check.get("age_hours")
        # "Silently failing" = stale beyond _STALE_MULTIPLIER * interval. If we can't read an age,
        # treat a reported stale state as worth one heal attempt.
        threshold_hours = (_STALE_MULTIPLIER * interval_min) / 60.0
        if stale_hours is not None and stale_hours < threshold_hours:
            continue  # stale but within normal jitter; the next periodic run will catch it
        checked += 1
        result = await _run_recovery_job(job)
        if result.get("ok", False) is not False:
            healed.append(subsystem)
            _record_incident(subsystem, stale_hours, f"recover:{job}", "recovered",
                             detail=str(result)[:500])
        else:
            still_failing.append(subsystem)
            _record_incident(subsystem, stale_hours, f"recover:{job}", "still_failing",
                             detail=str(result.get("error") or result)[:500])

    return {"ok": True, "checked": checked, "healed": healed, "still_failing": still_failing}


def recent_incidents(limit: int = 20) -> list[dict[str, Any]]:
    _ensure_incident_table()
    with connect() as con:
        rows = con.execute(
            "SELECT subsystem, detected_at, stale_hours, action, outcome, detail "
            "FROM self_heal_incidents ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def self_heal_summary() -> dict[str, Any]:
    """Fast read-only summary for the cockpit / brain tool. No recovery run."""
    incidents = recent_incidents(limit=10)
    recovered = sum(1 for i in incidents if i["outcome"] == "recovered")
    failing = sum(1 for i in incidents if i["outcome"] == "still_failing")
    return {
        "recent_incidents": incidents,
        "recovered_count": recovered,
        "still_failing_count": failing,
        "last_incident_at": incidents[0]["detected_at"] if incidents else None,
    }
