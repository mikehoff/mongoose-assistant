from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from ..db import audit, connect, utc_now_iso

# The ONE permanent conversation. Mongoose is a single resident companion, not disconnected chat
# surfaces: text chat in the cockpit, browser/CLI voice, the CLI `mongoose chat`, and Home Assistant
# Assist all default to this same thread, so a thing said by voice is remembered when you next open
# the Chat tab, and the phone, terminal and HA all continue the same conversation. Explicit ids still
# work when a caller wants an isolated thread, but the product default is continuity.
DEFAULT_CONVERSATION_ID = "mongoose-main"

# Memory scope. Owner facts live in the "owner" scope and are shared across every owner surface
# (cockpit, voice, CLI, HA). A trusted-guest conversation gets its OWN scope derived from its
# conversation id, so the owner's facts never surface in a guest chat and a guest's facts never
# pollute the owner brain. Isolation is a property of the storage layer, not of any one caller.
OWNER_SCOPE = "owner"


def scope_for_conversation(conversation_id: str | None) -> str:
    """Derive the memory scope from a conversation id.

    'guest:<device_id>' conversations get an isolated scope keyed on the device, so two different
    guests also can't see each other's facts. Everything else is the shared owner scope.
    This is the single source of truth for scope; never hand-build a scope string elsewhere.
    """
    cid = conversation_id or ""
    if cid.startswith("guest:"):
        return cid  # the full 'guest:<device_id>' string is the scope: unique per guest device
    if cid.startswith("group:"):
        # Shared guest/owner rooms get their own group scope. This prevents a guest saying
        # "remember..." in #mongoose from polluting the owner's private memory, while still
        # allowing the room to have its own short-term narrative.
        return cid
    return OWNER_SCOPE

# Back-compat alias: the CLI used to have its own thread. It now points at the one permanent
# conversation so the terminal shares the same context as every other surface.
DEFAULT_CLI_CONVERSATION = DEFAULT_CONVERSATION_ID

# How many recent messages to replay into the model as short-term context.
# This is a COUNT cap (a safety ceiling). The real bound is the TOKEN budget below: history
# is filled newest-first until either the count or the token budget is hit, whichever comes
# first. The token bound is what actually protects the model's context window , a few long
# turns can blow a count-only window and silently truncate the system prompt.
DEFAULT_HISTORY_LIMIT = 12

# The group lounge (the phone IRC tab): ONE room shared by the owner, every guest, and
# mongoose itself as a participant. Its own thread, distinct from the owner's private brain
# (DEFAULT_CONVERSATION_ID) and from each guest's private thread (guest:p:<name>). The
# "group:" namespace is recognised by the doctor census as expected.
GROUP_CONVERSATION_ID = "group:irc"

# Token budget for replayed history. Sized to leave room, within a typical local context
# window, for the system prompt + injected facts + the new prompt + the model's reply.
# Tunable via MEMORY_HISTORY_TOKEN_BUDGET. Conservative by default because the base M4 runs
# small contexts and Ollama silently truncates from the front on overflow.
DEFAULT_HISTORY_TOKEN_BUDGET = 1800


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate. ~4 chars/token is the rule of thumb for
    English; we round UP and add a small per-message overhead so we under-fill rather than
    overflow (truncation is worse than leaving a little headroom)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)



# --- Short-term: conversation history ---------------------------------------

def ensure_conversation(conversation_id: str | None = None, title: str | None = None) -> str:
    cid = conversation_id or uuid.uuid4().hex
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
            (cid, title, now, now),
        )
    return cid


def add_message(conversation_id: str, role: str, content: str) -> None:
    if not content:
        return
    now = utc_now_iso()
    # Attribution rides a ContextVar set by the device route around the whole chat turn, so
    # all 18 user-write sites in llm.py inherit it without threading kwargs through each.
    # Only user-authored text is attributed: the privacy question is who SAID what.
    sender = _SENDER_ATTRIBUTION.get() if role == "user" else None
    dev_id, dev_label = (sender or (None, None))
    with connect() as con:
        con.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at, "
            "sender_device_id, sender_label) VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, now, dev_id, dev_label),
        )
        con.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))


