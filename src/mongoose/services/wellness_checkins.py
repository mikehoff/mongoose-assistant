"""Structured mindfulness / wellness check-in reminders.

This is deliberately small and private: it nudges the owner and trusted guests to do a
brief self-journal, but it does not ask Mongoose to read, rate or share the journal
content. The actual private reflections stay with the person unless they choose to
write something back.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..db import connect, get_app_state, set_app_state, utc_now_iso

DEFAULT_OWNER_PROMPT = (
    "Two-minute structured check-in: mood/energy 1-10, one body signal, one emotion, "
    "what you need, and one tiny kind action. Private by default; share only what helps."
)
DEFAULT_GUEST_PROMPT = (
    "A gentle Mongoose wellness nudge: take two private minutes to self-journal mood/energy "
    "1-10, one body signal, one emotion, what you need, and one tiny kind action. "
    "You do not need to share the content."
)
DEEP_MEMORY_FACT = (
    "Structured mindfulness and wellness self-journalling check-ins matter to the owner and their trusted "
    "guests. Mongoose should gently support brief private check-ins through scheduler and Surpriser, "
    "without guilt, nagging, exposing private journal content, or broadcasting owner nudges to guests."
)

_STATE_PREFIX = "wellness_checkins_"


def deep_memory_fact() -> str:
    """The standing memory fact, worded for this household (site profile lexicon wellness.deep_memory_fact
    replaces the generic wording, e.g. to name the owner). It is persisted, so keep it stable."""
    from .. import profile as _profile
    return str(_profile.lexicon("wellness.deep_memory_fact", DEEP_MEMORY_FACT))


def _ensure_tables() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS wellness_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audience TEXT NOT NULL,
                source TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'nudged',
                created_at TEXT NOT NULL,
                responded_at TEXT,
                response_summary TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_wellness_checkins_created ON wellness_checkins(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wellness_checkins_audience ON wellness_checkins(audience, created_at)")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS wellness_journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audience TEXT NOT NULL,
                source TEXT NOT NULL,
                entry_text TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                local_day TEXT NOT NULL,
                checkin_id INTEGER,
                private INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_wellness_journal_entries_created ON wellness_journal_entries(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wellness_journal_entries_audience ON wellness_journal_entries(audience, created_at)")


def _bool_state(key: str, default: bool = False) -> bool:
    v = get_app_state(_STATE_PREFIX + key)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _int_state(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(get_app_state(_STATE_PREFIX + key, str(default)) or default)))
    except Exception:
        return default


def is_enabled() -> bool:
    return _bool_state("enabled", False)


def schedule_time() -> tuple[int, int]:
    """Daily scheduler time, local-hour/minute. Defaults to a gentle evening prompt."""
    return _int_state("hour", 20, 0, 23), _int_state("minute", 30, 0, 59)


def status() -> dict[str, Any]:
    h, m = schedule_time()
    return {
        "ok": True,
        "enabled": is_enabled(),
        "owner": _bool_state("owner", True),
        "guests": _bool_state("guests", True),
        "hour": h,
        "minute": m,
        "last_prompted_date": get_app_state(_STATE_PREFIX + "last_prompted_date"),
        "owner_prompt": get_app_state(_STATE_PREFIX + "owner_prompt", DEFAULT_OWNER_PROMPT),
        "guest_prompt": get_app_state(_STATE_PREFIX + "guest_prompt", DEFAULT_GUEST_PROMPT),
    }


def configure_checkins(*, enabled: bool = True, owner: bool = True, guests: bool = True,
                       hour: int | None = None, minute: int | None = None,
                       owner_prompt: str | None = None, guest_prompt: str | None = None,
                       source: str = "chat") -> dict[str, Any]:
    """Turn on/off the wellness self-journal reminder lane and preserve the importance in memory."""
    set_app_state(_STATE_PREFIX + "enabled", "true" if enabled else "false")
    set_app_state(_STATE_PREFIX + "owner", "true" if owner else "false")
    set_app_state(_STATE_PREFIX + "guests", "true" if guests else "false")
    if hour is not None:
        set_app_state(_STATE_PREFIX + "hour", str(max(0, min(23, int(hour)))))
    if minute is not None:
        set_app_state(_STATE_PREFIX + "minute", str(max(0, min(59, int(minute)))))
    if owner_prompt and owner_prompt.strip():
        set_app_state(_STATE_PREFIX + "owner_prompt", owner_prompt.strip()[:600])
    if guest_prompt and guest_prompt.strip():
        set_app_state(_STATE_PREFIX + "guest_prompt", guest_prompt.strip()[:600])
    memory: dict[str, Any]
    try:
        from .memory import remember_fact
        memory = remember_fact(deep_memory_fact(), source="wellness_checkins", scope="owner")
    except Exception as exc:  # memory is supportive, not required for the scheduler lane
        memory = {"ok": False, "error": str(exc)[:160]}
    _ensure_tables()
    set_app_state(_STATE_PREFIX + "configured_at", utc_now_iso())
    out = status()
    out.update({"configured": True, "source": source, "memory_recorded": bool(memory.get("ok", False)), "memory": memory})
    return out


def _today_local(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc).astimezone()).date().isoformat()


def _record(audience: str, source: str, prompt: str, status_value: str = "nudged") -> int:
    _ensure_tables()
    with connect() as con:
        cur = con.execute(
            "INSERT INTO wellness_checkins(audience, source, prompt, status, created_at) VALUES (?,?,?,?,?)",
            (audience, source, prompt, status_value, utc_now_iso()),
        )
        return int(cur.lastrowid)


async def run_checkin_cycle(*, source: str = "scheduler", force: bool = False,
                            now: datetime | None = None) -> dict[str, Any]:
    """Send due private self-journal nudges to owner devices and trusted guests.

    Owner delivery uses account/profile-targeted push. Guest delivery uses the guest mailbox, not
    generic phone push, so an owner nudge cannot leak to guest PWAs and guest journal content stays private.
    """
    cfg = status()
    if not cfg["enabled"] and not force:
        return {"ok": True, "sent": False, "reason": "wellness check-ins disabled", "configured": cfg}
    today = _today_local(now)
    if not force and get_app_state(_STATE_PREFIX + "last_prompted_date") == today:
        return {"ok": True, "sent": False, "reason": "already prompted today", "date": today}

    owner_sent = False
    guest_sent = False
    rows: list[int] = []
    errors: list[str] = []

    if cfg.get("owner", True):
        prompt = str(cfg.get("owner_prompt") or DEFAULT_OWNER_PROMPT)
        try:
            from .notifications import send_to_phone, store_notification, NotificationIn
            pushed = await send_to_phone(
                "Mindful check-in",
                prompt,
                priority="default",
                tags="seedling",
                target_user_id="owner",
                target_device_profiles=("primary_phone", "home_tablet"),
            )
            owner_sent = bool(pushed.get("sent"))
            store_notification(NotificationIn(
                source="wellness-checkin",
                app="mongoose",
                title="Mindful check-in",
                body=prompt,
                privacy="private",
                urgency="normal",
                device="owner",
            ))
            rows.append(_record("owner", source, prompt, "nudged" if owner_sent else "stored_only"))
        except Exception as exc:
            errors.append(f"owner: {str(exc)[:160]}")
            rows.append(_record("owner", source, prompt, "error"))

    if cfg.get("guests", True):
        prompt = str(cfg.get("guest_prompt") or DEFAULT_GUEST_PROMPT)
        try:
            from .mailbox import OWNER, send_message
            msg = send_message(sender=OWNER, recipient="guest:*", body=prompt, notify=True)
            guest_sent = bool(msg.get("ok"))
            if guest_sent:
                for rcpt in msg.get("delivered_to", []) or []:
                    rows.append(_record(str(rcpt), source, prompt, "nudged"))
            else:
                errors.append("guests: " + str(msg.get("error", "not delivered"))[:140])
        except Exception as exc:
            errors.append(f"guests: {str(exc)[:160]}")

    if owner_sent or guest_sent or rows:
        set_app_state(_STATE_PREFIX + "last_prompted_date", today)
    return {
        "ok": not errors or bool(rows),
        "sent": bool(owner_sent or guest_sent),
        "owner_sent": owner_sent,
        "guest_sent": guest_sent,
        "record_ids": rows,
        "errors": errors,
        "date": today,
        "source": source,
    }


def surpriser_candidate() -> dict[str, Any] | None:
    """A low-key owner-only Surpriser candidate, separate from the scheduler's guest broadcast."""
    cfg = status()
    if not cfg.get("enabled") or not cfg.get("owner", True):
        return None
    today = _today_local()
    if get_app_state(_STATE_PREFIX + "last_surpriser_date") == today:
        return None
    if get_app_state(_STATE_PREFIX + "last_prompted_date") == today:
        return None
    prompt = str(cfg.get("owner_prompt") or DEFAULT_OWNER_PROMPT)
    return {
        "kind": "wellness_checkin",
        "text": prompt,
        "why": "structured mindfulness/self-journal check-ins are enabled and no nudge has happened today",
        "score": 0.57,
        "family": "wellness_checkin",
        "imminent": False,
        "signature": "wellness_checkin:" + today,
    }


