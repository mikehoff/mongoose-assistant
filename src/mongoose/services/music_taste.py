"""Music taste, Mongoose's silly-but-genuine obsession with learning what everyone listens to.

Mongoose really wants to know your music: favourite singers (male and female), the decades you love,
genres, soundtracks, bands, the songs from your childhood, your teenage obsessions, and what you've
discovered recently. He asks about it in a goofy, low-stakes way, remembers it, and then, forever after
, occasionally lobs a "hey was thinking, is this song up your alley? (X by Y) lemme know lol" your way.

This module is the brain of that:
  - a per-scope PROFILE (owner or guest:<device>) storing what's been learned, as free-text lists per
    facet, plus which interview questions have been asked/answered;
  - a QUESTION BANK covering every facet, phrased playfully;
  - an ONBOARDING state machine: a brand-new user gets an eager flurry of questions on an ESCALATING
    schedule (close together at first, then spacing out), then it settles into the normal rhythm;
  - a quirky, always-freshly-phrased RECOMMENDATION generator for the normal surpriser.

No model is required for the mechanics (questions, scheduling, profile). The recommendation text is
templated + randomised so it sounds fresh without inventing facts about real songs, it suggests a real
song only when one is supplied (e.g. by a model elsewhere); otherwise it asks an open music question,
which is just as on-brand.

Scope isolation: everything is keyed by scope string, exactly like memory. A guest's music profile is
separate from the owner's.
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

OWNER_SCOPE = "owner"

# The facets Mongoose wants to learn. Order is roughly the order he'll ask.
FACETS = [
    "fave_singers",        # favourite singers in general
    "fave_female_singers", # specifically female vocalists
    "fave_male_singers",   # specifically male vocalists
    "fave_bands",          # bands/groups
    "fave_genres",         # genres
    "fave_decades",        # decades/eras
    "soundtracks",         # film/game/show soundtracks they love
    "childhood_hits",      # songs from childhood
    "teenage_obsessions",  # what they played to death as a teenager
    "recent_discoveries",  # newest finds
]

# Playful question bank, one or more phrasings per facet. {name} is filled if known.
_QUESTIONS: dict[str, list[str]] = {
    "fave_singers": [
        "ok important question: who are your absolute favourite singers? i need to know your soul",
        "if your life had a vocalist, who'd it be? give me your top singers",
    ],
    "fave_female_singers": [
        "which women own your playlists? fave female singers, go",
        "the female vocalist you'd never skip, who is she?",
    ],
    "fave_male_singers": [
        "and the lads, favourite male singers? lay em on me",
        "male vocalist you always turn up for?",
    ],
    "fave_bands": [
        "bands. groups. ensembles of legends. who are yours?",
        "which band would you see live tomorrow no questions asked?",
    ],
    "fave_genres": [
        "what genres are you actually about? (i won't judge, much)",
        "if i had to fill a room with one genre for you, what's it gotta be?",
    ],
    "fave_decades": [
        "what's your decade? 60s soul? 80s synth? 2010s chaos? tell me your era",
        "which decade of music just *gets* you?",
    ],
    "soundtracks": [
        "ok soundtracks, any film/show/game scores you secretly adore?",
        "is there a soundtrack that lives in your head rent free?",
    ],
    "childhood_hits": [
        "what songs were ON in your house as a kid? childhood bangers please",
        "first song you remember loving as a little human?",
    ],
    "teenage_obsessions": [
        "teenage you, what did you play into the GROUND? no shame here",
        "the song your teenage self would die for, what is it?",
    ],
    "recent_discoveries": [
        "anything you've discovered lately that you can't stop replaying?",
        "newest musical find? something you bumped this week?",
    ],
}

# Quirky recommendation lead-ins. {song} is filled with "X by Y". Always randomised so it sounds fresh.
_REC_TEMPLATES = [
    "hey was thinking, is {song} up your alley? lemme know if u like lol",
    "random but, {song}. felt like a you song? tell me if i'm onto smth",
    "ok hear me out: {song}? i feel like this might slap for you. yes? no?",
    "lowkey think you'd vibe with {song}. am i right or am i WAY off lol",
    "throwing this at you: {song}. up your street? or did i fumble it",
    "psst, {song}? had a hunch it's your kinda thing. verdict?",
    "i may have been thinking about your taste again. {song}? worth a listen?",
    "is {song} a hit or a miss for you? genuinely need to know lol",
]

# When Mongoose has no specific song to suggest, he just asks an open music question (still on-brand).
_OPEN_MUSIC_NUDGES = [
    "what've you been listening to lately? need new stuff for my mental playlist of You",
    "give me one song you love right now, building my picture of your taste lol",
    "quick: a song that always works for you. any genre. go",
    "i'm bored of my own playlists. what should i be obsessed with this week?",
]

# --- Fresh phrasing for the proactive surpriser -------------------------------------------------
# A surpriser must feel spontaneous, never like a recurring task reminder. So every reach-out draws a
# random, warm, a-little-dry line from a pool (chosen so it is never the same twice in a row) rather
# than reciting one fixed phrase. These lead-ins pair with the actual music question that follows.

# Thread lead-ins for a PROACTIVE first-contact invite. Each ends before the question so "<opener><q>"
# reads naturally. Warm, low-stakes, a touch dry. Never a task, always a passing thought.
_INVITE_OPENERS = [
    "Hey, Mongoose here. No agenda, I just got curious about your music. ",
    "Random one, whenever you fancy it. ",
    "Music thought, no rush at all. ",
    "This just popped into my head, so I am asking. ",
    "Not urgent, only a bit nosy about your taste. ",
    "Building my mental playlist of You, so. ",
    "I have been wondering about your taste in noise. ",
    "Quick low-stakes question, ignore it if you like. ",
    "Mongoose here, thinking about music again. ",
    "One for the void, answer or do not. ",
    "Idle curiosity, no pressure whatsoever. ",
]

# Thread lead-ins for the rare come-answer reminder (paired with the still-open question). These make
# clear it is a gentle poke, not a nag, and that Mongoose will happily let it drop.
_REMINDER_OPENERS = [
    "Still a little curious about this one, whenever you fancy it. ",
    "No pressure, just still wondering. ",
    "This is not going anywhere, but if the mood strikes. ",
    "Circling back once, then I will leave it be. ",
    "Still lodged in my head, this question. ",
    "Not nagging, only still a bit curious. ",
    "One gentle poke, then quiet I promise. ",
    "If you ever fancy answering, this is still here. ",
    "Idle thought: this one is still open. ",
    "Whenever, or never, but here it is again. ",
]

# Phone-notification titles. Short, playful, phone-friendly.
_INVITE_PUSH_TITLES = [
    "🪿🪇 Mongoose is curious",
    "🪿🎧 A music thought",
    "🪿🪇 Mongoose wonders",
    "🪿🎶 Idle music question",
]
_REMINDER_PUSH_TITLES = [
    "🪿🪇 Still curious",
    "🪿🎧 A gentle poke",
    "🪿🎶 One music question",
]

# Phone-notification bodies. Never framed as a chore, always a passing curiosity.
_INVITE_PUSH_BODIES = [
    "mongoose got curious about your music. come say hi in your private chat.",
    "there is a music question waiting in your private chat, no rush.",
    "mongoose is being a bit nosy about your taste. peek when you fancy it.",
    "a little music question is sitting in your private chat.",
    "mongoose would love to know what you listen to, whenever you like.",
    "wandered over to ask about your music. it is in your private chat.",
    "no agenda, just a music question in your private chat.",
    "mongoose has a music thought for you. private chat when you get a sec.",
    "curious about your taste in noise. come chat music sometime.",
]
_REMINDER_PUSH_BODIES = [
    "a music question is still waiting in your private chat, no rush.",
    "mongoose is still a little curious. peek whenever you like.",
    "that music question is still there if you fancy it.",
    "no pressure, just a music thought still sitting in your chat.",
    "still wondering about your taste. answer whenever, or not.",
    "one gentle nudge about the music question in your private chat.",
    "mongoose left a music question for you, still open.",
    "your private chat has a music question you have not seen yet.",
]

# Remember the last line handed out per pool so two consecutive reach-outs never read identically.
# Process-local (a scheduler sweep is one process); good enough to kill back-to-back repeats.
_last_line_idx: dict[str, int] = {}


def _pick(pool_key: str, pool: list[str]) -> str:
    """A random line from a pool, never the exact line returned last time for this pool, so the
    surpriser is freshly phrased every time and never repeats itself back-to-back."""
    if not pool:
        return ""
    if len(pool) == 1:
        return pool[0]
    last = _last_line_idx.get(pool_key)
    idx = random.randrange(len(pool))
    while idx == last:
        idx = random.randrange(len(pool))
    _last_line_idx[pool_key] = idx
    return pool[idx]


def invite_opener() -> str:
    """A fresh, warm lead-in for a proactive music invite (prefix to the question)."""
    return _pick("invite_opener", _INVITE_OPENERS)


def reminder_opener() -> str:
    """A fresh, gentle lead-in for the rare come-answer reminder (prefix to the still-open question)."""
    return _pick("reminder_opener", _REMINDER_OPENERS)


def invite_push_title() -> str:
    return _pick("invite_push_title", _INVITE_PUSH_TITLES)


def invite_push_body() -> str:
    return _pick("invite_push_body", _INVITE_PUSH_BODIES)


def reminder_push_title() -> str:
    return _pick("reminder_push_title", _REMINDER_PUSH_TITLES)


def reminder_push_body() -> str:
    return _pick("reminder_push_body", _REMINDER_PUSH_BODIES)

# Escalating onboarding schedule for proactive music nudges. Each tuple is
# (interval_minutes, window_minutes): ask at this interval for this long, then move to the next band;
# finally fall through to normal cadence. The gap between nudges lengthens as onboarding proceeds so
# the eager early flurry settles into an occasional touch. This is NOT gated behind a tab opt-in:
# Mongoose being music-curious, proactively as well as in chat, is part of who he is. Quiet hours,
# the proactive-delight cadence, rate-limiting, and the "only when nothing substantive" rule still
# govern when a nudge actually fires, so it never becomes a needy minute-by-minute interrogation.
#   1 min apart, then 2, then 4, then 8, then 10, then 10, then normal cadence.
ONBOARDING_SCHEDULE = [
    (1, 2),
    (2, 4),
    (4, 8),
    (8, 16),
    (10, 20),
    (10, 20),
]

# If the user ignores this many music questions in a row, slow the next one down by an hour so we stop
# nudging into silence. A flat add while the streak stands, not compounding.
_UNANSWERED_PENALTY_THRESHOLD = 2
_UNANSWERED_PENALTY_SECONDS = 3600

# The schedule is chat-gated: it only advances while the owner is actively chatting. This window
# defines "actively chatting" (a nudge only fires if the last chat was within it) and also the idle
# gap beyond which the onboarding clock is frozen and later resumed where it left off.
_ENGAGED_WINDOW_SECONDS = 900  # 15 minutes

# When a music question was asked (in chat) within this window, the owner's next chatty message is
# treated as the answer to it. Kept generous so a reply a little later still lands, but the caller only
# captures messages that read like answers (chat intent, not a fresh command or question).
_ANSWER_CAPTURE_WINDOW_SECONDS = 1800  # 30 minutes


def _db_path() -> Path:
    from ..settings import get_settings
    s = get_settings()
    return Path(getattr(s, "db_path", None) or (Path(getattr(s, "data_dir", "data")) / "mongoose.sqlite3"))


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path()))
    con.row_factory = sqlite3.Row
    return con


def ensure_table() -> None:
    """Lazily create the music profile table (works on an older DB)."""
    con = _con()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS music_taste (
                scope        TEXT PRIMARY KEY,
                facets_json  TEXT NOT NULL DEFAULT '{}',
                asked_json   TEXT NOT NULL DEFAULT '[]',
                started_at   REAL,
                last_asked_at REAL,
                onboarded    INTEGER NOT NULL DEFAULT 0,
                opted_in     INTEGER NOT NULL DEFAULT 0,
                opted_out    INTEGER NOT NULL DEFAULT 0,
                unanswered_streak INTEGER NOT NULL DEFAULT 0,
                last_chat_at REAL
            )
            """
        )
        # Migrate an older table that predates these columns: add whichever are missing.
        cols = {r[1] for r in con.execute("PRAGMA table_info(music_taste)").fetchall()}
        if "opted_in" not in cols:
            con.execute("ALTER TABLE music_taste ADD COLUMN opted_in INTEGER NOT NULL DEFAULT 0")
        if "opted_out" not in cols:
            con.execute("ALTER TABLE music_taste ADD COLUMN opted_out INTEGER NOT NULL DEFAULT 0")
        if "unanswered_streak" not in cols:
            con.execute("ALTER TABLE music_taste ADD COLUMN unanswered_streak INTEGER NOT NULL DEFAULT 0")
        if "last_chat_at" not in cols:
            con.execute("ALTER TABLE music_taste ADD COLUMN last_chat_at REAL")
        if "nudged_at" not in cols:
            # When we last sent the "come answer my music question" push for this scope, so the
            # 40-minute nudge fires at most once per unanswered question (re-armed by a new question).
            con.execute("ALTER TABLE music_taste ADD COLUMN nudged_at REAL")
        if "invited_at" not in cols:
            # When we last sent a proactive "come talk music" invite, to rate-limit first-contact.
            con.execute("ALTER TABLE music_taste ADD COLUMN invited_at REAL")
        if "invite_count" not in cols:
            # How many proactive invites have gone out without engagement, to cap the courting.
            con.execute("ALTER TABLE music_taste ADD COLUMN invite_count INTEGER NOT NULL DEFAULT 0")
        if "next_invite_at" not in cols:
            # Earliest time the NEXT proactive invite may fire. Rolled to a fresh random long gap each
            # time an invite goes out, so the courting never settles into a predictable daily rhythm.
            con.execute("ALTER TABLE music_taste ADD COLUMN next_invite_at REAL")
        if "reminder_at" not in cols:
            # When the ONE gentle come-answer reminder for the current outstanding question should fire,
            # or NULL if we rolled (probabilistically) to send no reminder at all for that question.
            con.execute("ALTER TABLE music_taste ADD COLUMN reminder_at REAL")
        con.commit()
    finally:
        con.close()