import contextvars

_SENDER_ATTRIBUTION: contextvars.ContextVar = contextvars.ContextVar(
    "mongoose_sender", default=None)


class sender_attribution:
    """Context manager: while active, user messages persisted by add_message record which
    device authored them. Async-safe (ContextVar), so concurrent chats from different
    devices cannot cross-attribute."""

    def __init__(self, device_id: str, label: str = ""):
        self._val = (str(device_id or ""), str(label or ""))
        self._tok = None

    def __enter__(self):
        self._tok = _SENDER_ATTRIBUTION.set(self._val if self._val[0] else None)
        return self

    def __exit__(self, *exc):
        _SENDER_ATTRIBUTION.reset(self._tok)
        return False


def quarantine_device_messages(device_id: str) -> dict:
    """Move a device's attributed messages OUT of the shared main conversation into
    quarantine:<device_id>. Called on revoke: revoking access must also stop the revoked
    device's words sitting invisibly inside the owner's brain thread. Only attributed rows
    can move; rows from before attribution existed stay put (rollover is the remedy there).
    Nothing is deleted: quarantine preserves evidence and allows restore."""
    if not device_id:
        return {"ok": False, "error": "device_id required"}
    qid = f"quarantine:{device_id}"
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
            (qid, f"Quarantined messages from device {device_id}", now, now))
        cur = con.execute(
            "UPDATE messages SET conversation_id = ? WHERE sender_device_id = ? AND conversation_id = ?",
            (qid, device_id, DEFAULT_CONVERSATION_ID))
        moved = cur.rowcount
        con.commit()
    return {"ok": True, "moved": moved, "quarantine_conversation": qid}


def rollover_main_conversation(reason: str = "") -> dict:
    """Archive the ENTIRE current main conversation under a dated id and start the shared
    brain thread fresh. The remedy for contamination by unattributed history (a mis-paired
    device's messages from before attribution existed cannot be selected individually).
    Nothing is deleted: the archive keeps every message under the new id."""
    stamp = utc_now_iso().replace(":", "").replace("-", "")[:13]
    archive_id = f"main-archive-{stamp}"
    now = utc_now_iso()
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                        (DEFAULT_CONVERSATION_ID,)).fetchone()[0]
        if n == 0:
            return {"ok": True, "moved": 0, "archive": None,
                    "detail": "main conversation is already empty"}
        con.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
            (archive_id, (f"Archived main conversation ({reason})" if reason
                          else "Archived main conversation"), now, now))
        con.execute("UPDATE messages SET conversation_id = ? WHERE conversation_id = ?",
                    (archive_id, DEFAULT_CONVERSATION_ID))
        con.commit()
    return {"ok": True, "moved": n, "archive": archive_id}


def recent_messages(conversation_id: str, limit: int = DEFAULT_HISTORY_LIMIT,
                    *, token_budget: int | None = DEFAULT_HISTORY_TOKEN_BUDGET
                    ) -> list[dict[str, str]]:
    """Recent conversation history for replay, bounded by BOTH a message count (ceiling)
    and a token budget (the real bound). We pull up to `limit` newest messages, then walk
    them newest-first and keep only as many as fit in `token_budget`, so a few long turns
    can't silently overflow the model's context window. Returns oldest-first for replay.
    Pass token_budget=None to disable the token bound (count-only)."""
    with connect() as con:
        rows = con.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
    # rows are newest-first; keep within the token budget walking from newest backward.
    kept: list[dict[str, str]] = []
    used = 0
    for r in rows:
        content = r["content"] or ""
        if token_budget is not None:
            cost = estimate_tokens(content)
            # always keep at least the single most recent message, even if it alone is huge,
            # so the model still sees the immediate prior turn.
            if kept and used + cost > token_budget:
                break
            used += cost
        kept.append({"role": r["role"], "content": content})
    # we appended newest-first; reverse to oldest-first for replay into the model.
    return list(reversed(kept))


def clear_conversation(conversation_id: str) -> dict[str, Any]:
    with connect() as con:
        cur = con.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        deleted = cur.rowcount
    audit("memory", "clear_conversation", True, payload={"conversation_id": conversation_id, "deleted": deleted})
    return {"conversation_id": conversation_id, "deleted_messages": deleted}


