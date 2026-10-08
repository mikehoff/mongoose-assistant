from __future__ import annotations

import json
import re
from datetime import datetime, time
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..db import audit, connect, utc_now_iso
from ..settings import get_settings


def _local_dedupe_id(*parts: str | None) -> str:
    """Deterministic id for an item that has no upstream source id, so the
    UNIQUE(source, source_*_id) upsert can deduplicate it. SQLite allows multiple
    NULLs in a unique index, so without this, manual/local re-ingests duplicate."""
    import hashlib
    raw = "\x1f".join((p or "") for p in parts)
    return "local:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class EventIn(BaseModel):
    source: str = "manual"
    source_event_id: str | None = None
    calendar_name: str | None = None
    title: str
    start_at: str
    end_at: str
    busy_status: str = "busy"
    sensitivity: str = "normal"
    detail_available: bool = True
    raw: dict[str, Any] = Field(default_factory=dict)


class TaskIn(BaseModel):
    source: str = "manual"
    source_task_id: str | None = None
    list_name: str | None = None
    title: str
    due_at: str | None = None
    reminder_at: str | None = None
    priority: str | None = None
    status: str = "open"
    raw: dict[str, Any] = Field(default_factory=dict)


def add_event(event: EventIn) -> dict[str, Any]:
    event_id = event.source_event_id or _local_dedupe_id(
        event.source, event.calendar_name, event.title, event.start_at, event.end_at)
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO events(source, source_event_id, calendar_name, title, start_at, end_at, busy_status, sensitivity, detail_available, raw_json, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_event_id) DO UPDATE SET
                calendar_name=excluded.calendar_name, title=excluded.title,
                start_at=excluded.start_at, end_at=excluded.end_at,
                busy_status=excluded.busy_status, sensitivity=excluded.sensitivity,
                detail_available=excluded.detail_available, raw_json=excluded.raw_json,
                last_seen=excluded.last_seen
            """,
            (
                event.source,
                event_id,
                event.calendar_name,
                event.title,
                event.start_at,
                event.end_at,
                event.busy_status,
                event.sensitivity,
                1 if event.detail_available else 0,
                json.dumps(event.raw),
                utc_now_iso(),
            ),
        )
    audit("calendar", "add_event", True, privacy=event.sensitivity, payload={"title": event.title})
    return {"id": cur.lastrowid, "ok": True}


def add_task(task: TaskIn) -> dict[str, Any]:
    task_id = task.source_task_id or _local_dedupe_id(
        task.source, task.list_name, task.title, task.due_at)
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO tasks(source, source_task_id, list_name, title, due_at, reminder_at, priority, status, raw_json, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_task_id) DO UPDATE SET
                list_name=excluded.list_name, title=excluded.title,
                due_at=excluded.due_at, reminder_at=excluded.reminder_at,
                priority=excluded.priority, status=excluded.status,
                raw_json=excluded.raw_json, last_seen=excluded.last_seen
            """,
            (
                task.source,
                task_id,
                task.list_name,
                task.title,
                task.due_at,
                task.reminder_at,
                task.priority,
                task.status,
                json.dumps(task.raw),
                utc_now_iso(),
            ),
        )
    audit("tasks", "add_task", True, payload={"title": task.title})
    return {"id": cur.lastrowid, "ok": True}