def _now() -> float:
    return time.time()


def _load(scope: str) -> dict[str, Any]:
    ensure_table()
    con = _con()
    try:
        row = con.execute("SELECT * FROM music_taste WHERE scope =?", (scope,)).fetchone()
        if row is None:
            return {"scope": scope, "facets": {}, "asked": [], "started_at": None,
                    "last_asked_at": None, "onboarded": False, "opted_in": False, "opted_out": False,
                    "unanswered_streak": 0, "last_chat_at": None, "nudged_at": None,
                    "invited_at": None, "invite_count": 0, "next_invite_at": None, "reminder_at": None}
        keys = row.keys()
        return {
            "scope": scope,
            "facets": json.loads(row["facets_json"] or "{}"),
            "asked": json.loads(row["asked_json"] or "[]"),
            "started_at": row["started_at"],
            "last_asked_at": row["last_asked_at"],
            "onboarded": bool(row["onboarded"]),
            "opted_in": bool(row["opted_in"]) if "opted_in" in keys else False,
            "opted_out": bool(row["opted_out"]) if "opted_out" in keys else False,
            "unanswered_streak": int(row["unanswered_streak"]) if "unanswered_streak" in keys and row["unanswered_streak"] is not None else 0,
            "last_chat_at": row["last_chat_at"] if "last_chat_at" in keys else None,
            "nudged_at": row["nudged_at"] if "nudged_at" in keys else None,
            "invited_at": row["invited_at"] if "invited_at" in keys else None,
            "invite_count": int(row["invite_count"]) if "invite_count" in keys and row["invite_count"] is not None else 0,
            "next_invite_at": row["next_invite_at"] if "next_invite_at" in keys else None,
            "reminder_at": row["reminder_at"] if "reminder_at" in keys else None,
        }
    finally:
        con.close()


