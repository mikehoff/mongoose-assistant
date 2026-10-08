from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..db import connect, utc_now_iso, audit

# Ritual engine.
#
# Rituals are actionable micro-practices, not vibe quests: short, concrete, useful
# things the user already knows help (physio, proprioception, breathing, a one-line
# journal, a resilience nudge, a friend check-in). Each has a fixed structure so it's
# predictable, and Mongoose tracks completions so it can learn which ones actually
# get done and honour a cooldown (don't nag).
#
# The scripts are deliberately plain and embodied. They use simple, generic
# posture cues without being clinical.


@dataclass(frozen=True)
class Ritual:
    name: str
    trigger: str            # the phrase that invokes it
    duration_s: int
    script: str
    completion_marker: str  # what "done" looks like
    cooldown_minutes: int
    follow_up: str = ""


RITUALS: dict[str, Ritual] = {
    "physio": Ritual(
        "physio", "physio check", 120,
        "Two minutes. Shoulders loose, slow breath, let the body settle. "
        "Do the boring useful thing.",
        "you did the two minutes", 180,
        follow_up="Mark it done? It counts."),
    "proprioception": Ritual(
        "proprioception", "proprioception reset", 60,
        "Stand still. Feet. Jaw. Ribs. Pelvis. Notice any tension without "
        "correcting it yet. Just notice.",
        "you ran through the body scan", 90),
    "breathing": Ritual(
        "breathing", "breathing minute", 60,
        "One minute. In for four, out for six. Let the out-breath be longer. "
        "Shoulders down on each exhale.",
        "you breathed for the minute", 60),
    "journal": Ritual(
        "journal", "journal me", 120,
        "One sentence: what am I feeling? One sentence: what do I need? "
        "One sentence: what is the next kind action?",
        "you wrote the three sentences", 240,
        follow_up="Want me to save those three lines to your diary?"),
    "resilience": Ritual(
        "resilience", "resilience check-in", 60,
        "What's one thing that would make tomorrow 2% easier? "
        "Just one. Small counts.",
        "you named one thing", 360),
    "friend_checkin": Ritual(
        "friend_checkin", "who should I check in with", 30,
        "Someone you haven't messaged in a bit. Want to send something small? "
        "I can draft it; you decide whether it goes.",
        "you considered reaching out", 720,
        follow_up="Say 'draft a soft check-in to <name>' and I'll prepare it."),
}


def rituals() -> dict[str, Ritual]:
    """RITUALS with any script the site profile words for its own body (lexicon rituals.scripts,
    {ritual name: script}), so the cues can name the person's actual pattern."""
    from .. import profile as _profile
    scripts = _profile.lexicon("rituals.scripts", {}) or {}
    if not scripts:
        return RITUALS
    return {k: (replace(r, script=scripts[k]) if k in scripts else r) for k, r in RITUALS.items()}


def list_rituals() -> list[dict[str, Any]]:
    return [
        {"name": r.name, "trigger": r.trigger, "duration_s": r.duration_s,
         "script": r.script, "cooldown_minutes": r.cooldown_minutes}
        for r in rituals().values()
    ]


def match_ritual(text: str) -> Ritual | None:
    """Map a phrase to a ritual. Accepts the trigger or the bare name."""
    t = (text or "").lower().strip()
    for r in rituals().values():
        if r.trigger in t or r.name in t:
            return r
    return None


def _minutes_since_last(con, ritual: str) -> float | None:
    row = con.execute(
        "SELECT started_at FROM ritual_log WHERE ritual = ? ORDER BY started_at DESC LIMIT 1",
        (ritual,)).fetchone()
    if not row:
        return None
    from datetime import datetime, timezone
    try:
        last = datetime.fromisoformat(row["started_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 60.0
    except Exception:
        return None


def start_ritual(name: str) -> dict[str, Any]:
    """Begin a ritual: returns its script and a log id to complete against. Honours
    cooldown by flagging (not blocking) a too-soon repeat, so Mongoose can gently
    say 'you just did this' rather than nag."""
    ritual = rituals().get(name) or match_ritual(name)
    if not ritual:
        return {"ok": False, "error": "unknown ritual", "available": list(RITUALS.keys())}
    with connect() as con:
        since = _minutes_since_last(con, ritual.name)
        cur = con.execute(
            "INSERT INTO ritual_log(ritual, started_at, outcome) VALUES (?, ?, NULL)",
            (ritual.name, utc_now_iso()))
        log_id = cur.lastrowid
    audit("ritual", "start", True, payload={"ritual": ritual.name})
    out = {
        "ok": True, "ritual": ritual.name, "log_id": log_id,
        "duration_s": ritual.duration_s, "script": ritual.script,
        "completion_marker": ritual.completion_marker, "follow_up": ritual.follow_up,
    }
    if since is not None and since < ritual.cooldown_minutes:
        out["recently_done"] = f"you did this {int(since)} min ago (cooldown {ritual.cooldown_minutes} min)"
    return out


def complete_ritual(log_id: int, outcome: str = "completed") -> dict[str, Any]:
    """Mark a started ritual done/skipped/snoozed. Recording completions is how
    Mongoose learns which rituals actually get finished."""
    if outcome not in ("completed", "skipped", "snoozed"):
        outcome = "completed"
    with connect() as con:
        con.execute("UPDATE ritual_log SET completed_at = ?, outcome = ? WHERE id = ?",
                    (utc_now_iso(), outcome, log_id))
    audit("ritual", "complete", True, payload={"log_id": log_id, "outcome": outcome})
    return {"ok": True, "log_id": log_id, "outcome": outcome}


def ritual_stats(days: int = 7) -> dict[str, Any]:
    """Completion counts per ritual over a window, for the diary/what-changed and to
    learn what's actually working."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect() as con:
        rows = con.execute(
            "SELECT ritual, outcome, COUNT(*) c FROM ritual_log "
            "WHERE started_at >= ? GROUP BY ritual, outcome", (cutoff,)).fetchall()
    stats: dict[str, dict[str, int]] = {}
    for r in rows:
        stats.setdefault(r["ritual"], {})[r["outcome"] or "started"] = r["c"]
    return {"days": days, "by_ritual": stats}
