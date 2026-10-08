from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..settings import get_settings

# The brief is Mongoose's reason to exist. Every other capability (calendar,
# email, weather, home, tasks, memory, the agent bus) is a single domain that a
# dedicated app already covers. What no single-domain app can do is look across
# ALL of them at once and tell you the thing that matters: the clash, the
# weather that wrecks the plan, the task that's now urgent, the message waiting,
# the email that looks actionable, the remembered fact that bears on today, and
# above all the CROSS-domain link (an email from someone you're meeting; heating
# off before bad weather) that no per-domain app can make.
#
# This module gathers from every domain, computes the cross-domain signals
# deterministically (so the model can't hallucinate a conflict that isn't there),
# then asks the resident model to synthesise a short brief in Mongoose's voice.
# It runs on demand (`mongoose brief`) or scheduled (dropped on the bus).

# An email "looks actionable" if its (non-private) subject/snippet hits one of
# these fixed cues. Deterministic, so the model is told which mail to weight, not
# asked to guess.
_ACTION_EMAIL = re.compile(
    r"\b(urgent|asap|action required|please reply|reply needed|respond|deadline|due|overdue|"
    r"invoice|payment|renewal|quote|estimate|expires?|chase|follow[ -]?up|today|tomorrow)\b",
    re.IGNORECASE,
)

# Automated mail: autoresponders, no-reply senders, delivery notices, bounces. This mail is
# NEVER an actionable inbound request, so it must not be surfaced as "needs your attention".
# It matters because an autoresponder often ECHOES the owner's own subject line ("Re: Urgent:
# ...") and would otherwise trip _ACTION_EMAIL, turning a bounce-back into a fake urgent request.
_AUTO_SENDER = re.compile(
    r"(^|[<\s])(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply|mailer[-_.]?daemon|"
    r"postmaster|bounce|notifications?|automated?|auto[-_.]?reply|system|mailer)@",
    re.IGNORECASE,
)
_AUTO_SUBJECT = re.compile(
    r"^\s*(re:\s*)*(automatic reply|auto[- ]?reply|auto[- ]?response|autoresponse|out of (the )?office|"
    r"o\.?o\.?o\.?|undeliverable|delivery (status|has failed)|mail delivery|returned mail|"
    r"we('ve| have) received|thank you for (your|contacting|reaching)|this is an automated|"
    r"your (message|email|request) has been received|acknowledge?ment of receipt)",
    re.IGNORECASE,
)
# Transactional CONFIRMATIONS (a receipt for something already done): a payment/order confirmation is a
# record, never an inbound request the owner must reply to. Searched ANYWHERE in the subject (unlike the
# anchored _AUTO_SUBJECT), because these phrases usually follow a vendor prefix ("Docmail , Payment
# received for order 70000001"). Kept to unambiguous done-deal phrasing so a real "payment overdue"/
# "your invoice is due" is NOT swallowed (those are actionable and say due/overdue, not "received").
_AUTO_RECEIPT = re.compile(
    r"\b(payment received|payment (confirmation|confirmed)|confirmation of payment|"
    r"order (confirmation|confirmed|received|shipped|dispatched)|your order (has|is|number|#)|"
    r"receipt (for|of|no\.?|number)|your receipt|you'?ve joined|you have joined|"
    r"subscription (confirmed|renewed))\b",
    re.IGNORECASE,
)
# Reply/forward chain, and the weak "urgency" cues that are almost always the QUOTED original's word on
# such a subject (not this sender's). A lone weak cue on a forwarded subject must not promote it: the
# Harbour "RE: FW: ... Urgent request for verification of insurance from howden website" thread was
# surfaced exactly this way , the "Urgent" belongs to the original web-form request, not the human reply.
_FWD_PREFIX = re.compile(r"^\s*((re|fw|fwd)\s*:\s*)+", re.IGNORECASE)
_WEAK_ACTION = {"urgent", "asap"}