def _save(state: dict[str, Any]) -> None:
    ensure_table()
    con = _con()
    try:
        con.execute(
            """INSERT INTO music_taste (scope, facets_json, asked_json, started_at, last_asked_at, onboarded, opted_in, opted_out, unanswered_streak, last_chat_at, nudged_at, invited_at, invite_count, next_invite_at, reminder_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(scope) DO UPDATE SET
                 facets_json=excluded.facets_json, asked_json=excluded.asked_json,
                 started_at=excluded.started_at, last_asked_at=excluded.last_asked_at,
                 onboarded=excluded.onboarded, opted_in=excluded.opted_in, opted_out=excluded.opted_out,
                 unanswered_streak=excluded.unanswered_streak, last_chat_at=excluded.last_chat_at,
                 nudged_at=excluded.nudged_at, invited_at=excluded.invited_at, invite_count=excluded.invite_count,
                 next_invite_at=excluded.next_invite_at, reminder_at=excluded.reminder_at""",
            (state["scope"], json.dumps(state.get("facets", {})), json.dumps(state.get("asked", [])),
             state.get("started_at"), state.get("last_asked_at"), 1 if state.get("onboarded") else 0,
             1 if state.get("opted_in") else 0, 1 if state.get("opted_out") else 0,
             int(state.get("unanswered_streak", 0) or 0), state.get("last_chat_at"), state.get("nudged_at"),
             state.get("invited_at"), int(state.get("invite_count", 0) or 0),
             state.get("next_invite_at"), state.get("reminder_at")),
        )
        con.commit()
    finally:
        con.close()