def most_recent_open_task(*, without_reminder: bool = False) -> dict[str, Any] | None:
    """The most recently touched OPEN task, so a follow-up 'nudge me next week' attaches to the task the
    owner just created rather than an orphan alarm. without_reminder skips ones that already have a time."""
    import sqlite3
    where = "status='open'" + (" AND reminder_at IS NULL AND due_at IS NULL" if without_reminder else "")
    with connect() as con:
        con.row_factory = sqlite3.Row
        r = con.execute(
            f"SELECT * FROM tasks WHERE {where} ORDER BY last_seen DESC, id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def set_task_reminder(task_id: int, reminder_at: str) -> dict[str, Any]:
    """Really set a task's reminder_at (the thing the confabulated 'I'll nudge you' never did)."""
    with connect() as con:
        con.execute("UPDATE tasks SET reminder_at=?, last_seen=? WHERE id=?",
                    (reminder_at, utc_now_iso(), int(task_id)))
    audit("tasks", "set_reminder", True, payload={"id": int(task_id), "reminder_at": reminder_at})
    return {"ok": True, "id": int(task_id), "reminder_at": reminder_at}


_ALLOWED_PRIORITY = ("high", "normal", "low")


def update_task(task_id: int, *, due_at: str | None = None, priority: str | None = None,
                status: str | None = None, is_rtm: bool | None = None,
                reminder_at: str | None = None, clear: tuple[str, ...] = ()) -> dict[str, Any]:
    """Owner edit of one task: deadline, priority, status, RTM flag, reminder.

    Only the fields you pass are touched; `clear` explicitly nulls fields (passing None means "leave
    alone", so there would otherwise be no way to REMOVE a due date). Writes are LOCAL only: the
    Microsoft To Do connector holds Tasks.Read and has no write path, so nothing edited here can
    travel back to Microsoft , which is exactly the boundary the owner asked for.
    """
    sets: list[str] = []
    vals: list[Any] = []
    if due_at is not None:
        sets.append("due_at=?"); vals.append(due_at)
    if reminder_at is not None:
        sets.append("reminder_at=?"); vals.append(reminder_at)
    if priority is not None:
        p = str(priority).strip().lower()
        if p not in _ALLOWED_PRIORITY:
            return {"ok": False, "error": f"priority must be one of {', '.join(_ALLOWED_PRIORITY)}"}
        sets.append("priority=?"); vals.append(p)
    if status is not None:
        s = str(status).strip().lower()
        if s not in ("open", "completed", "notstarted", "deleted"):
            return {"ok": False, "error": "status must be open, completed or deleted"}
        sets.append("status=?"); vals.append(s)
    if is_rtm is not None:
        sets.append("is_rtm=?"); vals.append(1 if is_rtm else 0)
    for field in clear:
        if field in ("due_at", "reminder_at", "priority", "is_rtm"):
            sets.append(f"{field}=NULL")
    if not sets:
        return {"ok": False, "error": "nothing to update"}
    sets.append("last_seen=?"); vals.append(utc_now_iso())
    vals.append(int(task_id))
    with connect() as con:
        cur = con.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        if not cur.rowcount:
            return {"ok": False, "error": f"no task {task_id}"}
        row = con.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
    audit("tasks", "update", True, payload={"id": int(task_id)})
    return {"ok": True, "task": dict(row) if row else {"id": int(task_id)}}


def _soon(today: str, days: int = 7) -> str:
    """The far edge of "due soon", as an ISO date."""
    from datetime import timedelta
    return (datetime.fromisoformat(today) + timedelta(days=days)).strftime("%Y-%m-%d")


def rtm_verdict(row: dict) -> tuple[bool, str]:
    """Is this task RTM work, and on whose authority? Returns (is_rtm, basis).

    THE single answer to that question. Chat used to carry its own private regex, which disagreed with
    this module on the owner's real list: its `\\bleaseholder\\b` could not match the plural in "remind
    leaseholders to inspect their balconies", so chat reported 3 RTM tasks where the truth was 12.
    Two classifiers means two answers, and the user gets whichever surface he happened to ask.

    Precedence is the point. The owner's checkbox ('owner') is final and never second-guessed. Failing
    that a task filed on the RTM list ('list') is taken at its word. Only then does the guess apply, and
    it is reported as 'guess' so a surface can show it as a suggestion rather than a decided fact.
    """
    stored = row.get("is_rtm")
    if stored is not None:
        return bool(stored), "owner"
    if (row.get("list_name") or "").strip().upper() == "RTM":
        return True, "list"
    return guess_is_rtm(row.get("title") or ""), "guess"


def guess_is_rtm(title: str) -> bool:
    """ADVISORY only: does this look like RTM company work? It seeds the checkbox the owner can flip;
    it never decides on its own, because a wrong silent classification is worse than an unset one.

    The vocabulary is deliberately grounded in what actually appears on the real list, not a guess at
    the domain. The first pass caught 1 of 77 because it only matched literal words like "leaseholder":
    it missed the buildings written out in full ("Flat 1, 1 Example Road" , the flat-code pattern "1ec"
    does not match "1 Example Road"), the company's statutory filings ("confirmation statement"), and
    building compliance shorthand ("FRA" = fire risk assessment).

    The list below is DOMAIN vocabulary: it is true of any RTM company, so it belongs in code. Names of
    this particular company's counterparties (the block insurer, its managing agent, its
    contractors) are FACTS ABOUT ONE CLIENT, so they live in property.json under rtm_keywords, next to
    the company identity that was externalised for the same reason. Hardcoding a real supplier here
    would put it straight back into the source tree being sterilised for a public repo, and would mean
    a code change every time the company changes insurer.
    """
    t = f" {(title or '').lower()} "
    hits = (
        # the company and its people (its own name comes from the site profile, see _site_rtm_terms)
        "rtm", "leaseholder", "director",
        # money owed and charged
        "service charge", "demand", "arrears", "reserve fund", "sinking fund", "apportion",
        # statutory / legal
        "confirmation statement", "companies house", "tribunal", "freehold", "section 20", "s20",
        # building compliance and fabric
        "fire risk", "fire door", "eicr", "asbestos", "communal", "balcon", "lift ",
        # insuring the building is RTM work whoever the insurer happens to be
        "block insurance", "buildings insurance", "block policy", "insurance renewal",
    )
    if any(h in t for h in hits):
        return True
    if any(h in t for h in _site_rtm_terms()):
        return True
    if any(k in t for k in _configured_rtm_keywords()):
        return True
    # Short acronyms need word boundaries so they cannot fire inside an unrelated word.
    return any(f" {a} " in t for a in ("fra", "ews1", "h&s"))


def _site_rtm_terms() -> tuple[str, ...]:
    """This site's own name and buildings, however they are written, from the site profile: the RTM
    company name (spaced and run together), each block code ('1ec'), and the street words. The street
    words default to each block alias minus its number ('example road'); a profile can name a shorter,
    more distinctive form via the lexicon "rtm.street_keywords". Never fatal.

    Cached per profile selection in 2-second buckets: this runs once per task, twice per Tasks screen,
    and a profile accessor re-resolves the profile through get_settings() (a .env re-read) every call."""
    import os
    import time as _time  # the module-level `time` is datetime.time
    return _site_rtm_terms_for((os.environ.get("MONGOOSE_PROFILE_FILE"), os.environ.get("MONGOOSE_DB_PATH"),
                                os.environ.get("MONGOOSE_DATA_DIR"), int(_time.monotonic() // 2)))


@lru_cache(maxsize=8)
def _site_rtm_terms_for(_key: tuple[Any, ...]) -> tuple[str, ...]:
    try:
        from .. import profile as _profile
        name = _profile.rtm_name().lower()
        out = [name, name.replace(" ", "")]
        out += [c.lower() for c in _profile.block_codes()]
        streets = _profile.lexicon("rtm.street_keywords", None)
        if not streets:
            streets = []
            for b in _profile.blocks():
                for a in b["aliases"]:
                    w = re.sub(r"^\s*[0-9]+\s*", "", a).strip().lower()
                    if " " in w:   # skip run-together forms like 'EXAMPLEROAD'
                        streets.append(w)
        out += [str(w).lower() for w in streets]
        return tuple(dict.fromkeys(w for w in out if len(w) > 1))
    except Exception:
        return ()


_KEYWORD_CACHE: dict[str, Any] = {"mtime": None, "value": ()}


def _configured_rtm_keywords() -> tuple[str, ...]:
    """This company's own counterparties, from property.json. Never fatal: a missing or malformed
    config degrades to the built-in domain vocabulary rather than breaking the task list.

    CACHED ON FILE MTIME. This is called once per task, and the task screen resolves every task twice
    (once to list, once to count), so an uncached read meant about 130 file opens and JSON parses per
    request. That alone made the Tasks screen slow enough to hit the phone's 15 second timeout, which
    presented as "Could not load tasks" the moment anything else was competing for the machine.
    """
    try:
        from .rtm_example_court import _property_file
        from pathlib import Path
        path = Path(str(_property_file()))
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except Exception:
        mtime = 0.0
    if _KEYWORD_CACHE["mtime"] == mtime:
        return _KEYWORD_CACHE["value"]
    try:
        from .rtm_example_court import load_property
        raw = load_property().get("rtm_keywords") or []
    except Exception:
        _KEYWORD_CACHE.update({"mtime": mtime, "value": ()})
        return ()
    # Blank or one-character entries would match nearly every title, so they are dropped.
    out = tuple(k.strip().lower() for k in raw if isinstance(k, str) and len(k.strip()) > 1)
    _KEYWORD_CACHE.update({"mtime": mtime, "value": out})
    return out


def _to_utc(ts: str):
    """Parse an ISO timestamp to an aware UTC datetime. Returns None if unparseable."""
    from datetime import datetime, timezone
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def availability(start_at: str, end_at: str) -> dict[str, Any]:
    # Overlap must be computed on normalized UTC instants, not raw ISO strings:
    # equivalent instants with different offsets (e.g. 11:30+01:00 vs 10:30Z)
    # do NOT string-compare correctly. We pull busy events and compare in Python.
    q_start, q_end = _to_utc(start_at), _to_utc(end_at)
    if q_start and q_end and q_end <= q_start:
        return {"busy": [], "is_free": False, "error": "end_at must be after start_at"}
    with connect() as con:
        rows = con.execute(
            """
            SELECT * FROM events
            WHERE busy_status IN ('busy', 'tentative')
            ORDER BY start_at ASC
            """
        ).fetchall()
    conflicts = []
    if q_start and q_end:
        for row in rows:
            e_start, e_end = _to_utc(row["start_at"]), _to_utc(row["end_at"])
            if e_start and e_end and e_start < q_end and e_end > q_start:
                conflicts.append(row)
        busy = []
        for row in conflicts:
            data = dict(row)
            if not data.get("detail_available"):
                data["title"] = "Busy"
            busy.append(data)
        return {"busy": busy, "is_free": len(busy) == 0}
    # Invalid/missing time window: be explicit rather than returning None.
    return {"busy": [], "is_free": False, "error": "invalid_time_window"}


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:
        return ZoneInfo("UTC")


def _parse_local(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    tz = _local_tz()
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def _day_bounds(day: str | None = None) -> tuple[datetime, datetime]:
    tz = _local_tz()
    target = datetime.fromisoformat(day).date() if day else datetime.now(tz).date()
    return datetime.combine(target, time.min, tzinfo=tz), datetime.combine(target, time.max, tzinfo=tz)


def _hide_private_event(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("detail_available"):
        data = dict(data)
        data["title"] = "Busy"
    return data


def events_today() -> list[dict[str, Any]]:
    start, end = _day_bounds()
    with connect() as con:
        rows = con.execute("SELECT * FROM events ORDER BY start_at ASC").fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        s = _parse_local(data.get("start_at"))
        e = _parse_local(data.get("end_at"))
        if s is None or e is None:
            continue
        if s <= end and e >= start:
            results.append(_hide_private_event(data))
    return results


def tasks_today(*, include_open_without_due: bool = False) -> list[dict[str, Any]]:
    start, end = _day_bounds()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM tasks WHERE status NOT IN ('completed', 'deleted') "
            "ORDER BY (CASE WHEN COALESCE(reminder_at, due_at) IS NULL THEN 1 ELSE 0 END) ASC, "
            "COALESCE(reminder_at, due_at) ASC, title ASC"
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        when = _parse_local(data.get("reminder_at")) or _parse_local(data.get("due_at"))
        if when is None:
            if include_open_without_due:
                results.append(data)
            continue
        # Overdue tasks (due before today) still belong on today's list , they roll forward until done
        # and are the most urgent thing to surface. Flag them so callers can mark them.
        if when < start:
            data["overdue"] = True
            results.append(data)
        elif start <= when <= end:
            results.append(data)
    return results


def upcoming_events(*, days: int = 14, limit: int = 50) -> list[dict[str, Any]]:
    """Upcoming events from the local synced calendar store. Read-only chat/tool helper."""
    from datetime import timedelta
    tz = _local_tz()
    now = datetime.now(tz)
    horizon = now + timedelta(days=max(1, min(int(days or 14), 90)))
    with connect() as con:
        rows = con.execute("SELECT * FROM events ORDER BY start_at ASC LIMIT 1000").fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = _hide_private_event(dict(row))
        start = _parse_local(data.get("start_at"))
        end = _parse_local(data.get("end_at"))
        if start is None or end is None:
            continue
        if end >= now and start <= horizon:
            out.append(data)
        if len(out) >= max(1, min(int(limit or 50), 200)):
            break
    return out



def reconcile_todo_tasks(seen_task_ids: set[str], seen_list_names: set[str],
                         *, source: str = "microsoft_todo") -> dict[str, Any]:
    """After a pull-sync, resolve open tasks that upstream no longer returns.

    A pull-sync only ever ADDS/updates tasks it sees. Without reconciliation, a task you complete or
    delete in the To Do app keeps its stale `status='open'` locally forever (a ghost). This marks any
    open task in `source` that was NOT seen this run as 'deleted' (no longer active upstream).

    SAFETY, this only reaps within lists that synced SUCCESSFULLY (`seen_list_names`). A list that
    failed or was skipped this run is excluded, so a transient Graph error can never silently wipe real
    tasks. If we cannot identify any successfully-synced list, we reap nothing. A task whose list_name is
    not among the seen lists is also left untouched (we did not get an authoritative view of its list)."""
    if not seen_list_names:
        return {"resolved": 0, "skipped": "no successfully-synced lists; nothing reaped"}
    placeholders = ",".join("?" for _ in seen_list_names)
    resolved = 0
    with connect() as con:
        rows = con.execute(
            f"SELECT id, source_task_id, list_name FROM tasks "
            f"WHERE source=? AND status='open' "
            f"AND COALESCE(NULLIF(list_name,''),'Inbox') IN ({placeholders})",
            (source, *[(n or "Inbox") for n in seen_list_names])).fetchall()
        for r in rows:
            d = dict(r)
            if d.get("source_task_id") not in seen_task_ids:
                con.execute("UPDATE tasks SET status='deleted', last_seen=? WHERE id=?",
                            (utc_now_iso(), d["id"]))
                resolved += 1
    if resolved:
        audit("tasks", "reconcile_resolved", True, payload={"resolved": resolved, "source": source})
    return {"resolved": resolved}


def task_lists() -> list[dict[str, Any]]:
    """Task list/folder counts from the local synced To Do store."""
    with connect() as con:
        rows = con.execute(
            """
            SELECT COALESCE(NULLIF(list_name,''), 'Inbox') AS name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status NOT IN ('completed','deleted') THEN 1 ELSE 0 END) AS open
            FROM tasks
            GROUP BY COALESCE(NULLIF(list_name,''), 'Inbox')
            ORDER BY open DESC, name ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]



def list_tasks(*, limit: int = 50, include_completed: bool = False,
               scope: str = "all") -> list[dict[str, Any]]:
    """Tasks from the local store, newest deadline first, unscheduled last.

    scope: 'all' | 'rtm' (company work) | 'personal' (explicitly NOT rtm) | 'unclassified'
    (is_rtm never set , shown as its own bucket so unclassified work is visible rather than silently
    lumped in with personal). Ordering puts anything with a date first, then high priority.
    """
    lim = max(1, min(int(limit or 50), 500))
    clauses = [] if include_completed else ["status NOT IN ('completed', 'deleted')"]
    s = (scope or "all").strip().lower()
    # 'rtm' is decided by rtm_verdict() in Python because the guess is not expressible in SQL. Filtering
    # on is_rtm=1 returned an EMPTY rtm scope for the owner's real list: every one of his 72 tasks had
    # is_rtm NULL, so the one scope he most wanted showed nothing at all.
    # 'personal' and 'unclassified' stay SQL and stay exclusive, so each matches its chip count.
    if s == "unclassified":
        clauses.append("is_rtm IS NULL")
    elif s == "personal":
        clauses.append("is_rtm=0")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # Verdict filtering happens after the query, so fetch enough rows to still fill the limit.
    fetch = min(500, lim * 8) if s == "rtm" else lim
    with connect() as con:
        rows = con.execute(
            f"""
            SELECT * FROM tasks
            {where}
            ORDER BY (CASE WHEN COALESCE(reminder_at, due_at) IS NULL THEN 1 ELSE 0 END) ASC,
                     COALESCE(reminder_at, due_at) ASC,
                     (CASE LOWER(COALESCE(priority,'')) WHEN 'high' THEN 0 WHEN 'normal' THEN 1
                           WHEN 'low' THEN 2 ELSE 3 END) ASC,
                     title ASC
            LIMIT ?
            """,
            (fetch,),
        ).fetchall()
    def _rank(t: dict[str, Any]) -> tuple:
        """Order by what needs attention, not merely by whether someone typed a date.

        SQL could only sort on the columns: anything undated fell to the bottom and was then alphabetical,
        so a fire risk assessment and an insurance claim sat below dozens of one-word scraps, and adding a
        date was the only lever that moved anything. Rank is therefore computed here, after the RTM verdict
        is known, in the order a person would triage: overdue, due soon, dated later, then undated with
        company work above personal, and high priority lifting within each band.
        """
        when = str(t.get("due_at") or t.get("reminder_at") or "")
        today = datetime.now().strftime("%Y-%m-%d")
        if when:
            band = 0 if when[:10] < today else (1 if when[:10] <= _soon(today) else 2)
        else:
            band = 3 if t.get("is_rtm_effective") else 4
        pri = {"high": 0, "normal": 1, "low": 2}.get(str(t.get("priority") or "").lower(), 1)
        return (band, pri, when or "9999", str(t.get("title") or "").lower())

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        eff, basis = rtm_verdict(d)
        d["is_rtm_effective"] = eff
        d["rtm_basis"] = basis          # 'owner' | 'list' | 'guess', so a surface can show a suggestion
        if s == "rtm" and not eff:
            continue
        out.append(d)
        if len(out) >= lim:
            break
    out.sort(key=_rank)
    return out


def task_counts(rows: "list[dict[str, Any]] | None" = None) -> dict[str, int]:
    """How the open list splits, so the UI can show the RTM/personal/unclassified breakdown.

    total/rtm/personal/unclassified stay MUTUALLY EXCLUSIVE and reflect only what the owner has decided,
    so an untouched task reads as unclassified and is never quietly filed as personal. A first attempt
    defined personal as "total minus rtm", which buried all 72 undecided tasks in the personal bucket.

    'rtm_effective' is the separate, overlapping answer to "what is RTM work today" (decided, else
    guessed). It is what chat answers with and what the RTM chip counts, because a guess the owner has
    not yet confirmed is still the most useful thing to show him.
    """
    # Reuse the caller's rows when it already has them: the screen asked for the list and then asked for
    # counts, which resolved all 72 tasks a second time for no new information.
    rows = rows if rows is not None else list_tasks(limit=500)
    rtm_effective = sum(1 for t in rows if t.get("is_rtm_effective"))
    with connect() as con:
        row = con.execute(
            """SELECT COUNT(*) total,
                      COALESCE(SUM(CASE WHEN is_rtm=1 THEN 1 ELSE 0 END),0) rtm,
                      COALESCE(SUM(CASE WHEN is_rtm=0 THEN 1 ELSE 0 END),0) personal,
                      COALESCE(SUM(CASE WHEN is_rtm IS NULL THEN 1 ELSE 0 END),0) unclassified,
                      COALESCE(SUM(CASE WHEN COALESCE(due_at,reminder_at) IS NULL THEN 1 ELSE 0 END),0) undated
               FROM tasks WHERE status NOT IN ('completed','deleted')""").fetchone()
    out = {k: int(row[k] or 0) for k in ("total", "rtm", "personal", "unclassified", "undated")}
    out["rtm_effective"] = rtm_effective
    return out