# --- Grounding guard --------------------------------------------------------
# The recurring "daily brief lie" is ALWAYS an over-claim of urgency: a quiet day (or a hedged "may need
# a reply" signal) written up as "urgent, needs your attention today". Chasing it with ever more email
# keyword filters is whack-a-mole: classifying "does this need a reply" is open-ended, so the next new
# phrasing always slips the denylist. This guard attacks the invariant instead of the examples: the brief
# may only ASSERT urgency/action when a signal actually flags something actionable. If it does not, and
# the prose still claims urgency, the brief is rejected and replaced with a deterministic one that can
# only restate the (conservative) signals. New mail phrasings can then under-surface, but never lie.
_URGENCY_CLAIM = re.compile(
    r"\b(urgent(ly)?|asap|immediately|right away|time[- ]sensitive|action required|"
    r"needs? (your )?(attention|action)|requires? (your )?(attention|action)|"
    r"reply (to|today|now|soon)|respond to|get back to|chase|follow up on|don'?t forget)\b",
    re.IGNORECASE,
)
# Negators near an urgency word mean the brief is SAYING THERE IS NONE ("nothing urgent", "no need to
# respond", "your day is clear") , that is honest, not a false alarm, so it must not trip the guard.
_NEGATORS = ("no ", "not ", "nothing", "none", "n't", "without", "quiet", "clear",
             "free of", "nobody", "isn", "aren", "don", "doesn", "wasn", "weren")


def _has_actionable_signal(signals: list[str]) -> bool:
    """True if some signal genuinely warrants action language (so urgency in the prose is grounded)."""
    for s in signals:
        if s.startswith("EMAIL:") and "may need a reply" in s:
            return True
        if s.startswith("TASKS:") and ("overdue" in s or "due today" in s):
            return True
        if s.startswith(("SOCIAL:", "BUS:")):
            return True
        if s.startswith("NOTIFICATION:") and "urgent" in s.lower():
            return True
    return False


def brief_is_grounded(text: str, signals: list[str]) -> bool:
    """False when the brief asserts urgency/action that no signal supports. Negated urgency ('nothing
    urgent') is fine. This is the resilient backstop: it does not enumerate automated mail, it enforces
    that the brief's urgency is a subset of the signals' urgency."""
    if _has_actionable_signal(signals):
        return True
    low = (text or "").lower()
    for m in _URGENCY_CLAIM.finditer(low):
        window = low[max(0, m.start() - 24):m.start()]
        if not any(neg in window for neg in _NEGATORS):
            return False
    return True


def _is_automated_email(e: dict[str, Any]) -> bool:
    """True for autoresponders, no-reply senders, bounces, and transactional confirmations (payment/order
    receipts): mail that is never a real inbound request the owner must action."""
    sender = str(e.get("sender") or "")
    subject = str(e.get("subject") or "")
    return bool(_AUTO_SENDER.search(sender) or _AUTO_SUBJECT.search(subject)
                or _AUTO_RECEIPT.search(subject))


def _is_actionable_email(e: dict[str, Any]) -> bool:
    """True only if the subject/sender carries a real action cue. A LONE weak cue (urgent/asap) on a
    reply/forward subject is treated as the quoted original's word, not the sender's, so a forwarded
    'RE: FW: ... Urgent ...' thread is not promoted unless it also carries a concrete cue (invoice, due,
    deadline, payment, ...). Automated mail is excluded by the caller before this runs."""
    subject = str(e.get("subject") or "")
    hay = f"{subject} {e.get('sender', '')}"
    matches = {m.group(0).lower() for m in _ACTION_EMAIL.finditer(hay)}
    if not matches:
        return False
    if _FWD_PREFIX.match(subject) and matches.issubset(_WEAK_ACTION):
        return False
    return True

# Words too common to indicate a real memory<->today match.
_STOPWORDS = {"the", "and", "for", "with", "from", "that", "this", "today",
              "tomorrow", "about", "your", "you", "have", "will"}


def _sanitize_text(value: Any, limit: int = 160) -> str:
    """Collapse whitespace and cap length, so nothing huge or multi-line is fed to
    the model and no snippet runs away."""
    text = " ".join(str(value or "").replace("\n", " ").replace("\r", " ").split())
    return (text[: limit - 1].rstrip() + "\u2026") if len(text) > limit else text


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", str(text or "").lower())
            if w not in _STOPWORDS}