def has_opted_in(scope: str = OWNER_SCOPE) -> bool:
    """True if the user has explicitly engaged the music chat for this scope (answered a music question,
    used the music feature, or toggled it on). Retained for state display and backward compatibility.
    Proactive nudges are NO LONGER gated on this: see is_opted_out for the flag that actually suppresses
    them."""
    return bool(_load(scope).get("opted_in"))


def is_opted_out(scope: str = OWNER_SCOPE) -> bool:
    """True if the user has explicitly turned proactive music off for this scope. This is the ONLY thing
    that silences proactive music nudges; by default (fresh scope) it is False, so nudges flow."""
    return bool(_load(scope).get("opted_out"))


def opt_in(scope: str = OWNER_SCOPE) -> dict[str, Any]:
    """Explicitly (re)enable the music chat for this scope and start the interview clock. Clears any
    prior opt-out. Called when the user answers a music question or flips the toggle on. Idempotent."""
    st = _load(scope)
    st["opted_in"] = True
    st["opted_out"] = False
    if st.get("started_at") is None:
        st["started_at"] = _now()
    _save(st)
    return st


def opt_out(scope: str = OWNER_SCOPE) -> dict[str, Any]:
    """Turn proactive music off for this scope: sets opted_out, which is the one flag that stops the
    onboarding nudges and proactive recs. Chat-time music curiosity (the persona) is unaffected."""
    st = _load(scope)
    st["opted_in"] = False
    st["opted_out"] = True
    _save(st)
    return st


def begin_if_new(scope: str = OWNER_SCOPE) -> dict[str, Any]:
    """Start the interview clock for a fresh scope. Proactive music nudges are part of who Mongoose is,
    so this is NOT gated behind an opt-in: it starts the clock unless the user has explicitly turned
    proactive music off (opted_out). Cadence controls (quiet hours, rate-limit, only-when-nothing-
    substantive) still decide when a nudge actually fires."""
    st = _load(scope)
    if not st.get("opted_out") and st.get("started_at") is None:
        st["started_at"] = _now()
        _save(st)
    return st