# --- Long-term: durable facts -----------------------------------------------

def _dedupe_key(fact: str) -> str:
    return hashlib.sha256(fact.strip().lower().encode("utf-8")).hexdigest()[:32]


def _is_low_quality_fact(fact: str) -> bool:
    """Reject degenerate auto-extracted facts that would pollute memory: too short, no real subject,
    or a bare pronoun fragment like 'likes him.' (no subject) or 'he does.'. Conservative: only
    filters clearly useless facts, never plausibly-real ones."""
    f = fact.strip().rstrip(".!?").strip().lower()
    if len(f) < 6:  # 'likes' etc.: too little to be a durable fact
        return True
    words = f.split()
    if len(words) < 2:
        return True
    # A fact that STARTS with a verb or a dangling pronoun object has no subject: "likes him",
    # "is nice", "does it", "wants that". Real facts name who/what: "Alex likes X", "the user...".
    _SUBJECTLESS_OPENERS = {
        "likes", "loves", "hates", "wants", "needs", "is", "are", "was", "were", "has", "have",
        "does", "did", "will", "would", "said", "says", "thinks", "feels", "knows", "him", "her",
        "them", "it", "that", "this", "really", "very",
    }
    if words[0] in _SUBJECTLESS_OPENERS:
        return True
    return False


def remember_fact(fact: str, source: str = "explicit", *, scope: str = OWNER_SCOPE) -> dict[str, Any]:
    fact = fact.strip()
    if not fact:
        return {"stored": False, "reason": "empty"}
    # Quality gate for auto/model-proposed facts: a degenerate fragment ("likes him.") pollutes
    # memory and degrades later context. Explicit user-stated facts ("remember that ...") bypass this
    # since the user vouched for them.
    if source != "explicit" and _is_low_quality_fact(fact):
        return {"stored": False, "reason": "low_quality", "fact": fact}
    # Scope is part of the dedupe key so an owner fact and a guest fact with identical text
    # are distinct rows in distinct scopes (and a guest can't overwrite an owner fact).
    key = _dedupe_key(f"{scope}\x1f{fact}")
    now = utc_now_iso()
    with connect() as con:
        existing = con.execute("SELECT id FROM memory_facts WHERE dedupe_key = ?", (key,)).fetchone()
        if existing is not None:
            # Exact duplicate: just bump updated_at, do NOT add another FTS row.
            con.execute("UPDATE memory_facts SET updated_at = ? WHERE id = ?", (now, existing["id"]))
            return {"stored": False, "reason": "duplicate", "fact": fact}
        cur = con.execute(
            "INSERT INTO memory_facts(fact, source, dedupe_key, scope, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (fact, source, key, scope, now, now),
        )
        fact_id = cur.fetchone()["id"]
        con.execute("INSERT INTO memory_fts(rowid, fact) VALUES (?, ?)", (fact_id, fact))
    audit("memory", "remember_fact", True, payload={"source": source, "scope": scope})
    return {"stored": True, "fact": fact, "source": source}


def search_facts(query: str, limit: int = 5, *, scope: str = OWNER_SCOPE) -> list[str]:
    import sqlite3
    from ..db import safe_fts_query
    query = (query or "").strip()
    if not query:
        return []
    match = safe_fts_query(query)
    if match is None:
        return []
    try:
        with connect() as con:
            rows = con.execute(
                "SELECT f.fact FROM memory_fts x JOIN memory_facts f ON f.id = x.rowid "
                "WHERE memory_fts MATCH ? AND f.scope = ? ORDER BY rank LIMIT ?",
                (match, scope, limit),
            ).fetchall()
        return [r["fact"] for r in rows]
    except sqlite3.OperationalError:
        # Only swallow FTS syntax/query errors; let real DB errors propagate so
        # corruption or schema bugs aren't silently hidden.
        return []


