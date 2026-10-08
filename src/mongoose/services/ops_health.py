"""Proactive ops-health checks that PUSH the owner, not just log.

Born from a real failure: the off-machine (Google Drive) backup stopped working on 23 July 2026 and ran
for seven weeks failing every night. Mongoose DID record each failure (a bug event + an observation, and
the Backups card showed it) but nothing ever surfaced it, so the owner never knew the RTM books had no
off-machine copy. Recording is not alerting. This module turns 'recorded' into 'alerted' for the few
subsystems whose SILENT failure is genuinely costly. Backup freshness is the first check; the shape
(assess -> issues -> deduped push) is meant to grow (tunnel down, sync stalled, etc.).

Read-only assessment (`assess_ops_health`) is separate from the side-effecting push (`run_ops_health_check`)
so the daily brief and the cockpit can show the same truth without sending anything.
"""
from __future__ import annotations

import datetime
from typing import Any

from ..db import get_app_state, set_app_state

# Off-machine backup older than this (or missing) is a problem worth a push. Daily backups run ~04:45,
# so 48h tolerates one missed/late night without crying wolf but still catches a real stall on day 2.
_STALE_HOURS = 48.0
_PUSH_DEDUP_HOURS = 20.0  # at most one push per issue-kind per ~day, so a persistent fault pings once daily


def _parse_iso(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        s = str(value).strip().replace("Z", "+00:00")
        d = datetime.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def assess_ops_health() -> dict[str, Any]:
    """Check the critical subsystems and return {ok, issues:[{kind, severity, title, detail}], checked}.
    Read-only: it never sends or writes. severity is 'alert' (push high) or 'warn' (push default)."""
    issues: list[dict[str, str]] = []
    now = _now()

    # ---- Off-machine backup freshness (the case this module was built for) ----
    try:
        from ..settings import get_settings
        s = get_settings()
        if not getattr(s, "backup_enabled", False):
            issues.append({
                "kind": "backup_disabled", "severity": "alert",
                "title": "Backups are OFF",
                "detail": "MONGOOSE_BACKUP_ENABLED is not set, so the brain (including the RTM books) is "
                          "not being backed up at all."})
        else:
            from ..backup_drive import is_connected, connection_health, list_drive_backups
            if not is_connected():
                issues.append({
                    "kind": "backup_drive_unconnected", "severity": "warn",
                    "title": "Off-machine backup not connected",
                    "detail": "Google Drive is not connected; backups are local-only, so a disk loss would "
                              "take the RTM books with it."})
            else:
                ch = connection_health() or {}
                if not ch.get("ok"):
                    # Connected (token present) but Drive calls fail: exactly the state that hid for 7
                    # weeks (Drive API disabled in the Cloud project). Alert, and don't trust the (empty)
                    # listing below as 'no backups'.
                    issues.append({
                        "kind": "backup_drive_error", "severity": "alert",
                        "title": "Off-machine backup is failing",
                        "detail": "Drive is connected but every call fails: "
                                  + str(ch.get("reason", "unknown"))[:200]})
                else:
                    import re

                    def _fn_ts(name: Any) -> datetime.datetime | None:
                        m = re.search(r"(\d{8}T\d{6})", str(name or ""))
                        if not m:
                            return None
                        try:
                            return datetime.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
                                tzinfo=datetime.timezone.utc)
                        except Exception:
                            return None

                    lst = list_drive_backups() or []
                    stamps = [t for t in (_fn_ts(x.get("name")) for x in lst) if t]
                    newest = max(stamps) if stamps else None
                    age_h = None if newest is None else (now - newest).total_seconds() / 3600.0
                    if newest is None or age_h > _STALE_HOURS:
                        age = "never" if newest is None else f"{age_h:.0f}h ago"
                        issues.append({
                            "kind": "backup_stale", "severity": "alert",
                            "title": "Off-machine backup is stale",
                            "detail": f"Newest off-machine (Drive) backup is {age}; expected within "
                                      f"{int(_STALE_HOURS)}h. The off-machine copy of the RTM books may be "
                                      f"old or missing ({len(lst)} in Drive)."})
    except Exception as exc:  # never let a health check itself go bang silently
        issues.append({"kind": "backup_check_error", "severity": "warn",
                       "title": "Backup health check errored", "detail": str(exc)[:200]})

    return {"ok": not issues, "issues": issues, "checked": now.isoformat()}


def brief_health_line() -> str:
    """A short line for the daily brief / its push. '' when everything is healthy, so the brief only
    grows a warning when there is one."""
    rep = assess_ops_health()
    if rep.get("ok"):
        return ""
    return " | ".join(f"WARNING: {i['title']} ({i['detail']})" for i in rep["issues"])


async def run_ops_health_check() -> dict[str, Any]:
    """Assess, then PUSH any issue to the owner's phone, deduped to once per ~day per issue-kind so a
    standing fault pings once daily rather than every cycle. Returns a summary."""
    rep = assess_ops_health()
    if rep.get("ok"):
        return {"ok": True, "issues": 0}
    from .notifications import send_to_phone
    now = _now()
    pushed: list[str] = []
    held: list[str] = []
    for issue in rep["issues"]:
        key = f"ops_health_pushed:{issue['kind']}"
        last = _parse_iso(get_app_state(key))
        if last and (now - last).total_seconds() < _PUSH_DEDUP_HOURS * 3600:
            held.append(issue["kind"])
            continue
        pri = "high" if issue.get("severity") == "alert" else "default"
        try:
            r = await send_to_phone("Mongoose: " + issue["title"], issue["detail"][:300],
                                    priority=pri, tags="warning")
        except Exception as exc:
            r = {"sent": False, "error": str(exc)[:120]}
        if r.get("sent"):
            set_app_state(key, now.isoformat())
            pushed.append(issue["kind"])
        else:
            held.append(issue["kind"])
    return {"ok": False, "issues": len(rep["issues"]), "pushed": pushed, "held_deduped": held}