def record_answer(facet: str, value: str, scope: str = OWNER_SCOPE) -> dict[str, Any]:
    """Store what the user told us for a facet (free text, appended to that facet's list). Marks the
    facet asked. Returns {ok, facet, values}."""
    if facet not in FACETS:
        return {"ok": False, "error": f"unknown facet '{facet}'"}
    value = (value or "").strip()
    if not value:
        return {"ok": False, "error": "empty value"}
    st = _load(scope)
    facets = st.setdefault("facets", {})
    lst = facets.setdefault(facet, [])
    if value not in lst:
        lst.append(value)
    asked = st.setdefault("asked", [])
    if facet not in asked:
        asked.append(facet)
    # Answering a music question IS an explicit opt-in to the music chat, and starts the clock.
    st["opted_in"] = True
    if st["started_at"] is None:
        st["started_at"] = _now()
    # The user engaged: clear any unanswered streak so the next question is back on the normal cadence,
    # and clear the proactive-invite counter so a re-engaged person is never held against the invite cap.
    st["unanswered_streak"] = 0
    st["invite_count"] = 0
    _maybe_complete(st)
    _save(st)
    return {"ok": True, "facet": facet, "values": lst}


def _maybe_complete(st: dict[str, Any]) -> None:
    """Onboarding is 'done' once most facets have an answer (or all have been asked)."""
    answered = sum(1 for f in FACETS if st.get("facets", {}).get(f))
    if answered >= max(4, len(FACETS) - 3) or len(st.get("asked", [])) >= len(FACETS):
        st["onboarded"] = True


def next_unasked_facet(scope: str = OWNER_SCOPE) -> Optional[str]:
    """The next facet we haven't asked about yet, in FACET order. None if all asked."""
    st = _load(scope)
    asked = set(st.get("asked", []))
    for f in FACETS:
        if f not in asked:
            return f
    return None


def mark_asked(facet: str, scope: str = OWNER_SCOPE) -> None:
    st = _load(scope)
    if facet not in st.setdefault("asked", []):
        st["asked"].append(facet)
    st["last_asked_at"] = _now()
    # Count this as one more question asked without an answer yet. record_answer resets it to 0.
    st["unanswered_streak"] = int(st.get("unanswered_streak", 0)) + 1
    st["nudged_at"] = None  # a fresh question re-arms the come-answer reminder
    # Roll the reminder plan for THIS question up front, once, so the rarity and the timing are decided
    # per question rather than re-diced every scheduler sweep. Usually there is no reminder at all; when
    # there is, it lands at a random 1 to 3 days out. reminder_at=None means "let this one be".
    st["reminder_at"] = (_now() + _reminder_delay()) if _should_send_reminder() else None
    _maybe_complete(st)
    _save(st)


# The come-answer reminder is the part most likely to feel like nagging, so it barely ever fires. Two
# dice are rolled ONCE per outstanding question (in mark_asked), not every scheduler sweep, so the
# behaviour is spontaneous rather than clockwork:
#   1. a coin-flip decides whether this question earns any reminder at all (usually it does not), and
#   2. if it does, a random 1 to 3 day delay decides when the single reminder lands.
# Net effect: most unanswered questions are simply left be, and the occasional one gets one gentle,
# unpredictably-timed poke. A surpriser nags rarely, and never on a schedule.
_REMINDER_DELAY_MIN_SECONDS = 1 * 24 * 3600  # soonest a reminder can land after the question
_REMINDER_DELAY_MAX_SECONDS = 3 * 24 * 3600  # latest, re-rolled per question
_REMINDER_PROBABILITY = 0.5                   # chance an unanswered question earns a reminder at all


def _reminder_delay() -> float:
    """A fresh random 1 to 3 day wait before an unanswered question's single reminder may fire. Rolled
    per question so the timing never becomes predictable."""
    return random.uniform(_REMINDER_DELAY_MIN_SECONDS, _REMINDER_DELAY_MAX_SECONDS)


def _should_send_reminder() -> bool:
    """Coin-flip (~half) whether an unanswered question earns a single gentle reminder at all. Usually
    False, so the question is simply left alone. Keeps the surpriser from ever feeling like a nag."""
    return random.random() < _REMINDER_PROBABILITY


def all_scopes() -> list[str]:
    """Every scope that has music-taste state (owner + each guest), for the nudge sweep."""
    ensure_table()
    con = _con()
    try:
        return [r[0] for r in con.execute("SELECT scope FROM music_taste").fetchall()]
    finally:
        con.close()


