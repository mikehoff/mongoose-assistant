from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

# Natural-language time parsing for alarms/timers/reminders. Deterministic (no model): turn
# phrases like "7am tomorrow", "in 20 minutes", "6:45 on weekdays", "at 18:30" into an absolute
# fire time (+ optional recurrence). Honest: returns None when it can't parse, so the caller can
# ask rather than guess a wrong time (a wrong alarm time is a real failure).

_DURATION_RE = re.compile(
    r"\bin\s+(?:(\d+)\s*(?:h|hours?|hrs?))?\s*(?:(\d+)\s*(?:m|min|mins?|minutes?))?\s*(?:(\d+)\s*(?:s|sec|secs?|seconds?))?\b",
    re.IGNORECASE,
)
_DURATION_BARE = re.compile(
    r"\b(\d+)\s*(h|hours?|hrs?|m|min|mins?|minutes?|s|sec|secs?|seconds?)\b", re.IGNORECASE)

_CLOCK_RE = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6,
             "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thurs": 3,
             "fri": 4, "sat": 5, "sun": 6}


def parse_when(text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Parse a time phrase into {fire_at, kind_hint, duration_seconds, recurrence}. Returns None
    if no time can be confidently extracted. kind_hint is 'timer' for durations, else 'alarm'."""
    if not text:
        return None
    now = now or datetime.now(now.tzinfo if now else None) or datetime.now()
    if now.tzinfo is None:
        from datetime import timezone
        now = now.replace(tzinfo=timezone.utc)
    low = text.lower().strip()

    # 1) duration ("in 20 minutes", "in 1h30") -> timer
    dur = _parse_duration(low)
    if dur is not None and dur > 0:
        return {"fire_at": now + timedelta(seconds=dur), "kind_hint": "timer",
                "duration_seconds": dur, "recurrence": None}

    # 2) recurrence cues
    recurrence = None
    if re.search(r"\bweekdays?\b|\bevery weekday\b|\bon weekdays\b", low):
        recurrence = "weekdays"
    elif re.search(r"\bweekends?\b|\bon weekends\b", low):
        recurrence = "weekends"
    elif re.search(r"\bevery day\b|\bdaily\b|\beach day\b", low):
        recurrence = "daily"

    # 2.5) day/week/month offsets that carry NO clock time ("next week", "in 3 days", "tomorrow",
    # a bare named weekday). Without this, "nudge me next week" parsed to nothing and the assistant
    # confabulated a reminder it never set (the T04-class hallucination). Default to 9am local.
    off = _parse_day_offset(low, now)
    if off is not None:
        c = _parse_clock(low)
        hh, mm = c if c else (9, 0)
        target = (now + timedelta(days=off)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return {"fire_at": target, "kind_hint": "alarm", "duration_seconds": None, "recurrence": recurrence}

    # 3) clock time ("7am", "6:45", "at 18:30")
    clock = _parse_clock(low)
    if clock is None:
        return None
    hh, mm = clock

    # day offset: tomorrow / today / a named weekday / next occurrence of the time
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if "tomorrow" in low:
        target = target + timedelta(days=1)
    else:
        named = _named_weekday(low)
        if named is not None:
            # advance to the next instance of that weekday (today allowed if time is future)
            days_ahead = (named - now.weekday()) % 7
            if days_ahead == 0 and target <= now:
                days_ahead = 7
            target = target + timedelta(days=days_ahead)
        elif target <= now and not recurrence:
            # a bare past time today rolls to tomorrow (you mean the next 7am)
            target = target + timedelta(days=1)

    return {"fire_at": target, "kind_hint": "alarm",
            "duration_seconds": None, "recurrence": recurrence}


def _parse_day_offset(low: str, now: datetime) -> int | None:
    """Days from now for time phrases that carry NO clock ('next week', 'in 3 days', 'tomorrow',
    'next month', a bare weekday like 'monday'). None if no such phrase is present."""
    if re.search(r"\btomorrow\b", low):
        return 1
    if re.search(r"\bnext week\b|\bin a week\b|\bin one week\b|\ba week (from now|from today)\b", low):
        return 7
    if re.search(r"\bin a fortnight\b|\bnext fortnight\b|\bin two weeks\b", low):
        return 14
    if re.search(r"\bnext month\b|\bin a month\b", low):
        return 30
    m = re.search(r"\bin (\d+)\s*(day|days|week|weeks)\b", low)
    if m:
        return int(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
    nw = _named_weekday(low)
    if nw is not None:
        return (nw - now.weekday()) % 7 or 7
    return None


def _parse_duration(low: str) -> int | None:
    # compressed "1h30" / "1h30m" form
    cm = re.search(r"\b(\d+)h(\d{1,2})m?\b", low)
    if cm:
        return int(cm.group(1)) * 3600 + int(cm.group(2)) * 60
    m = _DURATION_RE.search(low)
    if m and any(m.groups()):
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        total = h * 3600 + mi * 60 + s
        if total > 0:
            return total
    # bare "20 minutes" / "30 min" without "in"
    m2 = _DURATION_BARE.search(low)
    if m2:
        n = int(m2.group(1))
        unit = m2.group(2).lower()
        if unit.startswith("h"):
            return n * 3600
        if unit.startswith("m"):
            return n * 60
        if unit.startswith("s"):
            return n
    return None


def _parse_clock(low: str) -> tuple[int, int] | None:
    # avoid matching a duration's number; only match clock-like patterns
    for m in _CLOCK_RE.finditer(low):
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        # skip nonsense (e.g. matched part of a duration we already handled)
        if hh > 23 or mm > 59:
            continue
        if ampm == "pm" and hh < 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
        # a bare number with no am/pm and no colon is ambiguous as a time unless preceded by
        # "at" or a recurrence/day word; require some time context to avoid matching "set 3"
        if not ampm and m.group(2) is None:
            ctx = low[max(0, m.start() - 4):m.start()]
            if "at" not in ctx and "by" not in ctx and "wake" not in low and "alarm" not in low:
                # still allow if a day/recurrence word is present
                if not (("tomorrow" in low) or _named_weekday(low) is not None
                        or re.search(r"weekday|weekend|daily|every", low)):
                    continue
        return (hh, mm)
    return None


def _named_weekday(low: str) -> int | None:
    for name, idx in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            return idx
    return None
