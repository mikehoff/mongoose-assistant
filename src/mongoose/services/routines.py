"""Named routines: reusable multi-step agentic sequences for the local Gemma tool loop.

A routine is a saved natural-language instruction (a "recipe") that, when triggered, is fed to the
SAME run_tool_turn loop a normal chat message goes through. That is the whole safety argument: a
routine is not a privileged new execution path: it is just a stored prompt. Every guard the loop
already enforces (the prompt-injection block on acting-after-read, sensitive-action confirmation,
panel-safety entity protection, per-call dedupe, the hard step ceiling) applies unchanged. A routine
cannot do anything a typed message couldn't.

Why this earns its keep: the loop can now chain deeper (MONGOOSE_MAX_TOOL_STEPS, default 4, up to
12), so a multi-step household routine: "wind down for the night: what's my first meeting tomorrow,
set a wake alarm 45 min before, warm the yoga, and let Sam know I'm turning in": can actually run
end to end instead of being re-improvised each time. Routines give that headroom a name and a trigger.

Storage is a simple table; matching is by exact name or a trigger phrase. Routines never bypass
confirmation: if a routine's recipe would invoke a sensitive tool, the loop still returns a proposal
and stops, exactly as it would for a typed request.
"""
from __future__ import annotations

from typing import Any

from ..db import audit, connect, utc_now_iso


def ensure_tables() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS routines (
                name TEXT PRIMARY KEY,
                recipe TEXT NOT NULL,
                trigger_phrase TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_run_at TEXT,
                run_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )


def _normalise(name: str) -> str:
    return (name or "").strip().lower()


def save_routine(name: str, recipe: str, *, trigger_phrase: str | None = None) -> dict[str, Any]:
    """Create or update a routine. `recipe` is the natural-language instruction the loop will run."""
    name = _normalise(name)
    recipe = (recipe or "").strip()
    if not name:
        return {"ok": False, "error": "routine needs a name"}
    if not recipe:
        return {"ok": False, "error": "routine needs a recipe (what it should do)"}
    if len(recipe) > 2000:
        return {"ok": False, "error": "recipe too long (max 2000 chars)"}
    ensure_tables()
    now = utc_now_iso()
    trig = (trigger_phrase or "").strip().lower() or None
    with connect() as con:
        existing = con.execute(
            "SELECT name, trigger_phrase FROM routines WHERE name=?", (name,)
        ).fetchone()
        if existing:
            # Preserve the existing trigger phrase if the caller didn't supply a new one: updating a
            # routine's recipe shouldn't silently drop how it's triggered.
            if trig is None:
                trig = existing["trigger_phrase"]
            con.execute(
                "UPDATE routines SET recipe=?, trigger_phrase=?, updated_at=? WHERE name=?",
                (recipe, trig, now, name),
            )
            action = "updated"
        else:
            con.execute(
                "INSERT INTO routines(name, recipe, trigger_phrase, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, recipe, trig, now, now),
            )
            action = "created"
    audit("routines", action, True, payload={"name": name})
    return {"ok": True, "action": action, "name": name}


def delete_routine(name: str) -> dict[str, Any]:
    name = _normalise(name)
    ensure_tables()
    with connect() as con:
        cur = con.execute("DELETE FROM routines WHERE name=?", (name,))
        deleted = cur.rowcount
    return {"ok": deleted > 0, "deleted": deleted, "name": name}


def get_routine(name: str) -> dict[str, Any] | None:
    name = _normalise(name)
    ensure_tables()
    with connect() as con:
        row = con.execute(
            "SELECT name, recipe, trigger_phrase, created_at, updated_at, last_run_at, run_count "
            "FROM routines WHERE name=?", (name,)
        ).fetchone()
    return dict(row) if row else None


def list_routines() -> list[dict[str, Any]]:
    ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT name, recipe, trigger_phrase, last_run_at, run_count FROM routines "
            "ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def match_routine(text: str) -> dict[str, Any] | None:
    """Resolve free text to a routine: exact name, 'run/start <name>', or a trigger phrase match.
    Returns the routine row or None. Deliberately conservative: a near-miss returns None rather
    than running the wrong sequence."""
    t = (text or "").strip().lower()
    if not t:
        return None
    ensure_tables()
    # Strip a leading verb so "run wind down" / "start morning" resolve by name.
    for verb in ("run routine ", "run ", "start routine ", "start ", "do my ", "do "):
        if t.startswith(verb):
            t_name = t[len(verb):].strip()
            r = get_routine(t_name)
            if r:
                return r
    # Exact name
    r = get_routine(t)
    if r:
        return r
    # Trigger phrase (exact, to avoid surprising fuzzy matches)
    with connect() as con:
        row = con.execute(
            "SELECT name, recipe, trigger_phrase FROM routines WHERE trigger_phrase IS NOT NULL "
            "AND trigger_phrase = ?", (t,)
        ).fetchone()
    return dict(row) if row else None


def _record_run(name: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE routines SET last_run_at=?, run_count=run_count+1 WHERE name=?",
            (utc_now_iso(), name),
        )


async def run_routine(name: str) -> dict[str, Any]:
    """Execute a routine by feeding its recipe to the standard tool loop. The loop applies every
    existing safety guard; this function adds nothing privileged. Returns the loop's result plus
    the routine name, or an error if the routine doesn't exist."""
    routine = get_routine(name)
    if not routine:
        return {"ok": False, "error": f"no routine called '{_normalise(name)}'"}
    from ..tools import run_tool_turn
    result = await run_tool_turn(routine["recipe"])
    _record_run(routine["name"])
    audit("routines", "run", True,
          payload={"name": routine["name"], "stop_reason": result.get("stop_reason"),
                   "steps": len(result.get("steps", []))})
    return {"ok": True, "routine": routine["name"], **result}