def unanswered_asked_facet(scope: str = OWNER_SCOPE) -> Optional[str]:
    """The most recent facet asked in chat that still has no answer, ignoring the answer-capture
    window (unlike pending_facet). None if every asked facet has been answered."""
    st = _load(scope)
    facets = st.get("facets", {})
    for f in reversed(st.get("asked", [])):
        if not facets.get(f):
            return f
    return None


def nudge_due(scope: str = OWNER_SCOPE) -> bool:
    """True only when the current unanswered question's single, pre-rolled reminder time has arrived.
    Most questions never reach here: the reminder is planned only ~half the time (see mark_asked /
    _should_send_reminder), and even then only after a random 1 to 3 days. reminder_at is cleared once
    the reminder fires (mark_nudged) and re-rolled when a new question is asked, so there is at most one
    reminder per outstanding question and it never bursts."""
    st = _load(scope)
    if unanswered_asked_facet(scope) is None:
        return False
    ra = st.get("reminder_at")
    if not ra:
        return False  # no reminder planned for this question (the usual case), or already sent
    return _now() >= float(ra)


def mark_nudged(scope: str = OWNER_SCOPE) -> None:
    st = _load(scope)
    st["nudged_at"] = _now()
    st["reminder_at"] = None  # consumed: at most one gentle reminder per outstanding question
    _save(st)


# Proactive first-contact: Mongoose invites someone to the music interview WITHOUT waiting for them to
# message first. This is the fix for the logged bug "music interview should not have to wait for someone
# to message in private chat first". A surpriser must feel spontaneous, not scheduled, so the invites do
# NOT fire on a fixed daily clock: after each invite we roll a fresh, random, long gap (3 to 9 days)
# before the next may fire, regenerated every time, so it never settles into a rhythm the owner can feel
# coming. It also still stops after a few ignored invites so it never becomes nagging (the owner can
# start it any time by chatting).
_INVITE_GAP_MIN_SECONDS = 3 * 24 * 3600  # never sooner than ~3 days after the previous invite
_INVITE_GAP_MAX_SECONDS = 9 * 24 * 3600  # and up to ~9 days, re-rolled each time an invite fires
_PROACTIVE_INVITE_CAP = 3                # stop proactively inviting after this many, if ignored


def _next_invite_delay() -> float:
    """A fresh, random long gap (3 to 9 days) until the next proactive invite may fire. Rolled each time
    an invite goes out so the courting never falls into a predictable daily rhythm: it should feel like
    Mongoose just happened to think of you, not like a scheduled task."""
    return random.uniform(_INVITE_GAP_MIN_SECONDS, _INVITE_GAP_MAX_SECONDS)


def invite_due(scope: str = OWNER_SCOPE) -> bool:
    """True when Mongoose should proactively INVITE this scope into the music interview, independent of
    whether they have chatted. Only while onboarding, only when nothing is already awaiting their answer
    (the reminder path covers that case), never past the invite cap, and only once the randomly-rolled
    next-invite time has arrived (a fresh scope has none rolled yet, so its first invite is due at once)."""
    if is_opted_out(scope) or not is_onboarding(scope):
        return False
    if unanswered_asked_facet(scope) is not None:
        return False  # a question is already out; the re-nudge path handles reminding them
    st = _load(scope)
    if int(st.get("invite_count", 0)) >= _PROACTIVE_INVITE_CAP:
        return False
    na = st.get("next_invite_at")
    if na is None:
        return True  # never invited this scope yet: no gap rolled, so the first invite is due
    return _now() >= float(na)


def mark_invited(scope: str = OWNER_SCOPE) -> None:
    """Record that a proactive invite went out and roll a fresh random long gap before the next one may
    fire, so the cadence never settles into a rhythm. A subsequent answer resets the counter (see
    record_answer), so someone who engages is never counted against the ignore cap."""
    st = _load(scope)
    st["invited_at"] = _now()
    st["invite_count"] = int(st.get("invite_count", 0)) + 1
    st["next_invite_at"] = _now() + _next_invite_delay()
    _save(st)


def note_chat_activity(scope: str = OWNER_SCOPE) -> None:
    """Record that the owner is actively chatting, and freeze the onboarding schedule across any
    stretch of not-chatting. The escalating music schedule should only advance while the owner is
    engaged: if they go quiet, the clock pauses and resumes where it left off when they come back.
    We implement that by sliding started_at (and last_asked_at) forward by any idle gap longer than
    the engaged window, so elapsed-since-start reflects engaged time rather than wall-clock silence.
    Cheap and safe to call on every owner turn."""
    st = _load(scope)
    now = _now()
    last_chat = st.get("last_chat_at")
    if last_chat is not None:
        gap = now - last_chat
        if gap > _ENGAGED_WINDOW_SECONDS:
            # The owner stepped away. Slide the schedule's clock forward by the whole idle gap so the
            # onboarding band and the next-question timer resume from exactly where they paused.
            if st.get("started_at") is not None:
                st["started_at"] = float(st["started_at"]) + gap
            if st.get("last_asked_at"):
                st["last_asked_at"] = float(st["last_asked_at"]) + gap
    st["last_chat_at"] = now
    _save(st)


