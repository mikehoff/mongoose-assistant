from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# The Daily Operating Picture (DOP): Mongoose's flagship synthesis.
#
# This is NOT another dashboard card that reads one table. It is the higher-order
# organisation of EVERY domain Mongoose can see (calendar, weather, email, WhatsApp,
# tasks, files, home state, memory, system health, approvals) into a single structured
# "shape of the day":
#
#   today's shape   - the spine of the day: events, weather, the headline
#   what changed    - cross-domain delta since you last looked
#   what needs you  - approvals waiting, replies owed, decisions, repairs (the triage)
#   what can wait   - real but non-urgent
#   what's broken   - capabilities not configured / failing
#   what Mongoose did - recent receipts (honest trace)
#   next smallest step - the single most useful thing to do now
#
# FACTS BEFORE FLUENCY: every section is COMPUTED here from real signals. The model may
# later phrase the headline, but it never invents what's waiting, what changed, or what's
# broken. Cross-domain LINKS (an email from someone in today's calendar; weather that
# threatens an event) are the whole point, those are what no single-purpose tool can do.
#
# Reuses existing services (brief.gather, journal_layer.what_changed, whatsapp drafts,
# doctor) rather than duplicating any data path.


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _local_now():
    from ..settings import get_settings
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(get_settings().timezone))
    except Exception:
        return datetime.now(timezone.utc)


def _terms(text: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in (text or "")).split()
            if len(w) > 3}


def _contact_terms(name: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in (name or "")).split()
            if len(w) > 2}


def _cross_links(picture: dict[str, Any]) -> list[dict[str, str]]:
    """The heart of synthesis: connections BETWEEN domains that no single app can see.
    - an email or WhatsApp from someone you have a calendar event with today
    - weather that threatens an outdoor-looking event
    - a file recently edited that matches an event/task
    Each link is a short, evidence-grounded statement."""
    links: list[dict[str, str]] = []
    events = picture.get("events") or []

    # people you're meeting today (from event titles/attendees)
    event_people: set[str] = set()
    for e in events:
        event_people |= _contact_terms(str(e.get("title") or ""))
        for a in (e.get("attendees") or []):
            event_people |= _contact_terms(str(a))

    # email from someone you're meeting today
    for em in (picture.get("email_recent") or []):
        if str(em.get("privacy", "")).lower() in ("high", "secret"):
            continue
        sender = str(em.get("sender") or em.get("from") or "")
        if event_people & _contact_terms(sender):
            links.append({
                "kind": "email_meets_calendar",
                "text": f"Email from {sender.split('<')[0].strip() or sender}, who appears in today's calendar.",
            })

    # WhatsApp waiting from someone you're meeting today
    for w in (picture.get("whatsapp_waiting") or []):
        contact = str(w.get("contact") or "")
        if event_people & _contact_terms(contact):
            links.append({
                "kind": "whatsapp_meets_calendar",
                "text": f"Unanswered WhatsApp from {contact}, who you have a calendar event with today.",
            })

    # weather threatens an outdoor-looking event
    weather = picture.get("weather") or {}
    wsummary = str((weather or {}).get("summary") or "").lower()
    bad_weather = any(k in wsummary for k in ("rain", "storm", "snow", "wind", "shower"))
    if bad_weather:
        for e in events:
            title = str(e.get("title") or "").lower()
            if any(k in title for k in ("walk", "run", "park", "garden", "outdoor", "cycle", "ride", "match")):
                links.append({
                    "kind": "weather_threatens_event",
                    "text": f"Weather ({(weather or {}).get('summary')}) may affect '{e.get('title')}'.",
                })

    # a recently-edited file matching an event/task
    today_terms: set[str] = set()
    for e in events:
        today_terms |= _terms(str(e.get("title") or ""))
    for t in (picture.get("tasks") or []):
        today_terms |= _terms(str(t.get("title") or ""))
    for f in (picture.get("files_recent") or []):
        fname = str(f.get("name") or f.get("path") or "")
        if _terms(fname) & today_terms and today_terms:
            links.append({
                "kind": "file_matches_today",
                "text": f"Recently edited '{fname}' relates to something on today.",
            })
            break

    return links