def mark_surpriser_prompted() -> None:
    today = _today_local()
    set_app_state(_STATE_PREFIX + "last_surpriser_date", today)
    set_app_state(_STATE_PREFIX + "last_prompted_date", today)
    _record("owner", "surpriser", str(status().get("owner_prompt") or DEFAULT_OWNER_PROMPT), "nudged")


def log_checkin_response(*, audience: str = "owner", text: str = "", source: str = "chat") -> dict[str, Any]:
    """Persist a private, timestamped self-journal/check-in entry.

    Earlier versions only kept a short response_summary on the latest nudge. That made the
    UX feel like the journal had been accepted while the actual entry could be lost or
    overwritten by the next check-in. This writes every entry to wellness_journal_entries
    with its own UTC timestamp and local day, then marks the latest matching nudge complete.
    """
    _ensure_tables()
    now = utc_now_iso()
    local_day = _today_local()
    entry = " ".join((text or "").replace("\x00", " ").split()).strip() or "completed"
    summary = entry[:500]
    with connect() as con:
        row = con.execute(
            "SELECT id FROM wellness_checkins WHERE audience=? ORDER BY id DESC LIMIT 1",
            (audience,),
        ).fetchone()
        checkin_id = int(row["id"]) if row else None
        if not row:
            cur = con.execute(
                "INSERT INTO wellness_checkins(audience, source, prompt, status, created_at, responded_at, response_summary) "
                "VALUES (?,?,?,?,?,?,?)",
                (audience, source, "self-reported check-in", "completed", now, now, summary),
            )
            checkin_id = int(cur.lastrowid)
            created = True
        else:
            con.execute(
                "UPDATE wellness_checkins SET status='completed', responded_at=?, response_summary=? WHERE id=?",
                (now, summary, checkin_id),
            )
            created = False
        jcur = con.execute(
            "INSERT INTO wellness_journal_entries(audience, source, entry_text, summary, created_at, local_day, checkin_id, private) "
            "VALUES (?,?,?,?,?,?,?,1)",
            (audience, source, entry[:8000], summary, now, local_day, checkin_id),
        )
        journal_id = int(jcur.lastrowid)
    return {
        "ok": True,
        "created": created,
        "id": checkin_id,
        "journal_entry_id": journal_id,
        "created_at": now,
        "local_day": local_day,
        "stored_persistently": True,
    }


def recent(limit: int = 20) -> list[dict[str, Any]]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT id, audience, source, status, created_at, responded_at FROM wellness_checkins "
            "ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_journal_entries(limit: int = 20, *, audience: str | None = None, include_text: bool = False) -> list[dict[str, Any]]:
    """Recent private journal entries. Text is opt-in so status views stay discreet."""
    _ensure_tables()
    lim = max(1, min(int(limit), 100))
    cols = "id, audience, source, summary, created_at, local_day, checkin_id" + (", entry_text" if include_text else "")
    with connect() as con:
        if audience:
            rows = con.execute(
                f"SELECT {cols} FROM wellness_journal_entries WHERE audience=? ORDER BY id DESC LIMIT ?",
                (audience, lim),
            ).fetchall()
        else:
            rows = con.execute(
                f"SELECT {cols} FROM wellness_journal_entries ORDER BY id DESC LIMIT ?",
                (lim,),
            ).fetchall()
    return [dict(r) for r in rows]