def pending_facet(scope: str = OWNER_SCOPE) -> Optional[str]:
    """The facet the owner was most recently asked about in chat but has not answered yet, if that
    question is still recent enough that the next chatty message could be its answer. None otherwise."""
    st = _load(scope)
    if st.get("opted_out"):
        return None
    facets = st.get("facets", {})
    last_asked = st.get("last_asked_at") or 0.0
    if not last_asked or (_now() - float(last_asked)) > _ANSWER_CAPTURE_WINDOW_SECONDS:
        return None
    for f in reversed(st.get("asked", [])):
        if not facets.get(f):
            return f
    return None


def capture_answer(message: str, scope: str = OWNER_SCOPE) -> Optional[str]:
    """If a music question is pending (asked recently in chat, not yet answered), record `message` as
    its answer and return the facet. Returns None if nothing was pending. The CALLER decides whether the
    message reads like an answer (chat intent, not a fresh command or question) before calling this."""
    facet = pending_facet(scope)
    if facet is None:
        return None
    value = (message or "").strip()
    if not value:
        return None
    res = record_answer(facet, value, scope)
    return facet if res.get("ok") else None


def question_for(facet: str, *, name: str = "") -> str:
    """A playful question for a facet, randomised phrasing."""
    opts = _QUESTIONS.get(facet) or [f"tell me about your {facet.replace('_', ' ')}"]
    q = random.choice(opts)
    if name and "{name}" in q:
        q = q.format(name=name)
    return q


def is_onboarding(scope: str = OWNER_SCOPE) -> bool:
    """True while we're in the eager get-to-know-your-music phase. This runs by default for a fresh
    scope: proactive music curiosity is part of the persona, not an opt-in. A scope that has explicitly
    turned proactive music off (opted_out) is never 'onboarding'."""
    st = _load(scope)
    if st.get("opted_out"):
        return False
    if st.get("onboarded"):
        return False
    # onboarding applies once started and while there are still unasked facets
    return st.get("started_at") is not None and any(
        f not in set(st.get("asked", [])) for f in FACETS)


def onboarding_interval_seconds(scope: str = OWNER_SCOPE) -> Optional[int]:
    """Given how long ago onboarding started, return the CURRENT desired interval (seconds) between
    music nudges per the escalating schedule, or None if we've passed the schedule (fall back to normal).
    Schedule: 1min apart for the first 2min, 2min apart for the next 4min, 4min apart for the next 8min.
    """
    st = _load(scope)
    started = st.get("started_at")
    if started is None:
        return None
    elapsed_min = (_now() - started) / 60.0
    cursor = 0.0
    base: Optional[int] = None
    for interval_min, window_min in ONBOARDING_SCHEDULE:
        if elapsed_min < cursor + window_min:
            base = int(interval_min * 60)
            break
        cursor += window_min
    penalised = int(st.get("unanswered_streak", 0)) >= _UNANSWERED_PENALTY_THRESHOLD
    if base is None:
        # We are past the escalating window. Normally that means fall back to normal cadence (None).
        # But if the owner has ignored the last couple of questions, we still owe them the delayed
        # next one: hold it to an hour past the final band's spacing rather than dropping onboarding.
        if penalised:
            return int(ONBOARDING_SCHEDULE[-1][0] * 60) + _UNANSWERED_PENALTY_SECONDS
        return None
    # If the owner has left the last couple of music questions unanswered, slow the next one down by
    # an hour so we stop nudging into silence. Flat while the streak stands, does not compound.
    if penalised:
        base += _UNANSWERED_PENALTY_SECONDS
    return base


def due_for_onboarding_nudge(scope: str = OWNER_SCOPE) -> bool:
    """During onboarding, is it time for the next music question per the escalating schedule?
    Chat-gated: nothing fires unless the owner has been chatting within the engaged window, so the
    timing is effectively stopped while they are away and resumes when they start chatting again."""
    if not is_onboarding(scope):
        return False
    st = _load(scope)
    last_chat = st.get("last_chat_at")
    if last_chat is None or (_now() - float(last_chat)) > _ENGAGED_WINDOW_SECONDS:
        return False
    interval = onboarding_interval_seconds(scope)
    if interval is None:
        return False
    last = st.get("last_asked_at") or 0.0
    return (_now() - last) >= interval


def onboarding_nudge(scope: str = OWNER_SCOPE, *, name: str = "") -> Optional[dict[str, Any]]:
    """Produce the next onboarding music question (and mark it asked), or None if not due / not onboarding."""
    if not due_for_onboarding_nudge(scope):
        return None
    facet = next_unasked_facet(scope)
    if facet is None:
        return None
    q = question_for(facet, name=name)
    # Lead the proactive question with a light "by the way" opener so it lands as an aside, not an
    # interrogation. Use the owner's name when we have it.
    name = (name or "").strip()
    opener = f"By the way {name}, " if name else "By the way, "
    text = opener + q
    # Do NOT mark_asked here. This only GENERATES the question; a caller (e.g. the surpriser) may then
    # FILTER it out and never deliver it, yet marking-asked would open the answer-capture window and
    # silently eat the owner's next message (this filed 'build RTM', 'confirm ...', 'Implement ...' as
    # music answers for a whole session). The caller that actually DELIVERS the question calls
    # mark_asked(facet) after showing it.
    return {"kind": "music_interview", "facet": facet, "text": text,
            "family": "music_interview", "signature": f"music_q:{facet}"}