async def gather() -> dict[str, Any]:
    """Pull the current picture from every domain. Each part is best-effort: a
    failing source must not sink the brief."""
    picture: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat()}

    from .calendar_tasks import events_today, tasks_today
    try:
        picture["events"] = events_today()
    except Exception:
        picture["events"] = []
    try:
        picture["tasks"] = tasks_today(include_open_without_due=True)
    except Exception:
        picture["tasks"] = []

    from .weather import weather_summary
    try:
        import asyncio
        import os
        timeout = float(os.getenv("MONGOOSE_BRIEF_WEATHER_TIMEOUT_SECONDS", "3"))
        picture["weather"] = await asyncio.wait_for(weather_summary(), timeout=timeout)
    except Exception:
        # The brief / Daily Operating Picture is a resident awareness surface; it must not
        # stall the whole Cockpit/voice path on a slow live weather provider. Weather is
        # useful context, but stale/missing weather is safer than a hanging daily picture.
        picture["weather"] = None

    from . import bus
    try:
        # Unread messages addressed to the owner, newest first. Exclude messages
        # the brief/builder themselves posted, so the brief doesn't surface its own
        # past output back at the user (a self-reference loop that grows daily).
        owner = get_settings().owner_id
        unread = bus.list_messages(to=owner, unread_only=True, limit=20)
        picture["bus_unread"] = [m for m in unread
                                 if m.get("sender") not in ("mongoose", "builder")][:10]
    except Exception:
        picture["bus_unread"] = []

    # Email received in the last day (metadata only; privacy preserved so the
    # signal layer can decide what's safe to surface).
    from .email_index import recent_email
    try:
        picture["email_recent"] = recent_email(hours=24, limit=20)
    except Exception:
        picture["email_recent"] = []

    # Notifications in the last day (urgency + privacy preserved).
    from .notifications import recent_notifications
    try:
        picture["notifications_recent"] = recent_notifications(hours=24, limit=30)
    except Exception:
        picture["notifications_recent"] = []

    # Social: recent WhatsApp activity and who's waiting on a reply. Privacy-classed;
    # the brief surfaces WHO and a count, not message content.
    try:
        from .whatsapp import unanswered_inbound
        from ..db import connect as _con
        with _con() as c:
            wa_recent = c.execute(
                "SELECT contact, direction, MAX(indexed_at) AS latest FROM whatsapp_messages "
                "GROUP BY contact ORDER BY latest DESC LIMIT 8").fetchall()
        picture["whatsapp_recent"] = [dict(r) for r in wa_recent]
        picture["whatsapp_waiting"] = unanswered_inbound()
    except Exception:
        picture["whatsapp_recent"] = []
        picture["whatsapp_waiting"] = []

    # Memory: how much durable context Mongoose holds, and whether any of it was
    # updated very recently (a hint the picture has shifted). Facts themselves are
    # NOT dumped into the brief; only counts and recency are used.
    from . import memory as mem
    try:
        picture["facts"] = mem.all_facts(limit=200)
    except Exception:
        picture["facts"] = []

    # Selected home-state (read-only, configured entities only). Best-effort and
    # bounded so a flaky LAN can't hang the brief.
    from .home_state import read_selected_home_state
    try:
        picture["home_state"] = await read_selected_home_state()
    except Exception:
        picture["home_state"] = []

    # Files modified in the last day (a freshly-edited file often relates to
    # today's work). Metadata only.
    from .file_index import recent_files
    try:
        picture["files_recent"] = recent_files(hours=24, limit=20)
    except Exception:
        picture["files_recent"] = []

    return picture


