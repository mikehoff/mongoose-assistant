from __future__ import annotations

from typing import Any

# Presenters: turn the full readiness report into surface-shaped answers.
#
# The readiness REPORT is the truth (13+ items with evidence). But the surfaces want very
# different shapes:
#   - VOICE: one-line status + the top 1-3 gaps + ONE next action. Spoken, ranked, short.
#     Never a 13-item readout.
#   - PHONE: a status headline + a few action cards + what THIS device can do. No secrets,
#     no raw private content.
#   - COCKPIT: the full evidence graph (that's just the report itself).
#
# This is the layer that makes "Mongoose, are you ready?" → "Mostly. Three things need you."

from .readiness import (
    cached_readiness, _SEVERITY_RANK,
    READY, HAS_DATA, NOT_CONFIGURED, OFF_BY_DESIGN, DISABLED,
)

_OVERALL_PHRASE = {
    "ready": "Core brain is up and nothing configured is failing.",
    "degraded": "Mongoose is usable, but not fully connected.",
    "not_ready": "Mongoose isn't ready for daily use yet.",
}


def _attention_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Items that need the user, ranked by severity (most urgent first)."""
    att = [it for it in report["items"]
           if it.get("severity") in ("critical", "warning")]
    att.sort(key=lambda it: _SEVERITY_RANK.get(it.get("severity", "info"), 0), reverse=True)
    return att


def _working_labels(report: dict[str, Any]) -> list[str]:
    return [it["label"] for it in report["items"]
            if it.get("status") in (READY, HAS_DATA)]


def readiness_for_voice(mode: str = "short") -> dict[str, Any]:
    """A spoken-shaped answer to 'are you ready?'. Short by default: one summary line, the top
    gaps (max 3), and a single suggested action. Returns both a `speech` string (what to say)
    and structured fields (so the caller can act)."""
    report = cached_readiness()
    overall = report["overall"]
    attention = _attention_items(report)
    working = _working_labels(report)

    lead = _OVERALL_PHRASE.get(overall, "Here's where things stand.")

    # name a couple of things that ARE working, so it's not all-negative
    parts = [lead]
    if overall != "ready" and working:
        good = working[:3]
        parts.append(f"Working: {_join(good)}.")

    # the top gaps, phrased as short clauses
    top = attention[:3]
    if top:
        gap_clauses = [_gap_clause(it) for it in top]
        parts.append(f"{_join(gap_clauses, sep='; ')}.")
    elif overall == "ready":
        parts.append("Nothing needs you.")

    # one suggested next action (the most urgent that's a safe/runnable thing)
    action = _first_actionable(attention)
    if action:
        parts.append(action["spoken"])

    speech = " ".join(parts)
    return {
        "speech": speech,
        "overall": overall,
        "gaps": [{"label": it["label"], "why": it.get("why", ""),
                  "next_action": it.get("next_action")} for it in top],
        "working": working,
        "attention_count": len(attention),
    }


def needs_me_for_voice() -> dict[str, Any]:
    """Spoken answer to 'what needs me?'. Ranked, max ~3, action-oriented."""
    report = cached_readiness()
    attention = _attention_items(report)
    if not attention:
        return {"speech": "Nothing needs you right now. Everything that's set up is working.",
                "items": []}
    top = attention[:3]
    n = len(attention)
    clauses = [_need_clause(it) for it in top]
    head = (f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} you. "
            if n else "")
    body = _join(clauses, sep="; ") + "."
    return {"speech": head + body,
            "items": [{"label": it["label"], "why": it.get("why", ""),
                       "next_action": it.get("next_action")} for it in top]}


def what_is_broken_for_voice() -> dict[str, Any]:
    """Spoken answer to 'what's broken?'. Only genuinely failing/critical things."""
    report = cached_readiness()
    broken = [it for it in report["items"] if it.get("severity") == "critical"
              or it.get("status") == "failing"]
    if not broken:
        return {"speech": "Nothing's broken. Some things may be stale or not set up, but nothing's failing.",
                "items": []}
    clauses = [it["label"].lower() for it in broken[:3]]
    return {"speech": f"{_join(clauses)} {'is' if len(clauses) == 1 else 'are'} not working. "
                      + (broken[0].get("why", "")),
            "items": [{"label": it["label"], "why": it.get("why", "")} for it in broken]}


