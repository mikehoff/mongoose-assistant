"""Guest light tasks, the bounded, low-compute things a trusted guest can do, fully isolated from
Alex's heavy tools and scheduler.

The trusted_guest role has all owner tools off by design (email, calendar, home control, files, the real
scheduler). This module adds a SEPARATE, self-contained set of light capabilities a guest can use:
a low-stakes coach / counsellor / research assistant / day helper / ADHD coach. The hard rules:

  - Everything is keyed to the GUEST's own scope ('guest:<device_id>'). A guest reminder/timer/note
    lives in the guest's own namespace and is NEVER written to Alex's tasks or Alex's scheduler.
  - These are LOW-COMPUTE: short model turns or pure-Python helpers. No long agentic chains, no tool
    fan-out, nothing that competes with Alex's heavy workloads.
  - No capability here can read or write owner data. There is no parameter that takes an owner scope.

What a guest gets:
  - reflect(): a brief counselling-style reflection / reframe (supportive, not clinical).
  - focus_step(): an ADHD-style 'break it down + one next action + short timer' nudge.
  - day_plan(): a simple ordered plan for the guest's stated tasks (their list, their scope).
  - quick_research(): a short, self-contained answer to a light question (model turn, no tools).
  - guest reminders/notes: stored in a guest-scoped table, surfaced only to that guest.

Mongoose proactively tells guests these exist via guest_surpriser.guest_can_do() / capabilities_blurb().
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

# SAFETY (external audit, additional findings #5): reflect()'s system prompt ASKED the model
# to redirect on crisis wording, but nothing checked for it deterministically. When the model
# is down, the fallback ignored the message entirely and handed explicit self-harm text an
# open-ended introspective question instead of a clear safety redirect. This must never depend
# on the model noticing: checked BEFORE any model call, EN+PL (the owner and guests may use
# Polish), deliberately narrow and high-precision (explicit self-harm/immediate-danger wording
# only) so it doesn't fire on ordinary venting about a hard day.
_CRISIS_RE = re.compile(
    r"\b("
    r"suicid\w*|kill\s+myself|end\s+my\s+life|want\s+to\s+die|(?:hurt|harm)\s+myself|"
    r"self[-\s]?harm\w*|not\s+safe\s+right\s+now|in\s+immediate\s+danger|feel\w*\s+unsafe|"
    r"panic\s+attack|"
    r"samobój\w*|samookalecz\w*|zabi[jćc]\w*\s+si[eę]|skrzywdz\w*\s+si[eę]|"
    r"chc[eę]\s+umrze[cć]|nie\s+chc[eę]\s+ży[cć]"
    r")\b",
    re.IGNORECASE,
)
_CRISIS_RESPONSE = (
    "I hear you, and I want you to be safe right now. Please reach out to someone who can "
    "help immediately: call 999 if you're in danger right now, or Samaritans on 116 123 "
    "(free, 24/7, any time) to talk to someone. If there's a person nearby you trust, please "
    "tell them too. I'm not the right kind of help for this moment, but real help is close by."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path():
    from ..settings import get_settings
    from pathlib import Path
    s = get_settings()
    return str(getattr(s, "db_path", None) or (Path(getattr(s, "data_dir", "data")) / "mongoose.sqlite3"))


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path())
    con.row_factory = sqlite3.Row
    return con


def _require_guest(scope: str) -> None:
    """Hard guard: every guest-task entry point must be called with a guest scope. If something ever
    tries to call these with the owner scope, refuse, this is the boundary that stops guest features
    from being pointed at Alex's data."""
    if not (scope or "").startswith("guest:"):
        raise ValueError("guest tasks require a 'guest:<device_id>' scope")


def ensure_tables() -> None:
    con = _con()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS guest_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_scope TEXT NOT NULL,
                kind TEXT NOT NULL,            -- 'reminder' | 'note' | 'todo' | 'timer'
                text TEXT NOT NULL,
                due_at TEXT,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_guest_items ON guest_items(guest_scope, done);")
        con.commit()
    finally:
        con.close()