def _todays_shape(picture: dict[str, Any]) -> dict[str, Any]:
    """The spine of the day: timed events ahead, weather, a one-line headline."""
    now = _local_now()
    events = picture.get("events") or []
    upcoming = []
    for e in events:
        start = _parse_iso(e.get("start_at") or e.get("start") or e.get("start_time"))
        when = None
        if start:
            try:
                from zoneinfo import ZoneInfo
                from ..settings import get_settings
                local = start.astimezone(ZoneInfo(get_settings().timezone))
            except Exception:
                local = start
            when = local.strftime("%H:%M")
            if local < now and e.get("busy_status") != "all_day" and not e.get("all_day"):
                continue  # already passed
        upcoming.append({"title": e.get("title"), "when": when, "all_day": bool(e.get("all_day"))})
    weather = picture.get("weather") or {}
    return {
        "date": now.strftime("%A %d %B"),
        "events_ahead": upcoming,
        "event_count": len(upcoming),
        "weather": (weather or {}).get("summary"),
    }


def _what_needs_you(picture: dict[str, Any], doctor: dict[str, Any]) -> list[dict[str, Any]]:
    """The triage queue: things that genuinely need the user, joined across domains.
    Each item has a kind, a one-line text, and (where possible) how to act."""
    items: list[dict[str, Any]] = []

    # approvals waiting (WhatsApp drafts)
    try:
        from .whatsapp import list_drafts
        for d in list_drafts("pending"):
            items.append({"kind": "approval", "urgency": "now",
                          "text": f"Approve or edit the WhatsApp draft to {d.get('contact')}.",
                          "act": "Approvals tab"})
    except Exception:
        pass

    # replies owed (unanswered inbound)
    for w in (picture.get("whatsapp_waiting") or [])[:5]:
        items.append({"kind": "reply", "urgency": "soon",
                      "text": f"Reply to {w.get('contact')} on WhatsApp.",
                      "act": "open thread"})

    # actionable email (already flagged by brief logic if present)
    for em in (picture.get("email_recent") or []):
        if em.get("looks_actionable"):
            subj = "" if str(em.get("privacy", "")).lower() in ("high", "secret") else (em.get("subject") or "")
            items.append({"kind": "email", "urgency": "soon",
                          "text": f"An email looks like it needs action{(': ' + subj) if subj else ''}.",
                          "act": "open email"})

    # repairs: degraded/missing capabilities from doctor
    for key, c in (doctor.get("checks") or {}).items():
        if c and c.get("ok") is False and key in ("scheduler", "ollama", "database"):
            items.append({"kind": "repair", "urgency": "now" if key in ("ollama", "database") else "soon",
                          "text": f"{key} is not healthy: {c.get('detail', '')}".strip(),
                          "act": "Setup & Repair"})

    # readiness gaps: the readiness layer knows about more than doctor (stale Gmail, a
    # disconnected WhatsApp session, a device missing permissions). Promote ONLY the gaps that
    # genuinely need attention AND affect something the user relies on, so this stays a triage
    # queue, not a nag. De-dupe against the repairs already added above by capability id.
    try:
        from .readiness import collect_all
        seen_ids = {"scheduler", "ollama", "local_model", "database"}
        for it in collect_all():
            if it.id in seen_ids or not it.needs_attention:
                continue
            if not it.affects:           # a gap that affects nothing isn't worth surfacing
                continue
            na = it.next_action or {}
            # severity -> urgency: critical=now, warning=soon
            urgency = "now" if it.severity == "critical" else "soon"
            items.append({"kind": "readiness", "urgency": urgency,
                          "text": it.why or f"{it.label} needs attention.",
                          "act": na.get("label", "Setup & Repair"),
                          "affects": it.affects})
    except Exception:
        pass

    # rank: now before soon
    order = {"now": 0, "soon": 1, "later": 2}
    items.sort(key=lambda i: order.get(i.get("urgency", "later"), 3))
    return items


def _what_can_wait(picture: dict[str, Any]) -> list[str]:
    """Real but non-urgent: recent files, lower-priority context."""
    out = []
    files = picture.get("files_recent") or []
    if files:
        out.append(f"{len(files)} file(s) indexed in the last day.")
    tasks = [t for t in (picture.get("tasks") or []) if not t.get("due")]
    if tasks:
        out.append(f"{len(tasks)} open task(s) with no due date.")
    return out


