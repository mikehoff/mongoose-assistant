from __future__ import annotations

from datetime import datetime
from typing import Any

# Firing + delivery for Time Alerts.
#
# Delivery floor (always attempted): phone push via ntfy , already real, reports success
# honestly. Upgrade (only if the target devices + HA tts/notify are configured): announce on a
# speaker. We record which channels actually delivered, so the ledger never claims a wake-up
# reached you when it didn't.
#
# Speaker announce is gated behind an explicit setting (MONGOOSE_ALERT_ANNOUNCE) AND requires
# the HA tts/notify services to be allowlisted. Until the user opts in, alerts deliver to the
# phone , the honest floor , and say so.


async def fire_due_alerts(now: datetime | None = None) -> dict[str, Any]:
    """Fire every alert whose time has arrived. Called each scheduler tick. For each: deliver
    (push + optional announce), mark fired, and if recurring, schedule the next occurrence."""
    from . import time_alerts as TA
    due = TA.due_alerts(now=now)
    fired = []
    for alert in due:
        result = await deliver_alert(alert)
        delivered = result.get("delivered") or []
        if delivered:
            TA.mark_fired(alert["id"])
            status = "fired"
        else:
            # If the phone/channel is unavailable, do not mark the reminder as successfully fired and
            # disappear it. Users discover this after a day: "it says it fired but I never got it".
            # Mark it failed with a visible reason so the alerts panel can explain what happened.
            failures = result.get("failures") or []
            reason = "; ".join(
                f"{f.get('channel', 'channel')}: {f.get('reason', 'unknown')}"
                for f in failures[:3] if isinstance(f, dict)
            ) or "no delivery channel reported success"
            TA.mark_status(alert["id"], TA.FAILED, detail=reason[:400])
            status = "failed"
        # recurring alarms chain forward as fresh one-shots even if today's delivery failed; otherwise
        # one bad phone/push outage kills tomorrow's recurring alarm too.
        nxt = TA.reschedule_recurring(alert, now=now)
        fired.append({"id": alert["id"], "kind": alert["kind"], "label": alert.get("label", ""),
                      "status": status, "delivered": delivered, "failures": result.get("failures", []),
                      "rescheduled": bool(nxt)})
    return {"fired_count": len(fired), "fired": fired}


async def deliver_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Deliver one alert. Phone push is the floor; speaker announce is attempted only when
    enabled and targets exist. Returns which channels actually delivered (honest, not assumed)."""
    delivered: list[str] = []
    failures: list[dict[str, str]] = []

    kind = alert.get("kind", "alert")
    label = alert.get("label") or _default_label(kind)
    title, body = _phrase(kind, label, alert)

    # 1) phone push , the guaranteed floor
    want_push = "push" in (alert.get("delivery") or ["push"])
    if want_push:
        try:
            from .notifications import send_to_phone
            tag = {"alarm": "alarm_clock", "timer": "hourglass", "reminder": "bell"}.get(kind, "bell")
            res = await send_to_phone(
                title, body, priority="high", tags=tag,
                target_device_profiles=("primary_phone", "home_tablet"),
            )
            if res.get("sent"):
                delivered.append("push")
            else:
                failures.append({"channel": "push", "reason": res.get("reason", "unknown")})
        except Exception as exc:
            failures.append({"channel": "push", "reason": str(exc)})

    # 2) speaker announce , opt-in upgrade, only if enabled + targets present
    targets = alert.get("targets") or []
    if targets and _announce_enabled():
        for target in targets:
            try:
                ok = await _announce_on_device(target, f"{title}. {body}".strip())
                if ok:
                    delivered.append(f"announce:{target}")
                else:
                    failures.append({"channel": f"announce:{target}", "reason": "not_delivered"})
            except Exception as exc:
                failures.append({"channel": f"announce:{target}", "reason": str(exc)})

    return {"delivered": delivered, "failures": failures}


def _announce_enabled() -> bool:
    """Speaker announce is off until the user explicitly enables it (it widens what Mongoose
    does to the house). The floor (phone push) works regardless."""
    import os
    return os.getenv("MONGOOSE_ALERT_ANNOUNCE", "false").strip().lower() in ("1", "true", "yes", "on")


async def _announce_on_device(target: str, text: str) -> bool:
    """Announce text on a speaker via Home Assistant tts/notify. Returns whether it was
    delivered. This is the ONLY place that needs the tts/notify HA services; if they're not
    allowlisted/configured, it returns False (and the floor still delivered to the phone)."""
    try:
        from .home_router import announce_via_home_assistant
        return await announce_via_home_assistant(target, text)
    except Exception:
        return False


def _default_label(kind: str) -> str:
    return {"alarm": "Alarm", "timer": "Timer done", "reminder": "Reminder"}.get(kind, "Alert")


def _phrase(kind: str, label: str, alert: dict[str, Any]) -> tuple[str, str]:
    """The notification title + body for an alert."""
    if kind == "alarm":
        return ("Alarm", label if label != "Alarm" else "Time to wake up.")
    if kind == "timer":
        mins = (alert.get("duration_seconds") or 0) // 60
        suffix = f" ({mins} min)" if mins else ""
        return ("Timer", f"{label}{suffix}" if label and label != "Timer done" else f"Timer done{suffix}.")
    if kind == "reminder":
        return ("Reminder", label)
    return ("Mongoose", label)