# ── capability blurb (what Mongoose tells a guest) ─────────────────────────────────────────────────

def capabilities_blurb() -> dict[str, Any]:
    from .. import profile as _profile
    host = _profile.owner_name()
    return {
        "intro": "While you're here, I can be a low-key helper, no pressure, nothing that touches "
                 f"{host}'s private stuff.",
        "can_do": [
            {"id": "reflect", "label": "Talk something through",
             "blurb": "A calm space to think out loud, a reframe, or a quick mood lift."},
            {"id": "focus", "label": "Help me focus / ADHD nudge",
             "blurb": "Break a task down, pick one next action, set a short timer, gentle check-ins."},
            {"id": "plan", "label": "Plan my day",
             "blurb": "Turn your list into a simple ordered plan for the time you've got."},
            {"id": "research", "label": "Look something up",
             "blurb": "A short, self-contained answer to a light question."},
            {"id": "remind", "label": "Remind me / jot a note",
             "blurb": "A quick reminder or note, just for you, only while you're using me."},
            {"id": "fun", "label": "Something fun",
             "blurb": "A fact, a riddle, a would-you-rather, or a cheer-up."},
            {"id": "bug", "label": "Report something broken",
             "blurb": "Tell me if something isn't working (a light, the wifi, a tablet) and I'll flag it for the owner."},
        ],
        "boundaries": f"I can't see or change {host}'s emails, calendar, files, home, or his schedule, "
                      "those stay private. Anything you jot here is yours and stays in your space.",
    }


# ── light coaching / research (short model turns) ──────────────────────────────────────────────────

async def _short_model(prompt: str, *, system: str | None = None) -> str:
    """A single short, low-compute model turn for guests. Never uses tools or owner context. Returns ''
    if no model is available."""
    try:
        from ..llm import ask_persona
        full = (system + "\n\n" if system else "") + prompt
        out = await ask_persona(full, role="chat")
        return (out or "").strip()
    except Exception:
        return ""


async def reflect(scope: str, message: str) -> dict[str, Any]:
    """A brief, supportive counselling-style reflection/reframe. Warm, non-clinical, no diagnosis."""
    _require_guest(scope)
    if _CRISIS_RE.search(message or ""):
        # Deterministic and first: never let this depend on the model being up, noticing the
        # wording, or the fallback string happening to fit. Return immediately, no model call.
        return {"kind": "reflect", "text": _CRISIS_RESPONSE, "crisis": True}
    sys = ("You are a warm, grounded listener helping a guest think something through. Reflect back "
           "what you hear, offer one gentle reframe or question, keep it short (2-4 sentences). You are "
           "NOT a therapist and must not diagnose; if they sound in crisis, kindly suggest they reach a "
           "real person or local support. No lists.")
    text = await _short_model(message, system=sys)
    if not text:
        text = ("I'm here and listening. It sounds like a lot is on your mind, what feels like the "
                "heaviest part right now?")
    return {"kind": "reflect", "text": text}


async def focus_step(scope: str, task: str) -> dict[str, Any]:
    """ADHD-style: break the task into a tiny first action + a short timer suggestion."""
    _require_guest(scope)
    if _CRISIS_RE.search(task or ""):
        # Same class as reflect() (external audit finding #5), just a sibling function the
        # audit didn't happen to test: the OLD fallback here echoed the task text verbatim
        # into a task-breakdown template, so crisis wording typed into the wrong flow got
        # something WORSE than generic -- "just open the thing for 'I want to kill myself'
        # and do the first tiny piece". Closing the whole class, not just the tested function.
        return {"kind": "focus", "text": _CRISIS_RESPONSE, "crisis": True}
    sys = ("You are a kind ADHD-style focus coach. Take the task and give: a one-line reframe so it "
           "feels smaller, then ONE concrete next action that takes under 10 minutes. End by suggesting "
           "a short timer (e.g. 'try 15 minutes'). Keep it to 3-4 short sentences, encouraging, no "
           "lecturing.")
    text = await _short_model(f"Task: {task}", system=sys)
    if not text:
        text = (f"Let's shrink it. Just open the thing for '{task}' and do the first tiny piece, no "
                f"finishing, just starting. Set a 15-minute timer and stop when it rings.")
    return {"kind": "focus", "text": text, "suggested_timer_minutes": 15}