def profile(scope: str = OWNER_SCOPE) -> dict[str, Any]:
    """The learned music profile for a scope (facet -> list of values)."""
    st = _load(scope)
    return {"scope": scope, "facets": st.get("facets", {}), "onboarded": st.get("onboarded", False),
            "asked": st.get("asked", [])}


def profile_summary(scope: str = OWNER_SCOPE) -> str:
    """A compact human/persona-readable summary of what we know, for weaving into chat or the persona."""
    facets = _load(scope).get("facets", {})
    if not facets:
        return ""
    label = {
        "fave_singers": "singers", "fave_female_singers": "female vocalists",
        "fave_male_singers": "male vocalists", "fave_bands": "bands", "fave_genres": "genres",
        "fave_decades": "decades", "soundtracks": "soundtracks", "childhood_hits": "childhood songs",
        "teenage_obsessions": "teenage favourites", "recent_discoveries": "recent finds",
    }
    bits = []
    for f in FACETS:
        vals = facets.get(f)
        if vals:
            bits.append(f"{label.get(f, f)}: {', '.join(vals[:4])}")
    return "; ".join(bits)


def recommendation_text(song: str = "", *, scope: str = OWNER_SCOPE) -> str:
    """A quirky, freshly-randomised music nudge. If a real 'X by Y' song string is supplied, suggest it;
    otherwise ask an open music question (still on-brand). Never invents a fake song title itself."""
    song = (song or "").strip()
    if song:
        return random.choice(_REC_TEMPLATES).format(song=song)
    return random.choice(_OPEN_MUSIC_NUDGES)


def recommendation_candidate(song: str = "", *, scope: str = OWNER_SCOPE) -> dict[str, Any]:
    """A surpriser-shaped candidate for a music recommendation/nudge."""
    text = recommendation_text(song, scope=scope)
    # signature varies so it never reads as a repeat; family lets the surpriser rate-limit the SHAPE.
    sig = f"music_rec:{abs(hash(text)) % 10_000_000}"
    return {"kind": "music_rec", "text": text, "why": "Mongoose loves swapping music taste",
            "score": 0.5, "signature": sig, "family": "music_rec"}


# --- Person-to-person recommendation copy -------------------------------------------------------
# When one person recommends a song FOR another, Mongoose delivers it into the recipient's own chat
# thread in his warm, a-little-dry voice, attributed to the sender. {who} is the recommender's name,
# {song} the "X by Y" string. Randomised so two arrivals never read identically back-to-back.
_DIRECT_REC_TEMPLATES = [
    "{who} says you'd like this: '{song}'. Give it a spin?",
    "{who} reckons '{song}' is right up your street. Worth a listen?",
    "passing this along from {who}: '{song}'. See what you make of it.",
    "{who} wanted you to hear '{song}'. Verdict once you've had a listen?",
    "a song for you, courtesy of {who}: '{song}'. Any good?",
    "{who} thought of you and '{song}'. Give it a go and let them know?",
    "{who} is fairly sure '{song}' is your kind of thing. Fancy a listen?",
    "from {who}, with taste: '{song}'. Up your alley, or way off?",
]

# Short, phone-friendly push titles/bodies for an incoming person-to-person recommendation.
_DIRECT_REC_PUSH_TITLES = [
    "🪿🎧 A song for you",
    "🪿🎶 Someone's got a track for you",
    "🪿🎵 Music passed your way",
]


def direct_recommendation_text(from_name: str, song: str, note: str = "") -> str:
    """The message dropped into the recipient's chat when {from_name} recommends {song} to them.
    Warm, freshly phrased, attributed to the sender. An optional note is appended in their words."""
    who = (from_name or "someone").strip() or "someone"
    song = (song or "").strip()
    base = _pick("direct_rec", _DIRECT_REC_TEMPLATES).format(who=who, song=song)
    note = (note or "").strip()
    if note:
        base = base + f" ({who} adds: {note})"
    return base


def direct_recommendation_push_title() -> str:
    """A short, playful phone-notification title for an incoming recommendation."""
    return _pick("direct_rec_push_title", _DIRECT_REC_PUSH_TITLES)


def direct_recommendation_push_body(from_name: str, song: str) -> str:
    """A short phone-notification body naming the sender and the track."""
    who = (from_name or "someone").strip() or "someone"
    return f"{who} recommends '{(song or '').strip()}'. It is waiting in your private chat."