def _whats_broken(doctor: dict[str, Any]) -> list[dict[str, str]]:
    """Capabilities not configured or failing, honestly surfaced."""
    out = []
    for key, c in (doctor.get("checks") or {}).items():
        if c and c.get("ok") is False:
            out.append({"capability": key, "detail": str(c.get("detail", ""))})
    return out


def _next_smallest_step(needs: list[dict[str, Any]], broken: list[dict[str, str]]) -> str:
    """The single most useful next action. Prefer an urgent need; else a repair; else
    a calm confirmation."""
    now_items = [n for n in needs if n.get("urgency") == "now"]
    if now_items:
        return now_items[0]["text"]
    if needs:
        return needs[0]["text"]
    if broken:
        return f"Configure {broken[0]['capability']} when you have a moment."
    return "Nothing needs you right now. The day is in hand."


async def daily_operating_picture() -> dict[str, Any]:
    """Compose the full Daily Operating Picture from every available domain. Never
    raises; each section degrades independently."""
    # the cross-domain picture (reuses the brief's gatherer, the one source)
    try:
        from .brief import gather
        picture = await gather()
    except Exception:
        picture = {}

    # system health (for what's broken / repairs)
    try:
        from ..doctor import run_doctor
        doctor = run_doctor(timeout_seconds=1.0)
    except Exception:
        doctor = {"checks": {}}

    # cross-domain delta (peek, don't acknowledge, so the DOP doesn't consume it)
    try:
        from .journal_layer import what_changed
        changed = what_changed(acknowledge=False)
    except Exception:
        changed = {"changes": [], "detail": {}}

    # recent receipts (what Mongoose did) - device + (future) action receipts
    receipts: list[dict[str, Any]] = []
    try:
        from ..devices import device_receipts
        for r in device_receipts(5):
            receipts.append({"at": r.get("at"), "text": f"{r.get('event')} · {r.get('device_name') or ''}".strip(" ·")})
    except Exception:
        pass

    needs = _what_needs_you(picture, doctor)
    broken = _whats_broken(doctor)
    links = _cross_links(picture)

    return {
        "generated_at": _local_now().isoformat(),
        "todays_shape": _todays_shape(picture),
        "cross_domain_links": links,
        "what_changed": changed.get("changes", []),
        "what_needs_you": needs,
        "what_can_wait": _what_can_wait(picture),
        "whats_broken": broken,
        "what_mongoose_did": receipts,
        "next_smallest_step": _next_smallest_step(needs, broken),
        "counts": {
            "needs_you": len(needs),
            "changed": len(changed.get("changes", [])),
            "broken": len(broken),
            "links": len(links),
        },
    }


def format_dop(dop: dict[str, Any]) -> str:
    """Render the DOP as calm text for the CLI."""
    out = []
    shape = dop.get("todays_shape", {})
    out.append(f"=== Today: {shape.get('date', '')} ===")
    if shape.get("weather"):
        out.append(f"Weather: {shape['weather']}")
    evs = shape.get("events_ahead", [])
    if evs:
        out.append("Ahead today:")
        for e in evs:
            when = e.get("when") or ("all day" if e.get("all_day") else "")
            out.append(f"  {when}  {e.get('title')}".rstrip())
    else:
        out.append("No timed events ahead today.")

    links = dop.get("cross_domain_links", [])
    if links:
        out.append("\nConnections worth noticing:")
        for l in links:
            out.append(f"  • {l['text']}")

    needs = dop.get("what_needs_you", [])
    out.append("\nWhat needs you:" if needs else "\nWhat needs you: nothing right now.")
    for n in needs:
        flag = "!" if n.get("urgency") == "now" else "-"
        out.append(f"  {flag} {n['text']}")

    changed = dop.get("what_changed", [])
    if changed:
        out.append("\nWhat changed since you last looked:")
        for c in changed:
            out.append(f"  • {c}")

    broken = dop.get("whats_broken", [])
    if broken:
        out.append("\nNot configured / needs attention:")
        for b in broken:
            out.append(f"  • {b['capability']}: {b['detail']}")

    out.append(f"\nNext smallest step: {dop.get('next_smallest_step', '')}")
    return "\n".join(out)