def all_facts(limit: int = 100, *, scope: str = OWNER_SCOPE) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT fact, source, updated_at FROM memory_facts WHERE scope = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def forget_fact(fact: str, *, scope: str = OWNER_SCOPE) -> dict[str, Any]:
    key = _dedupe_key(f"{scope}\x1f{fact}")
    with connect() as con:
        row = con.execute("SELECT id, fact FROM memory_facts WHERE dedupe_key = ?", (key,)).fetchone()
        if not row:
            return {"forgotten": False, "reason": "not found"}
        con.execute("INSERT INTO memory_fts(memory_fts, rowid, fact) VALUES('delete', ?, ?)", (row["id"], row["fact"]))
        con.execute("DELETE FROM memory_facts WHERE id = ?", (row["id"],))
    audit("memory", "forget_fact", True)
    return {"forgotten": True}


def _delete_fact_by_id(fact_id: int) -> bool:
    with connect() as con:
        row = con.execute("SELECT id, fact FROM memory_facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            return False
        con.execute("INSERT INTO memory_fts(memory_fts, rowid, fact) VALUES('delete', ?, ?)", (row["id"], row["fact"]))
        con.execute("DELETE FROM memory_facts WHERE id = ?", (row["id"],))
    audit("memory", "forget_fact", True)
    return True


_STOPWORDS = {"the", "a", "an", "user", "i", "is", "are", "was", "of", "on", "in", "to", "and",
              "my", "that", "it", "likes", "dislikes", "like", "dislike", "fond", "really"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOPWORDS}


def find_forget_candidates(query: str, limit: int = 5, *, scope: str = OWNER_SCOPE) -> list[dict[str, Any]]:
    """Find stored facts that plausibly match a natural forget request, using three cheap signals
    (no embeddings): FTS, normalised substring, and token overlap. Returns id+fact+score, best first.
    This is the search half of safe forgetting; deletion is decided by the caller.
    Scoped: a guest's forget request only ever surfaces that guest's own facts."""
    q = (query or "").strip()
    if not q:
        return []
    qtok = _tokens(q)
    scored: dict[int, dict[str, Any]] = {}
    with connect() as con:
        rows = con.execute("SELECT id, fact FROM memory_facts WHERE scope = ?", (scope,)).fetchall()
    for r in rows:
        fact = r["fact"]
        ftok = _tokens(fact)
        if not ftok:
            continue
        overlap = len(qtok & ftok)
        score = 0.0
        # substring (either direction) is a strong signal
        if q.lower() in fact.lower() or any(part and part in fact.lower() for part in [q.lower()]):
            score += 2.0
        # token overlap, normalised by the query's content tokens
        if qtok:
            score += 2.0 * (overlap / max(1, len(qtok)))
        if overlap:
            score += 0.5 * overlap
        if score > 0:
            scored[r["id"]] = {"id": r["id"], "fact": fact, "score": round(score, 3)}
    out = sorted(scored.values(), key=lambda d: d["score"], reverse=True)
    return out[:limit]


def forget_memory(query: str, polarity_hint: str = "", *, scope: str = OWNER_SCOPE) -> dict[str, Any]:
    """Safe natural-language forgetting, now backed by the general memory target resolver so
    differently-worded references to the same memory (e.g. 'Hawaiian' <-> 'pineapple on pizza',
    'HA' <-> 'Home Assistant') resolve correctly, while ambiguity asks rather than deleting blindly.
    Returns one of:
      {"status":"forgotten", "fact":...}
      {"status":"needs_confirmation", "candidates":[{id,fact},...]}
      {"status":"not_found"}
    Never deletes on ambiguity; never reports success unless a row was actually removed.
    Scoped: only searches and deletes within the caller's scope."""
    facts = [{"id": r["id"], "fact": r["fact"]} for r in _all_fact_rows(scope)]
    from ..memory_resolver import resolve_forget
    decision = resolve_forget(query, facts, polarity_hint=polarity_hint)
    if decision["action"] == "delete":
        cand = decision["candidate"]
        ok = _delete_fact_by_id(cand["id"])
        if ok:
            return {"status": "forgotten", "fact": cand["fact"]}
        return {"status": "not_found"}
    if decision["action"] == "confirm":
        return {"status": "needs_confirmation", "candidates": decision["candidates"]}
    return {"status": "not_found"}


def remember_preference(fact: str, polarity: str, target: str, source: str = "preference",
                        *, scope: str = OWNER_SCOPE) -> dict[str, Any]:
    """Store a preference, but supersede only a CLEAR opposite preference about the SAME referent.

    This is intentionally conservative. A broad preference ("likes pineapple") must NOT silently
    delete a narrower context-bound one ("dislikes pineapple on pizza"), and vice versa. We reuse the
    same referent resolver as fuzzy forgetting and only auto-supersede when it returns a strong single
    opposite-polarity match. Ambiguity or medium/weak similarity keeps both facts and lets later user
    turns disambiguate. Scoped: preference superseding stays within the caller's scope.
    """
    from ..memory_resolver import resolve_forget
    opposite = "dislikes" if polarity == "likes" else "likes"
    opposite_facts = [
        {"id": r["id"], "fact": r["fact"]}
        for r in _all_fact_rows(scope)
        if f"the user {opposite} " in r["fact"].lower()
    ]
    superseded = None
    if opposite_facts:
        decision = resolve_forget(target, opposite_facts, polarity_hint=opposite)
        if decision.get("action") == "delete":
            cand = decision["candidate"]
            if _delete_fact_by_id(cand["id"]):
                superseded = cand["fact"]
    stored = remember_fact(fact, source=source, scope=scope)
    return {**stored, "superseded": superseded}


def _all_fact_rows(scope: str = OWNER_SCOPE) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT id, fact FROM memory_facts WHERE scope = ?", (scope,)).fetchall()
    return [dict(r) for r in rows]


# --- Explicit "remember X" detection ----------------------------------------

_REMEMBER = re.compile(r"\b(?:please\s+)?remember(?:\s+that)?\s+(.+)", re.IGNORECASE)
_MY_NAME = re.compile(r"\bmy name is\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)", re.IGNORECASE)

# Cheap pre-filter: does this message plausibly STATE a durable fact about the
# user? Only then is it worth spending a model call to extract. First-person
# self-disclosure ("I am/like/have/prefer", "my X is", "I live/work/own ...").
# Questions and commands are excluded so "what do I like?" or "turn off the
# lights" never trigger extraction.
_FACT_BEARING = re.compile(
    r"\b(?:i'?m|i am|i like|i love|i hate|i prefer|i enjoy|i have|i live|i work|"
    r"i own|i drive|i use|i go|i play|i study|i train|i visit|i meet|i attend|"
    r"my\s+\w+\s+(?:is|are|was|goes|lives|works|likes|has|attends|plays|studies)|"
    r"i\s+(?:usually|always|normally|often|every)\s+\w+|"
    r"(?:every|each)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|day|morning|evening)|"
    r"on\s+(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays)|"
    r"i'?ve\s+got|i don'?t\s+like|call me|remember)\b",
    re.IGNORECASE,
)


def looks_fact_bearing(text: str) -> bool:
    """True if the message plausibly contains a durable fact worth extracting.

    Lets ordinary turns ("what's the weather", "tell me a joke") skip the
    extraction model call entirely, so only self-disclosing turns pay for it.
    Questions are excluded even if they contain a trigger word. A NEGATED memory
    instruction ("don't remember that...", "forget that...") is also excluded, so
    the model extractor is never given a chance to store what the user asked NOT
    to store, this closes the path the explicit-memory guard alone did not.
    """
    stripped = text.strip()
    # An instruction NOT to remember must not trigger extraction.
    if _NEGATED_REMEMBER.search(stripped):
        return False
    # A pure question is unlikely to be stating a fact about oneself.
    if stripped.endswith("?") and not _REMEMBER.search(stripped):
        return False
    return bool(_FACT_BEARING.search(stripped))


_NEGATED_REMEMBER = re.compile(
    r"\b(?:do\s*n[o']t|don't|do\s+not|never|please\s+don't|please\s+do\s+not|stop)\b[^.]*\bremember\b"
    r"|\bforget\b",
    re.IGNORECASE)


def detect_explicit_memory(text: str) -> str | None:
    """If the user explicitly asked to remember something, return the fact.
    Negated instructions ("do not remember that ...", "please don't remember ...")
    must NOT be treated as a request to store the fact."""
    if _NEGATED_REMEMBER.search(text):
        return None
    m = _REMEMBER.search(text)
    if m:
        return m.group(1).strip().rstrip(".")
    return None


def auto_extract_facts(user_text: str) -> list[str]:
    """Lightweight, deterministic extraction of durable facts from a user turn.

    Intentionally conservative: only high-confidence patterns (e.g. "my name
    is X") so we don't store noise. For richer extraction use
    propose_facts_via_model, which is model-assisted and confirmation-gated.
    """
    facts: list[str] = []
    name = _MY_NAME.search(user_text)
    if name:
        facts.append(f"The user's name is {name.group(1).strip()}.")
    return facts


_EXTRACT_SYSTEM = (
    "You extract durable, factual things worth remembering long-term about the "
    "user from a message. Return ONLY a JSON array of short fact strings, no "
    "prose, no markdown. Include only stable facts (name, preferences, "
    "relationships, recurring details), NOT transient things (today's weather, "
    "one-off requests) and NOT anything the user did not actually state. If "
    "there is nothing durable, return []."
)


async def propose_facts_via_model(user_text: str, *, role: str = "chat") -> list[str]:
    """Ask the local model to propose durable facts. Does NOT store them; the
    caller decides (e.g. after user confirmation). Returns [] on any failure so
    extraction never breaks a turn.

    Defaults to the chat model (role="chat") so extraction runs on the model
    that is already resident after a chat turn, avoiding a model swap (and the
    associated memory thrash) on a 24GB box. Pass role="tool" for the tool
    model if you want its stronger structured-output behaviour and don't mind
    the load.
    """
    import json as _json
    from ..llm import ask_ollama, LocalModelError

    try:
        raw = await ask_ollama(user_text, system=_EXTRACT_SYSTEM, role=role)
    except LocalModelError:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        data = _json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    # Keep only non-empty strings, de-duplicated against what's already stored.
    existing = {f["fact"].strip().lower() for f in all_facts(limit=500)}
    out: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip() and item.strip().lower() not in existing:
            out.append(item.strip())
    return out[:8]


# --- Context assembly --------------------------------------------------------

_BROAD_RECALL = re.compile(
    r"what\s+do\s+you\s+(?:remember|know)\s+about\s+me"
    r"|what\s+do\s+you\s+remember"
    r"|tell\s+me\s+(?:what\s+you\s+know|about\s+myself|about\s+me)"
    r"|what\s+do\s+you\s+know\s+about\s+me"
    r"|everything\s+you\s+(?:know|remember)",
    re.IGNORECASE)


def is_broad_recall(text: str) -> bool:
    return bool(_BROAD_RECALL.search(text or ""))


def build_context(conversation_id: str, prompt: str, *, history_limit: int = DEFAULT_HISTORY_LIMIT
                  ) -> tuple[list[dict[str, str]], list[str]]:
    """Return (history_messages, relevant_facts) for a turn.

    Short-term history is bounded by both a message count and a TOKEN budget (from settings),
    so verbose turns can't silently overflow the model context. For broad recall questions
    ("what do you remember about me?") a literal FTS search retrieves nothing, so return the
    most recent stored facts instead."""
    try:
        from ..settings import get_settings
        budget = getattr(get_settings(), "history_token_budget", DEFAULT_HISTORY_TOKEN_BUDGET)
    except Exception:
        budget = DEFAULT_HISTORY_TOKEN_BUDGET
    history = recent_messages(conversation_id, history_limit, token_budget=budget)
    # Scope facts to the conversation's namespace. A guest:* conversation can only ever see its own
    # facts: never the owner's. This is the isolation guarantee, enforced here in the data layer so
    # it holds no matter which caller (chat, voice, a future endpoint, a scheduled job) builds context.
    scope = scope_for_conversation(conversation_id)
    if is_broad_recall(prompt):
        facts = [f["fact"] for f in all_facts(limit=20, scope=scope)]
    else:
        facts = search_facts(prompt, scope=scope)
    return history, facts