async def quick_research(scope: str, question: str) -> dict[str, Any]:
    """A short, self-contained answer to a light question. One model turn, no tools, no owner data."""
    _require_guest(scope)
    if _CRISIS_RE.search(question or ""):
        return {"kind": "research", "text": _CRISIS_RESPONSE, "crisis": True}
    sys = ("Answer the guest's light question briefly and helpfully in 2-5 sentences. If you're not "
           "sure, say so honestly. Don't pad. No owner-specific or private information, you don't have "
           "access to any, and shouldn't imply you do.")
    text = await _short_model(question, system=sys)
    if not text:
        text = "I can't reach my model right now to look into that, but I'm happy to try again shortly."
    return {"kind": "research", "text": text}


def day_plan(scope: str, tasks: list[str]) -> dict[str, Any]:
    """Turn the guest's own list into a simple ordered plan. Pure-Python (no model needed), scope-local."""
    _require_guest(scope)
    items = [t.strip() for t in (tasks or []) if t and t.strip()]
    if not items:
        return {"kind": "plan", "text": "Tell me a few things you'd like to get done and I'll order them.",
                "steps": []}
    # Simple heuristic: keep order but front-load anything that reads time-sensitive. Match on whole
    # words (so "read a book" isn't flagged by "book"), and use unambiguous urgency cues.
    import re as _re
    urgent_words = ("call", "email", "pay", "reply", "submit", "deadline", "now", "today",
                    "urgent", "asap", "tonight", "send")
    pat = _re.compile(r"\b(" + "|".join(urgent_words) + r")\b", _re.I)
    def rank(t: str) -> int:
        return 0 if pat.search(t) else 1
    ordered = sorted(items, key=rank)
    steps = [{"n": i + 1, "task": t} for i, t in enumerate(ordered)]
    return {"kind": "plan", "steps": steps,
            "text": "Here's a simple order to tackle these, quick/time-sensitive first."}


# ── guest-scoped reminders / notes (NEVER Alex's scheduler) ────────────────────────────────────────

def add_item(scope: str, *, kind: str, text: str, due_at: str | None = None) -> dict[str, Any]:
    """Store a guest reminder/note/todo in the GUEST's own namespace. This never goes near Alex's tasks
    or the real scheduler."""
    _require_guest(scope)
    ensure_tables()
    if kind not in ("reminder", "note", "todo", "timer"):
        kind = "note"
    con = _con()
    try:
        cur = con.execute(
            "INSERT INTO guest_items (guest_scope, kind, text, due_at, done, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)", (scope, kind, text.strip(), due_at, _now_iso()))
        con.commit()
        return {"ok": True, "id": cur.lastrowid, "kind": kind, "text": text.strip(), "due_at": due_at}
    finally:
        con.close()


def list_items(scope: str, *, include_done: bool = False) -> list[dict[str, Any]]:
    """List THIS guest's items only, the query is hard-scoped to guest_scope, so a guest can never see
    another guest's or Alex's items."""
    _require_guest(scope)
    ensure_tables()
    con = _con()
    try:
        q = "SELECT * FROM guest_items WHERE guest_scope = ?"
        if not include_done:
            q += " AND done = 0"
        q += " ORDER BY (due_at IS NULL), due_at ASC, created_at ASC"
        return [dict(r) for r in con.execute(q, (scope,)).fetchall()]
    finally:
        con.close()


def complete_item(scope: str, item_id: int) -> dict[str, Any]:
    """Mark one of THIS guest's items done. Scoped so a guest can't touch items that aren't theirs."""
    _require_guest(scope)
    ensure_tables()
    con = _con()
    try:
        cur = con.execute("UPDATE guest_items SET done = 1 WHERE id = ? AND guest_scope = ?",
                          (item_id, scope))
        con.commit()
        return {"ok": cur.rowcount > 0, "id": item_id}
    finally:
        con.close()
