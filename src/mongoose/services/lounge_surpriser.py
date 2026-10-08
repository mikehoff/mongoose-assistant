"""Guest-safe lounge surprises for the shared IRC room.

This is deliberately separate from the owner Surpriser. The lounge may be shared by house
visitors, the owner, and Mongoose itself, so every bit of context here is built only from
what is publicly visible in #mongoose or from guest profile fields the guest explicitly
allowed to be shared. It never reads the owner's email, calendar, files, private chat, home
state, or a guest's one-to-one thread.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable

from ..db import connect, get_app_state, set_app_state
from .memory import GROUP_CONVERSATION_ID

_LAST_LOUNGE_SURPRISE_KEY = "lounge_last_starter"
_LAST_REFLECTION_SIG_KEY = "lounge_last_reflection_signature"

# Words that mean "do not turn this into a playful recap". Silence is better than a
# delightful-seeming surprise over grief, self-harm, violence, panic, medical trouble,
# conflict, money/legal stress, or obviously private logistics.
_TRIGGERISH_RE = re.compile(
    r"\b("
    r"suicide|suicidal|self[-\s]?harm|kill myself|hurt myself|\bkill\b|murder|die|death|dead|grief|funeral|"
    r"trauma|abuse|assault|rape|violence|threat|unsafe|panic|breakdown|depressed|depression|"
    r"hospital|ambulance|cancer|diagnosis|blood|overdose|drugs?|addiction|"
    r"evict|eviction|debt|bailiff|court|tribunal|lawyer|solicitor|police|"
    r"fight|argument|angry|crying|upset|triggered"
    r")\b",
    re.IGNORECASE,
)
# SAFETY (external audit, additional findings #4): the English-only list let genuinely
# explicit Polish self-harm/crisis wording ("mam depresję i myśli samobójcze": "I have
# depression and suicidal thoughts") straight through and turned it into a whimsical group
# narrative prompt ("the depresję subplot"). Users may write in Polish, so
# this cannot be an edge case. Stems (\w* where sensible) deliberately biased toward catching
# more for the self-harm/violence/mental-health categories, since a missed crisis signal is
# far worse than an occasional over-cautious suppression of an innocuous Polish message.
_TRIGGERISH_PL_RE = re.compile(
    r"\b("
    r"samobój\w*|samookalecz\w*|zabi[jćc]\w*\s+si[eę]|skrzywdz\w*\s+si[eę]|"
    r"chc[eę]\s+umrze[cć]|nie\s+chc[eę]\s+ży[cć]|"
    r"depresj\w*|l[eę]k\w*|atak\w*\s+panik\w*|za[lł]aman\w*|trauma\w*|"
    r"przemoc\w*|napa[sś]\w*|gwa[lł]t\w*|gro[zź]b\w*|niebezpiecz\w*|"
    r"szpital\w*|karetk\w*|diagnoz\w*|przedawkowan\w*|uzale[zż]nien\w*|narkotyk\w*|"
    r"eksmisj\w*|komornik\w*|d[lł]ug\w*|sąd\w*|prawnik\w*|adwokat\w*|policj\w*|wyrok\w*|"
    r"k[lł][oó]tni\w*|w[sś]ciek\w*|p[lł]acz\w*|zestresowan\w*|zmartwion\w*"
    r")\b",
    re.IGNORECASE,
)
_PII_RE = re.compile(
    r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\b(?:\+?\d[\d\s().-]{8,}\d)\b|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b)",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(
    # Forgiving of typos and elongations, so a lone guest's "hellow"/"heyy"/"hiii" still gets
    # the instant, warm deterministic greeting instead of silence that reads as broken.
    r"^(h+i+|h+e+l+o+w?|h+e+y+a?|hiya|yo+|sup|wa?s+up|hej|czesc|cześć|siema|"
    r"dzień dobry|dobry wieczór)\b", re.I)
_ORIENT_RE = re.compile(r"\b(what is this|who are you|what are you|how does this room work|what is irc|what's irc|where am i)\b", re.I)
_GROUP_ADDRESS_RE = re.compile(r"\b(anyone|everyone|everybody|room|people)\b", re.I)
_ROOM_INVITE_RE = re.compile(r"\b(thoughts|opinions|vote|what do we think|help me choose)\b", re.I)

_STOPWORDS = {
    "about", "after", "again", "also", "because", "before", "being", "could", "didn", "does",
    "doing", "dont", "don't", "everyone", "from", "going", "have", "here", "just", "like",
    "maybe", "mongoose", "really", "right", "room", "should", "something", "that", "their",
    "there", "they", "thing", "think", "this", "though", "want", "what", "when", "where",
    "which", "with", "would", "youre", "you're", "your", "czesc", "cześć", "jest", "czy",
}

_TOPIC_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("the music thread", ("song", "album", "music", "playlist", "concert", "singer", "band", "vinyl", "track", "harbour"),
     ("what song gets the closing credits?", "one skip-free song each?")),
    ("the food subplot", ("food", "dinner", "tea", "coffee", "pizza", "pasta", "cake", "snack", "breakfast", "lunch", "cook", "restaurant"),
     ("what is the objectively correct snack for this room?", "sweet, salty, or chaotic?")),
    ("the animal subplot", ("dog", "cat", "puppy", "pet", "animal", "otter", "mongoose", "fox", "bird"),
     ("which animal would be the best house guest?", "which animal has this room's energy?")),
    ("the travel thread", ("travel", "trip", "holiday", "flight", "train", "porto", "beach", "hotel", "city", "country"),
     ("where are we sending the imaginary group chat holiday?", "window seat, aisle seat, or no-plane-ever?")),
    ("the film-and-TV corner", ("film", "movie", "show", "series", "episode", "netflix", "cinema", "actor"),
     ("what would the title of this room's sitcom be?", "one comfort-watch nomination?")),
    ("the games-and-quiz energy", ("game", "quiz", "trivia", "score", "win", "winner", "riddle", "guess"),
     ("team names, please?", "who is pretending not to be competitive?")),
    ("the house logistics thread", ("wifi", "wi-fi", "password", "light", "socket", "charger", "remote", "tablet", "speaker", "heating"),
     ("what is the one tiny house convenience everyone secretly wants?", "what needs a label before someone loses patience?")),
    ("the Polish cameo", ("polish", "poland", "polska", "warsaw", "krakow", "cześć", "czesc", "hej", "dzień"),
     ("what Polish word should everyone learn first?", "which phrase deserves a dramatic group reading?")),
]

_NEUTRAL_STARTERS = (
    "small first-round question: what object in the room has the most personality?",
    "tiny table question: what is the most underrated household luxury?",
    "icebreaker with low stakes: what snack would improve this room by 12%?",
    "one-liner prompt: what song should play when someone enters this lounge?",
    "room vote: best animal to appoint as honorary bouncer?",
    "micro-quiz: what is a useless fact you carry around proudly?",
)

_ROLE_LINE = (
    "I’m the little house sidekick in this room: useful when invited, quiet when the humans have the floor."
)


def _safe_name(value: Any, fallback: str = "someone") -> str:
    cleaned = "".join(ch for ch in str(value or "") if ch == " " or (ord(ch) >= 32 and ord(ch) != 127))
    cleaned = " ".join(cleaned.replace(":", "").split()).strip()
    return (cleaned or fallback)[:40]


def _safe_text(value: Any, limit: int = 280) -> str:
    cleaned = "".join(ch for ch in str(value or "") if ch in "\n\r\t" or (ord(ch) >= 32 and ord(ch) != 127))
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:limit]


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _old_enough(value: Any, minutes: float, *, now: datetime | None = None) -> bool:
    dt = _parse_iso(value)
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - dt) >= timedelta(minutes=minutes)


def _ensure_tables(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS lounge_message_meta (
            message_id INTEGER PRIMARY KEY,
            device_id TEXT,
            sender TEXT,
            kind TEXT NOT NULL DEFAULT 'human',
            created_at TEXT NOT NULL,
            FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_lounge_meta_device ON lounge_message_meta(device_id, message_id)")




def ensure_tables(con) -> None:
    """Public alias for callers that need the lounge metadata schema before a query."""
    _ensure_tables(con)


def ensure_lounge_conversation(con) -> None:
    con.execute(
        "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        (GROUP_CONVERSATION_ID, "the lounge"),
    )


def store_human_message(*, device_id: str, sender: str, text: str, line: str | None = None) -> int:
    """Store a human lounge line and attach device metadata for exact own-message flags.

    `messages.content` remains the legacy, readable "Name: text" form. The side table stores
    device identity, so two guests both called Alex no longer see each other's lines as "mine".
    """
    sender = _safe_name(sender)
    text = _safe_text(text, 2000)
    line = _safe_text(line or f"{sender}: {text}", 2100)
    with connect() as con:
        _ensure_tables(con)
        ensure_lounge_conversation(con)
        cur = con.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,datetime('now'))",
            (GROUP_CONVERSATION_ID, "user", line),
        )
        mid = int(cur.lastrowid)
        con.execute(
            "INSERT OR REPLACE INTO lounge_message_meta(message_id, device_id, sender, kind, created_at) "
            "VALUES (?,?,?,?,datetime('now'))",
            (mid, device_id, sender, "human"),
        )
        con.execute("UPDATE conversations SET updated_at=datetime('now') WHERE id=?", (GROUP_CONVERSATION_ID,))
        con.commit()
        return mid


def mark_latest_human_message(*, device_id: str, sender: str, line: str) -> int | None:
    """Attach metadata to the newest matching legacy message inserted by chat_with_memory."""
    sender = _safe_name(sender)
    line = _safe_text(line, 2100)
    with connect() as con:
        _ensure_tables(con)
        row = con.execute(
            "SELECT id FROM messages WHERE conversation_id=? AND role='user' AND content=? "
            "ORDER BY id DESC LIMIT 1",
            (GROUP_CONVERSATION_ID, line),
        ).fetchone()
        if row is None:
            return None
        mid = int(row["id"])
        con.execute(
            "INSERT OR REPLACE INTO lounge_message_meta(message_id, device_id, sender, kind, created_at) "
            "VALUES (?,?,?,?,datetime('now'))",
            (mid, device_id, sender, "human"),
        )
        con.commit()
        return mid


def post_mongoose_line(text: str, *, kind: str = "surprise") -> int:
    text = _safe_text(text, 900)
    if not text:
        return 0
    with connect() as con:
        _ensure_tables(con)
        ensure_lounge_conversation(con)
        cur = con.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,datetime('now'))",
            (GROUP_CONVERSATION_ID, "assistant", text),
        )
        mid = int(cur.lastrowid)
        con.execute(
            "INSERT OR REPLACE INTO lounge_message_meta(message_id, device_id, sender, kind, created_at) "
            "VALUES (?,?,?,?,datetime('now'))",
            (mid, "mongoose", "mongoose", kind),
        )
        con.execute("UPDATE conversations SET updated_at=datetime('now') WHERE id=?", (GROUP_CONVERSATION_ID,))
        con.commit()
        return mid


def note_guest_joined(*, device_id: str, public_name: str, mongoose_name: str = "",
                      fun_fact: str = "", announce_join: bool = False,
                      share_fun_fact: bool = False) -> dict[str, Any]:
    """Add a join beat to #mongoose (default on; claim-form opt-out respected).

    If the guest opted out of being announced, nothing public is posted. If they allowed a fun
    fact, it is quoted and attributed to them (never presented as Mongoose's own words). This
    gives the room continuity without exposing hidden invite labels or private threads.
    """
    if not announce_join:
        # Explicit opt-out only: someone who unticked the box slips in quietly.
        return {"posted": False, "reason": "guest opted out of the join announcement"}
    device_id = str(device_id or "")
    if not device_id:
        return {"posted": False, "reason": "missing device"}
    key = f"lounge_join_announced:{device_id}"
    if get_app_state(key):
        return {"posted": False, "reason": "already announced"}
    name = _safe_name(public_name or mongoose_name, "a guest")
    text = f"{name} has joined #mongoose."
    if share_fun_fact and _safe_text(fun_fact):
        # Attribute the fun fact to the joiner and quote it, so it never reads as Mongoose's
        # own words. Mongoose's "I'm the sidekick" self-intro is said at room-open (see
        # _ROLE_LINE callers), not glued onto every join, which was what made this one line
        # read as three tangled voices (announcement + guest's fact + Mongoose's self-intro).
        text += f' {name}\'s fun fact: "{_safe_text(fun_fact, 180)}".'
    mid = post_mongoose_line(text, kind="join")
    set_app_state(key, datetime.now(timezone.utc).isoformat())
    return {"posted": bool(mid), "message_id": mid, "text": text}


def _rows(limit: int = 80) -> list[dict[str, Any]]:
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT m.id, m.role, m.content, m.created_at, lm.device_id, lm.sender AS meta_sender, lm.kind "
            "FROM messages m LEFT JOIN lounge_message_meta lm ON lm.message_id=m.id "
            "WHERE m.conversation_id=? ORDER BY m.id DESC LIMIT ?",
            (GROUP_CONVERSATION_ID, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _parse_human(row: dict[str, Any]) -> tuple[str, str]:
    content = str(row.get("content") or "")
    sender, sep, rest = content.partition(": ")
    if sep:
        return _safe_name(row.get("meta_sender") or sender), rest
    return _safe_name(row.get("meta_sender") or "someone"), content


def _public_guest_profiles() -> list[dict[str, str]]:
    """Consent-respecting public directory for group context."""
    try:
        with connect() as con:
            rows = con.execute(
                "SELECT gp.public_name, gp.fun_fact, gp.share_fun_fact "
                "FROM guest_profiles gp JOIN paired_devices pd ON pd.device_id=gp.device_id "
                "WHERE pd.revoked=0 ORDER BY gp.created_at ASC LIMIT 50"
            ).fetchall()
        out = []
        for r in rows:
            item = {"name": _safe_name(r["public_name"])}
            if r["share_fun_fact"] and _safe_text(r["fun_fact"]):
                item["fun_fact"] = _safe_text(r["fun_fact"], 140)
            out.append(item)
        return out
    except Exception:
        return []


def _has_unclassifiable_script(text: str) -> bool:
    """Backstop beyond the EN+PL keyword lists (external audit #4's second recommendation):
    a guest writing in a script neither lexicon can ever cover (Cyrillic, CJK, Arabic, ...)
    has content the safety layer cannot confidently classify at all. Latin-script languages
    (English, Polish, and anything else using the same alphabet) are NOT caught by this: they
    get real keyword coverage instead, so ordinary Polish chit-chat keeps being reflect-able
    rather than blanket-suppressed just for using diacritics."""
    import unicodedata
    for ch in text:
        if ch.isalpha():
            try:
                name = unicodedata.name(ch)
            except ValueError:
                continue
            if not (name.startswith("LATIN") or name.startswith("DIGIT")):
                return True
    return False


def _safe_to_reflect(texts: Iterable[str]) -> bool:
    blob = "\n".join(str(t or "") for t in texts)[-3000:]
    if not blob.strip():
        return True
    if _TRIGGERISH_RE.search(blob) or _TRIGGERISH_PL_RE.search(blob) or _PII_RE.search(blob):
        return False
    if _has_unclassifiable_script(blob):
        return False
    return True


def _topic_rules() -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """_TOPIC_RULES with any keyword list the site profile replaces (lexicon lounge.topic_keywords,
    {topic label: [keywords]}), so a household's own names and in-jokes steer the topic pick."""
    from .. import profile as _profile
    over = _profile.lexicon("lounge.topic_keywords", {}) or {}
    if not over:
        return _TOPIC_RULES
    return [(label, tuple(over[label]) if label in over else words, prompts)
            for label, words, prompts in _TOPIC_RULES]


def _topic_from(texts: Iterable[str]) -> tuple[str, str]:
    blob = " \n ".join(str(t or "").lower() for t in texts)
    best: tuple[int, str, tuple[str, ...]] | None = None
    for label, words, prompts in _topic_rules():
        score = sum(blob.count(w.lower()) for w in words)
        if score and (best is None or score > best[0]):
            best = (score, label, prompts)
    if best:
        return best[1], random.choice(best[2])
    words = re.findall(r"[a-zA-ZÀ-ž][a-zA-ZÀ-ž'’-]{3,}", blob)
    candidates = [w.strip("'’").lower() for w in words if w.lower() not in _STOPWORDS]
    if candidates:
        # Deterministic-ish top word without making every run identical.
        counts: dict[str, int] = {}
        for w in candidates:
            counts[w] = counts.get(w, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return f"the {top} subplot", "what is the next tiny twist in that story?"
    return "the just-arrived energy", random.choice(_NEUTRAL_STARTERS)


def _names_text(names: list[str]) -> str:
    names = [_safe_name(n) for n in names if _safe_name(n)]
    seen: list[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    if not seen:
        return "the room"
    if len(seen) == 1:
        return seen[0]
    if len(seen) == 2:
        return f"{seen[0]} and {seen[1]}"
    return ", ".join(seen[:2]) + f" and {seen[2]}" + ("" if len(seen) <= 3 else " and co.")


def lounge_max_id() -> int:
    """The highest message id in the #mongoose room. A client that rendered a line optimistically
    can set its poll cursor to this so the next incremental poll skips its own echo (and the
    reply it already showed), so nothing is duplicated."""
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT MAX(id) FROM messages WHERE conversation_id=?",
                          (GROUP_CONVERSATION_ID,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0


def lounge_snapshot(limit: int = 80) -> dict[str, Any]:
    rows = _rows(limit)
    humans: list[dict[str, Any]] = []
    assistant: list[dict[str, Any]] = []
    for r in rows:
        if r.get("role") == "assistant":
            assistant.append(r)
            continue
        sender, text = _parse_human(r)
        humans.append({**r, "sender": sender, "text": text})
    recent_humans = humans[-12:]
    recent_texts = [h["text"] for h in recent_humans]
    recent_speakers = []
    for h in recent_humans:
        if h["sender"] not in recent_speakers:
            recent_speakers.append(h["sender"])
    profiles = _public_guest_profiles()
    visible_names = [p["name"] for p in profiles]
    for n in recent_speakers:
        if n not in visible_names:
            visible_names.append(n)
    topic, next_prompt = _topic_from(recent_texts)
    latest = rows[-1] if rows else None
    return {
        "rows": rows,
        "humans": humans,
        "assistant": assistant,
        "recent_humans": recent_humans,
        "recent_speakers": recent_speakers,
        "visible_names": visible_names[:8],
        "profiles": profiles[:8],
        "topic": topic,
        "next_prompt": next_prompt,
        "safe_to_reflect": _safe_to_reflect(recent_texts),
        "latest": latest,
    }


def _reflection_signature(snap: dict[str, Any]) -> str:
    ids = [str(h.get("id")) for h in snap.get("recent_humans", [])[-6:]]
    base = "|".join(ids) + "|" + str(snap.get("topic") or "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def _model_lounge_line(snap: dict[str, Any]) -> str | None:
    """Let the MODEL phrase the lounge line from the real, publicly-visible room facts (the owner's always-think
    principle), so the icebreaker is fresh and in Mongoose's voice instead of a fixed template. Returns None
    (caller keeps the deterministic template) if no local model is reachable or we're inside a running event
    loop. Best-effort, never blocks or raises. Uses ONLY guest-safe snapshot fields, same as the templates."""
    try:
        import asyncio
        from ..llm import ask_persona
        try:
            asyncio.get_running_loop()
            return None  # already in an event loop; keep the template rather than nest asyncio.run
        except RuntimeError:
            pass
        names_txt = _names_text(snap.get("visible_names") or snap.get("recent_speakers") or [])
        topic = str(snap.get("topic") or "the room")
        profiles = snap.get("profiles") or []
        fun_bits = [f"{p['name']} ({p['fun_fact']})" for p in profiles if p.get("fun_fact")]
        seed = str(snap.get("next_prompt") or random.choice(_NEUTRAL_STARTERS))
        facts = (f"People visibly in the lounge now: {names_txt or 'nobody yet'}. "
                 f"Current shared topic: {topic}. "
                 f"Public calling cards (the ONLY personal facts you may use): "
                 f"{'; '.join(fun_bits) if fun_bits else 'none'}. "
                 f"A light question you may reuse or improve on: {seed}.")
        expectation = (
            "You are Mongoose, the low-key house sidekick in a shared lounge chat room. Write ONE short, warm "
            "line to gently open or continue the room: greet whoever is here by name, nod to the topic if "
            "there is one, and pose a light group question. One or two sentences, in your own voice, useful "
            "when invited and quiet otherwise. Never invent a fact about a person beyond the public calling "
            "cards given, and never mention the owner's private data.")
        out = asyncio.run(ask_persona(f"{expectation}\n\nRoom facts (use ONLY these): {facts}", role="chat"))
        return (out or "").strip() or None
    except Exception:
        return None


def compose_lounge_surprise(snap: dict[str, Any]) -> str | None:
    """Return one safe, group-aware line, or None if silence is better. Model-first (grounded on the room
    snapshot); the deterministic templates below are the fallback when the model is unreachable."""
    if not snap.get("safe_to_reflect", True):
        return None
    humans = snap.get("humans") or []
    names = snap.get("visible_names") or snap.get("recent_speakers") or []
    names_txt = _names_text(names)
    topic = str(snap.get("topic") or "the room")
    topic_phrase = topic[4:] if topic.lower().startswith("the ") else topic
    prompt = str(snap.get("next_prompt") or random.choice(_NEUTRAL_STARTERS))
    profiles = snap.get("profiles") or []
    fun_bits = [f"{p['name']} ({p['fun_fact']})" for p in profiles if p.get("fun_fact")]

    if not humans:
        if names and fun_bits:
            fallback = f"{names_txt}, the lounge is open. I know the public calling cards only: {', '.join(fun_bits[:2])}. {prompt}"
        elif names:
            fallback = f"{names_txt}, the lounge is open. {_ROLE_LINE} {prompt}"
        else:
            fallback = f"The lounge is open. {_ROLE_LINE} {random.choice(_NEUTRAL_STARTERS)}"
    elif len(humans) <= 2:
        fallback = f"Room note: {names_txt}, you have the first little {topic_phrase} going. {_ROLE_LINE} {prompt}"
    else:
        templates = [
            f"Previously in #mongoose: {names_txt} have started {topic}. Tiny next beat: {prompt}",
            f"Micro-recap: {topic} is now the lounge subplot. {names_txt}, {prompt}",
            f"Room continuity note: {names_txt} are giving this place a proper little table. I'll be the sidekick, not the chairperson. {prompt}",
        ]
        fallback = random.choice(templates)

    return _model_lounge_line(snap) or fallback


def maybe_lounge_surprise() -> dict[str, Any]:
    """Maybe post one asynchronous, guest-safe lounge starter/reflection.

    Called from the history poll path, so it feels asynchronous to guests without needing a
    background worker. It is quiet by default: cooldown, no two Mongoose lines in a row,
    no interrupting active chat, and no playful reflections over triggering/private content.
    """
    if (os.environ.get("MONGOOSE_LOUNGE_STARTERS") or "").strip().lower() in {"off", "false", "0"}:
        return {"posted": False, "reason": "disabled"}
    now = datetime.now(timezone.utc)
    try:
        quiet_min = float(os.environ.get("MONGOOSE_LOUNGE_QUIET_MINUTES") or 20)
        cooldown_min = float(os.environ.get("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES") or 90)
    except Exception:
        quiet_min, cooldown_min = 20.0, 90.0
    if not _old_enough(get_app_state(_LAST_LOUNGE_SURPRISE_KEY), cooldown_min, now=now):
        return {"posted": False, "reason": "cooldown"}
    snap = lounge_snapshot()
    latest = snap.get("latest")
    if latest is not None:
        if latest.get("role") == "assistant":
            return {"posted": False, "reason": "mongoose already spoke last"}
        if not _old_enough(latest.get("created_at"), quiet_min, now=now):
            return {"posted": False, "reason": "room active"}
    if not snap.get("safe_to_reflect", True):
        return {"posted": False, "reason": "sensitive recent context"}
    sig = _reflection_signature(snap)
    if get_app_state(_LAST_REFLECTION_SIG_KEY) == sig:
        return {"posted": False, "reason": "same reflection already used"}
    text = compose_lounge_surprise(snap)
    if not text:
        return {"posted": False, "reason": "nothing safe to say"}
    mid = post_mongoose_line(text, kind="reflection" if snap.get("humans") else "starter")
    set_app_state(_LAST_LOUNGE_SURPRISE_KEY, now.isoformat())
    set_app_state(_LAST_REFLECTION_SIG_KEY, sig)
    return {"posted": True, "message_id": mid, "text": text, "signature": sig}


def prior_human_count_for_device(device_id: str) -> int:
    with connect() as con:
        _ensure_tables(con)
        row = con.execute(
            "SELECT COUNT(*) AS n FROM lounge_message_meta lm JOIN messages m ON m.id=lm.message_id "
            "WHERE m.conversation_id=? AND lm.device_id=? AND lm.kind='human'",
            (GROUP_CONVERSATION_ID, str(device_id or "")),
        ).fetchone()
    return int(row["n"] if row else 0)


def recently_active_other_devices(device_id: str, within_seconds: int) -> list[str]:
    """The activity tracker: OTHER human participants who posted a lounge line within the last
    `within_seconds`. Presence is by recent MESSAGING, not membership, so a friend who joined
    last week but is idle does not count as company."""
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT DISTINCT lm.device_id FROM lounge_message_meta lm "
            "WHERE lm.kind='human' AND lm.device_id IS NOT NULL AND lm.device_id != ? "
            "AND lm.created_at >= datetime('now', ?)",
            (str(device_id or ""), f"-{int(within_seconds)} seconds"),
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def _joined_recently(device_id: str, within_seconds: int = 600) -> bool:
    """True when this device is a FRESH arrival: its first lounge line was within the last
    `within_seconds` (default 10 min), or it has never posted before (this is its first line)."""
    with connect() as con:
        _ensure_tables(con)
        row = con.execute(
            "SELECT COUNT(*) AS n, (MIN(lm.created_at) >= datetime('now', ?)) AS recent "
            "FROM lounge_message_meta lm WHERE lm.kind='human' AND lm.device_id=?",
            (f"-{int(within_seconds)} seconds", str(device_id or "")),
        ).fetchone()
    if not row or not row["n"]:
        return True
    return bool(row["recent"])


def is_messaging_alone(device_id: str) -> bool:
    """True when no OTHER participant has messaged within the window that applies to this person.
    A fresh arrival (new joiner) counts as alone after just 1 minute of others' silence, because
    a newcomer who says something and hears nothing gives up fast; an established participant gets
    4 minutes before Mongoose steps in. Owner or guest, any recently-active human counts as
    company. When alone, the 'public' room is really a one-to-one, so Mongoose engages."""
    if not device_id:
        return False
    window = 60 if _joined_recently(device_id) else 240
    return not recently_active_other_devices(device_id, window)


def deterministic_reply_for_turn(*, text: str, sender: str, device_id: str) -> str | None:
    """Small no-model replies for orientation/new-arrival moments.

    These create more human-feeling group dynamics even when the local model is down, while
    staying safe and not over-participating.
    """
    text = _safe_text(text, 2000)
    sender = _safe_name(sender)
    if not text:
        return None
    if not _safe_to_reflect([text]):
        return None
    snap = lounge_snapshot()
    others = [n for n in (snap.get("visible_names") or []) if n != sender]
    others_txt = _names_text(others) if others else "whoever else drops in"
    prior_count = prior_human_count_for_device(device_id)
    if _ORIENT_RE.search(text):
        return f"This is #mongoose: a shared little lounge for the people paired with this Mongoose. {_ROLE_LINE}"
    words = re.findall(r"[A-Za-zÀ-ž']+", text)
    if prior_count == 0 and _GREETING_RE.search(text) and len(words) <= 2 and not _GROUP_ADDRESS_RE.search(text):
        return f"Hi {sender}. You’re in #mongoose with {others_txt}. {_ROLE_LINE}"
    return None


def should_model_reply_to(*, text: str, sender: str, device_id: str) -> bool:
    """Broaden model participation a little without turning Mongoose into a reply bot."""
    text = _safe_text(text, 2000)
    if not text or not _safe_to_reflect([text]):
        return False
    if text.rstrip().endswith("?"):
        return True
    if _ORIENT_RE.search(text):
        return True
    if _ROOM_INVITE_RE.search(text) and not _GREETING_RE.search(text):
        return True
    # Alone in the room? Then this "public" lounge is really a one-to-one, so engage on any real
    # message rather than leave a lone person talking to a void. is_messaging_alone uses the
    # activity tracker: nobody else has messaged within the applicable window (1 min for a fresh
    # arrival, 4 min for an established participant).
    try:
        if is_messaging_alone(device_id):
            return True
    except Exception:
        pass
    # A first greeting gets a deterministic reply, not a model call.
    return False


def group_system_prompt(base_rules: str) -> str:
    """Augment the IRC model prompt with only public room context."""
    snap = lounge_snapshot(limit=60)
    people = _names_text(snap.get("visible_names") or snap.get("recent_speakers") or [])
    topic = snap.get("topic") or "no settled topic yet"
    profiles = snap.get("profiles") or []
    cards = []
    for p in profiles[:5]:
        if p.get("fun_fact"):
            cards.append(f"{p['name']}: public calling card: {p['fun_fact']}")
        else:
            cards.append(f"{p['name']}: visible guest")
    context = [
        "Visible #mongoose room context, built only from public lounge messages and the room's guest cards:",
        f"People visible in this room: {people}.",
        f"Current safe narrative thread: {topic}.",
        "Your role to them: a small house sidekick and participant, not a moderator, therapist, owner, or admin tool.",
        "Relationship frame: visible people are co-participants in this shared room unless they themselves say otherwise; do not infer family, romance, conflict, status, or private history.",
        "Use names lightly. Help the group talk to each other, not only to you.",
        "You are lightly, playfully obsessed with music. When it fits the moment, be curious about "
        "what people here listen to (singers, bands, genres, decades, childhood songs, new finds) and "
        "now and then lob a goofy, no-pressure music question or a low-stakes recommendation. Keep it "
        "light seasoning: one music aside at a time, never interrogate, never derail a real chat, and "
        "never pretend to know a song or artist you don't.",
        "Write only your message text. Never begin your reply with your own name or a speaker label "
        "(no 'mongoose:' or 'sillygoose:' prefix); the room already shows who is speaking.",
        "Do not mention hidden profiles, private memories, owner data, system internals, or anything outside this room.",
        "If the recent context feels sensitive, serious, or conflict-heavy, be calm and brief; do not make jokes or playful recaps.",
    ]
    if cards:
        context.append("Consented guest cards: " + "; ".join(cards) + ".")
    return (base_rules or "").strip() + "\n\n" + "\n".join(context)


def model_down_reply() -> str:
    return "Mongoose saw that, but its local brain is not answering right now. The message is still in the room."