def _parse_iso(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_date(dt):
    """The date of an instant in the configured local timezone."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(get_settings().timezone)).date()
    except Exception:
        return dt.date()


def _today_terms(picture: dict[str, Any]) -> set[str]:
    """The concrete terms in today's evidence (event/task titles, non-private
    email subjects and notification titles). Used to decide which memory facts
    actually bear on today."""
    parts: list[str] = []
    for e in picture.get("events") or []:
        parts.append(str(e.get("title") or ""))
    for t in picture.get("tasks") or []:
        parts.append(str(t.get("title") or ""))
    for e in picture.get("email_recent") or []:
        if str(e.get("privacy", "")).lower() not in ("high", "secret"):
            parts.append(str(e.get("subject") or ""))
    for n in picture.get("notifications_recent") or []:
        if str(n.get("privacy", "")).lower() not in ("high", "secret"):
            parts.append(str(n.get("title") or ""))
    return _terms(" ".join(parts))


def relevant_memory_facts(picture: dict[str, Any], limit: int = 5) -> list[str]:
    """Memory facts that share concrete terms with today's evidence. Deterministic
    and intentionally simple: it surfaces a stored fact only when it actually
    connects to something happening today (so "drives a Honda" appears on a day
    with driving, not every day). The model is not asked to judge relevance."""
    keywords = _today_terms(picture)
    if not keywords:
        return []
    matched: list[str] = []
    for item in picture.get("facts") or []:
        fact = item.get("fact", "") if isinstance(item, dict) else str(item)
        if keywords & _terms(fact):
            matched.append(_sanitize_text(fact, 160))
        if len(matched) >= limit:
            break
    return matched


def cross_signals(picture: dict[str, Any]) -> list[str]:
    """Compute cross-domain facts DETERMINISTICALLY. These are ground truth the
    model is told to build on, not invent. Each returned string is a concrete,
    checked observation."""
    signals: list[str] = []
    events = picture.get("events") or []

    # 1. Calendar conflicts: overlapping events.
    parsed = []
    for e in events:
        s, en = _parse_iso(e.get("start_at", "")), _parse_iso(e.get("end_at", ""))
        if s and en:
            parsed.append((s, en, e.get("title", "an event")))
    parsed.sort()
    for i in range(len(parsed) - 1):
        s1, e1, t1 = parsed[i]
        s2, e2, t2 = parsed[i + 1]
        if s2 < e1:
            signals.append(f"CONFLICT: '{t1}' overlaps '{t2}'.")

    # 2. Weather vs outdoor-ish events: rain during an event window.
    wx = picture.get("weather") or {}
    rain = wx.get("rain_window") or wx.get("rain") or {}
    rain_desc = None
    if isinstance(rain, dict):
        rain_desc = rain.get("summary") or rain.get("window")
    if rain_desc and parsed:
        signals.append(f"WEATHER: rain expected ({rain_desc}); {len(parsed)} event(s) today, "
                       "check any that involve travel or being outdoors.")

    # Overdue / due-today tasks. Compare "today" in the configured local timezone,
    # not UTC, or tasks near midnight get misclassified.
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local_today = now.astimezone(ZoneInfo(get_settings().timezone)).date()
    except Exception:
        local_today = now.date()
    overdue, due_today = 0, 0
    for t in (picture.get("tasks") or []):
        due = _parse_iso(t.get("due_at", ""))
        if not due:
            continue
        due_date = _local_date(due)
        if due_date < local_today:
            overdue += 1
        elif due_date == local_today:
            due_today += 1
    if overdue:
        signals.append(f"TASKS: {overdue} task(s) overdue.")
    if due_today:
        signals.append(f"TASKS: {due_today} task(s) due today.")

    # 4. Waiting agent messages.
    unread = picture.get("bus_unread") or []
    if unread:
        kinds = ", ".join(sorted({m.get("type", "note") for m in unread}))
        signals.append(f"BUS: {len(unread)} unread message(s) for you ({kinds}).")

    # 4b. Social: WhatsApp continuity. Surface WHO is waiting on a reply (not the
    # message content), so the brief can gently support staying in touch.
    waiting = picture.get("whatsapp_waiting") or []
    if waiting:
        names = ", ".join(sorted({w.get("contact") for w in waiting if w.get("contact")})[:4])
        if names:
            signals.append(f"SOCIAL: waiting on a reply to {names}.")

    # 5. Email in the last day. Privacy-aware: count everything, but only name
    # senders/subjects for non-private mail, and flag mail that LOOKS ACTIONABLE
    # (invoice, deadline, reply needed) so the brief leads with what needs a reply.
    emails = picture.get("email_recent") or []
    if emails:
        private = [e for e in emails if str(e.get("privacy", "")).lower() in ("high", "secret")]
        ordinary = [e for e in emails if e not in private]
        # Automated mail (autoresponders, no-reply, bounces, receipts) is never an actionable
        # inbound request. Exclude it from the actionable set, or a "Re: Urgent: ..." bounce-back
        # gets reported as a live request needing attention (it isn't).
        automated = [e for e in ordinary if _is_automated_email(e)]
        actionable = [e for e in ordinary
                      if e not in automated and _is_actionable_email(e)]
        if actionable:
            examples = "; ".join(
                f"{(e.get('sender') or '?').split('<')[0].strip()}: {_sanitize_text(e.get('subject'), 60)}"
                for e in actionable[:3])
            signals.append(f"EMAIL: {len(actionable)} of {len(ordinary)} recent email(s) may need "
                           f"a reply: {examples}. Do not overstate urgency beyond what the subject shows.")
        elif ordinary:
            # No actionable mail. Report only the NON-automated senders, and say plainly that nothing
            # needs a reply, so the model does not turn autoresponders/receipts into "threads to untangle".
            human = [e for e in ordinary if not _is_automated_email(e)]
            if human:
                senders = ", ".join(sorted({(e.get("sender") or "?").split("<")[0].strip()
                                            for e in human})[:4])
                signals.append(f"EMAIL: {len(human)} new email(s) in the last day"
                               + (f" (from {senders})" if senders.strip(", ") else "")
                               + ", none flagged as needing a reply.")
            else:
                signals.append(f"EMAIL: {len(ordinary)} new message(s) in the last day, all automated "
                               "(autoresponders and receipts); nothing needs a reply.")
        if private:
            signals.append(f"EMAIL: {len(private)} sensitive message(s) (not detailed here).")

    # 6. Notifications: surface high/critical urgency from the last day, privacy
    # respected. Routine notifications are summarised as a count only.
    notifs = picture.get("notifications_recent") or []
    if notifs:
        urgent = [n for n in notifs if str(n.get("urgency", "")).lower() in ("critical", "high")]
        for n in urgent[:3]:
            dev = n.get("device") or ""
            on_dev = f" on {dev}" if dev and dev not in ("unknown", "") else ""
            if str(n.get("privacy", "")).lower() in ("high", "secret"):
                signals.append(f"NOTIFICATION: an urgent private alert from {n.get('app', 'an app')}{on_dev}.")
            else:
                signals.append(f"NOTIFICATION: {n.get('app', 'app')} - {n.get('title', 'urgent')}{on_dev}.")
        routine = len(notifs) - len(urgent)
        if routine > 0:
            signals.append(f"NOTIFICATION: {routine} other notification(s) in the last day.")

    # 7. CROSS-DOMAIN: an urgent/important email from someone you're meeting today.
    # This is the kind of link no single-domain app makes.
    if emails and parsed:
        attendee_terms = {w.lower() for _, _, title in parsed for w in title.split()
                          if len(w) > 3}
        for e in emails:
            sender = (e.get("sender") or "").lower()
            if any(term in sender for term in attendee_terms):
                signals.append("CROSS: you have recent email from someone tied to a meeting "
                               "today, worth a look before it.")
                break

    # 8. Memory relevance: surface stored facts that actually connect to today's
    # evidence (not a recency count). Facts ARE shown here because they matched
    # something already on today's agenda; still capped and sanitized.
    relevant = relevant_memory_facts(picture)
    if relevant:
        signals.append(f"MEMORY: {len(relevant)} stored fact(s) bear on today: "
                       + "; ".join(relevant[:3]) + ".")

    # 9. Selected home-state: surface notable states from the configured entities
    # (not a dump). Notable = unlocked locks, open doors/covers/windows, lights or
    # fans left on, low battery, or unavailable. The classification mirrors the
    # home_state reader's domain/device_class awareness.
    home = picture.get("home_state") or []
    for h in home:
        state = str(h.get("state", "")).lower()
        domain = h.get("domain", "")
        dclass = str(h.get("device_class", "")).lower()
        name = h.get("name", h.get("entity_id", "a device"))
        if state in ("unavailable", "unknown"):
            signals.append(f"HOME: {name} is unavailable.")
        elif domain == "lock" and state and state != "locked":
            signals.append(f"HOME: {name} is unlocked.")
        elif domain == "cover" and state in ("open", "opening"):
            signals.append(f"HOME: {name} is open.")
        elif domain == "binary_sensor" and dclass in ("door", "window", "garage_door", "opening") and state == "on":
            signals.append(f"HOME: {name} is open.")
        elif domain in ("light", "switch", "fan") and state == "on":
            signals.append(f"HOME: {name} is on.")
        elif domain == "climate" and state == "off":
            signals.append(f"HOME: {name} is off.")
        elif dclass == "battery" or "battery" in str(h.get("entity_id", "")).lower():
            try:
                if float(state) <= 20:
                    signals.append(f"HOME: {name} battery is low ({state}%).")
            except ValueError:
                pass

    # 11. Files modified in the last day. Surface a count, and name any whose
    # title shares a term with today's agenda (a file tied to a meeting/task).
    files = picture.get("files_recent") or []
    if files:
        today = _today_terms(picture)
        relevant_files = [f for f in files if today & _terms(f.get("title", ""))]
        if relevant_files:
            names = "; ".join(_sanitize_text(f.get("title") or f.get("path"), 60)
                              for f in relevant_files[:3])
            signals.append(f"FILES: {len(relevant_files)} recently-edited file(s) relate to "
                           f"today: {names}.")
        else:
            signals.append(f"FILES: {len(files)} file(s) edited in the last day.")

    # 10. CROSS-DOMAIN: heating off AND rain/cold weather flagged.
    if rain_desc and any(h.get("domain") == "climate"
                         and str(h.get("state", "")).lower() == "off" for h in home):
        signals.append("CROSS: heating is off and the weather looks poor, you may want it on.")

    return signals


def _brief_system() -> str:
    owner = get_settings().owner_id
    return (
        f"You are Mongoose writing a short daily brief for {owner}. You have the whole "
        "picture across the calendar, tasks, weather, email, notifications, your own "
        "memory, and selected home state, which no single app does. Write 2-4 short "
        "sentences that surface what actually matters TODAY, leading with anything "
        "time-sensitive, conflicting, or cross-cutting (e.g. an email from someone in "
        "today's meetings, or heating off before bad weather). Be specific and useful, "
        "not a list dump. Never read out private email or private notification content: "
        "the signals already mask those as counts, respect that. If the day is genuinely "
        "clear, say so briefly. Use the CONFIRMED SIGNALS as ground truth; do not invent "
        "conflicts, mail, or device states not present. NEVER call anything urgent, important, "
        "or say it needs attention or a reply unless a signal explicitly flags it that way: a "
        "'may need a reply' signal is the strongest you may echo, and a plain count of new mail "
        "means nothing needs action, so do not imply it does. Do not name a sender as needing a "
        "reply unless a signal names them. Under-stating a quiet day is fine; over-stating it is "
        "a lie. Mongoose's voice: warm, direct, a little dry."
    )


async def generate_brief(*, post_to_bus: bool = False) -> dict[str, Any]:
    """Produce the brief text. Optionally drop it on the bus as a message to the
    human so a scheduled run leaves it waiting for them."""
    picture = await gather()
    signals = cross_signals(picture)

    # Build a compact, model-friendly digest of the raw picture.
    events = picture.get("events") or []
    ev_lines = [f"- {e.get('title','?')} {(e.get('start_at') or '')[:16]}" for e in events[:8]]
    tasks = picture.get("tasks") or []
    task_lines = [f"- {t.get('title','?')} (due {(t.get('due_at') or '')[:16] or 'no date'})" for t in tasks[:8]]
    wx = picture.get("weather") or {}
    wx_line = wx.get("summary") or wx.get("headline") or "no weather data"

    emails = picture.get("email_recent") or []
    notifs = picture.get("notifications_recent") or []
    home = picture.get("home_state") or []
    files = picture.get("files_recent") or []
    relevant_mem = relevant_memory_facts(picture)
    # Home-state digest names only the configured entities and their state (no
    # private content); email/notification lines are counts, the detail lives in
    # the privacy-aware signals above.
    home_lines = [f"- {h.get('name', h.get('entity_id'))}: {h.get('state')}" for h in home[:8]]
    mem_lines = [f"- {m}" for m in relevant_mem[:6]]

    user = (
        "CONFIRMED SIGNALS (ground truth, build on these):\n"
        + ("\n".join(f"- {s}" for s in signals) if signals else "- (none flagged)")
        + "\n\nToday's events:\n" + ("\n".join(ev_lines) or "- none")
        + "\n\nOpen tasks:\n" + ("\n".join(task_lines) or "- none")
        + f"\n\nWeather: {wx_line}"
        + f"\n\nEmail in last 24h: {len(emails)}  Notifications: {len(notifs)}"
        + "\n\nMemory relevant to today:\n" + ("\n".join(mem_lines) or "- none")
        + "\n\nSelected home state:\n" + ("\n".join(home_lines) or "- none / not configured")
        + f"\n\nFiles edited in last 24h: {len(files)}"
        + "\n\nWrite the brief now."
    )

    from ..llm import ask_ollama, LocalModelError
    guard_tripped = False
    try:
        text = await ask_ollama(user, system=_brief_system(), role="chat")
        # Backstop: if the model inflated a non-actionable day into an urgency claim the signals do not
        # support, do not ship it , replace with the deterministic brief, which cannot lie. This is what
        # makes the fix resilient to email phrasings the signal classifier has never seen.
        if not brief_is_grounded(text, signals):
            guard_tripped = True
            text = _fallback_brief(signals)
    except LocalModelError as exc:
        # Even if the model is down, a deterministic fallback brief is useful.
        text = _fallback_brief(signals) if signals else "No brief: the local model is unavailable."
        text += f"\n(model unavailable: {exc})"

    result = {
        "brief": text.strip(),
        "signals": signals,
        "event_count": len(events),
        "task_count": len(tasks),
        "unread_count": len(picture.get("bus_unread") or []),
        "email_count": len(emails),
        "notification_count": len(notifs),
        "memory_context_count": len(relevant_mem),
        "home_state_count": len(home),
        "file_count": len(files),
        "grounding_guard_tripped": guard_tripped,
        "generated_at": picture["generated_at"],
    }

    if post_to_bus:
        from . import bus
        bus.send_message(sender="mongoose", to=get_settings().owner_id, type="brief",
                         subject=f"Daily brief: {datetime.now().strftime('%a %d %b')}",
                         body=result["brief"])

    # Leave a trustworthy trace: a receipt naming which domains fed the brief.
    try:
        from .journal_layer import receipt
        domains = []
        if picture.get("events"):
            domains.append("calendar")
        if picture.get("weather"):
            domains.append("weather")
        if picture.get("email_recent"):
            domains.append("email")
        if picture.get("whatsapp_recent"):
            domains.append("whatsapp")
        if picture.get("tasks"):
            domains.append("tasks")
        result["receipt"] = receipt("Brief posted", {"signals": len(signals), "domains": len(domains)})
    except Exception:
        pass

    return result


def _fallback_brief(signals: list[str]) -> str:
    """A plain brief built ONLY from the deterministic signals, no model. Used when the model is down OR
    when its output failed the grounding guard. Restates the signals verbatim-ish, so it cannot invent
    urgency: its honesty is structural, not a matter of the model behaving."""
    if not signals:
        return "Your day looks clear, nothing flagged across calendar, tasks, mail or home."
    return "Here is what is flagged today: " + " ".join(s.split(": ", 1)[-1] for s in signals)