def watching_for_voice() -> dict[str, Any]:
    """Spoken answer to 'what are you watching?'. The live signal sources, briefly."""
    report = cached_readiness()
    watched = [it["label"] for it in report["items"]
               if it.get("status") not in (NOT_CONFIGURED, OFF_BY_DESIGN, DISABLED)
               and it.get("affects")]
    if not watched:
        return {"speech": "I'm not actively watching any sources yet, nothing's connected.",
                "items": watched}
    return {"speech": f"I'm watching {_join(watched[:6])}.", "items": watched}


def readiness_for_phone(device_id: str | None = None) -> dict[str, Any]:
    """Phone-shaped: a status headline, the top action cards, what's ready, and (if a device
    is identified) what THIS device can do. No secrets, no raw private content."""
    report = cached_readiness()
    overall = report["overall"]
    attention = _attention_items(report)
    working = _working_labels(report)

    headline = {
        "ready": "Core brain ready",
        "degraded": f"Mongoose is usable, with {len(attention)} gap{'s' if len(attention) != 1 else ''}",
        "not_ready": "Mongoose isn't ready yet",
    }.get(overall, "Mongoose status")

    cards = []
    for it in attention[:4]:
        na = it.get("next_action") or {}
        cards.append({
            "title": it["label"],
            "why": it.get("why", ""),
            "urgency": _urgency(it.get("severity", "info")),
            "action": {"label": na.get("label", ""), "kind": na.get("kind", "")} if na else None,
            "affects": it.get("affects", []),
        })

    out = {
        "headline": headline,
        "status": overall,
        "top_cards": cards,
        "ready": working,
        "not_ready": [it["label"] for it in report["items"]
                      if it.get("status") in ("not_configured", "needs_authorisation")],
    }

    # this-device block (capabilities, freshness, what's missing) , no secrets
    if device_id:
        out["device"] = _device_block(device_id)
    return out


# --- phrasing helpers --------------------------------------------------------

def _gap_clause(it: dict[str, Any]) -> str:
    """A short spoken clause for a gap, e.g. 'Gmail is stale' / 'WhatsApp is disconnected'."""
    label = it["label"]
    status = it.get("status", "")
    phrase = {
        "needs_authorisation": f"{label} needs sign-in",
        "stale": f"{label} is stale",
        "degraded": f"{label} needs attention",
        "failing": f"{label} isn't working",
        "unknown": f"{label} is unverified",
    }.get(status, f"{label} needs attention")
    return phrase


def _need_clause(it: dict[str, Any]) -> str:
    na = it.get("next_action") or {}
    if na.get("label"):
        return f"{it['label']} ({na['label'].lower()})"
    return it["label"]


def _first_actionable(attention: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the most urgent item with a SAFE, runnable action and phrase an offer to do it."""
    for it in attention:
        na = it.get("next_action")
        if not na:
            continue
        kind = na.get("kind", "")
        if kind in ("runs_sync", "safe_repair", "opens_url", "starts_service"):
            label = na["label"].lower()
            if kind == "opens_url":
                spoken = f"I can open {label} if you'd like."
            elif kind == "starts_service":
                # label is usually itself a verb phrase ("install the scheduler service")
                spoken = f"I can {label} if you'd like."
            else:  # runs_sync / safe_repair
                spoken = f"I can {label} now if you'd like."
            return {"spoken": spoken, "action": na}
    return None


def _device_block(device_id: str) -> dict[str, Any]:
    try:
        from ..devices import list_devices
        for d in list_devices():
            if d.get("device_id") == device_id and not d.get("revoked"):
                cap = d.get("capability_status", {})
                can = [k for k, v in cap.items() if v == "enabled"] if isinstance(cap, dict) else d.get("capabilities", [])
                missing = [k for k, v in cap.items() if v == "available"] if isinstance(cap, dict) else []
                return {
                    "name": d.get("name", "device"),
                    "role": d.get("profile", "unknown"),
                    "last_seen": (d.get("freshness", {}) or {}).get("detail", ""),
                    "can": can,
                    "missing": missing,
                }
    except Exception:
        pass
    return {}


def _urgency(severity: str) -> str:
    return {"critical": "high", "warning": "medium", "info": "low"}.get(severity, "low")


def _join(items: list[str], sep: str = ", ") -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if sep == ", " and len(items) == 2:
        return f"{items[0]} and {items[1]}"
    if sep == ", ":
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    return sep.join(items)
