"""Delight cache + autogenerator, scales delights to the hundreds/thousands without a model call on
every pull.

Why: pregenerated lists top out at a few dozen, which repeats fast and starves under the family
cooldown. Instead we GENERATE delights in batches with the local model across many topics (general
science / psychology / universe + Alex's interests, and a guest-safe topic set), store them in a brain
table deduped by signature, and SERVE from that cache cheaply. A light background top-up keeps the pool
full. The curated shipped pool (delight.py) remains the always-available offline fallback.

Honesty: model-generated facts are LABELLED source='model' so a "fun fact" is never passed off as
verified. Jokes don't need that caveat.

Isolation: this cache is content only (facts/jokes), no owner data, so it's safe to draw from for both
the owner surpriser and the guest surpriser. The AUDIENCE split (which topics, whether owner-only
material is ever included) is enforced by the caller, not here; this module only ever produces neutral
delights.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3
from datetime import datetime, timezone
from typing import Any

# Topic wells. 'general' is the always-safe set (science / psychology / universe / dad jokes) that needs
# no interest match , the backbone of "there is no truly blank world". Interest topics personalise for
# the owner. Guest-safe topics deliberately exclude anything owner-personal.
_GENERAL_TOPICS = [
    "a surprising science fact", "a psychology or human-behaviour fact", "an astronomy or space fact",
    "a fact about the ocean or deep sea", "a fact about animals", "a fact about the human body",
    "a history fact that sounds made up but is true", "a fact about language or words",
    "a maths or numbers curiosity", "a geography fact", "a fact about music or sound",
    "a fact about food or cooking chemistry", "a 'good news' fact about the world improving",
]
_DAD_JOKE_TOPIC = "a clean, groan-worthy dad joke"

_GUEST_SAFE_TOPICS = list(_GENERAL_TOPICS)  # guests get the neutral well; no owner interests
_TARGET_POOL_MIN = 400          # keep at least this many UNUSED delights cached, per audience. Each is
                                # served at most once (used flag), and top-up keeps generating fresh
                                # ones, so the effective unique count grows well past 1000 over time.
_BATCH = 12                      # generate this many per top-up pass


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


def ensure_table() -> None:
    con = _con()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS delight_cache (
                sig TEXT PRIMARY KEY,
                kind TEXT NOT NULL,          -- 'fact' | 'joke'
                audience TEXT NOT NULL,      -- 'general' | 'guest' | interest name
                text TEXT NOT NULL,
                source TEXT NOT NULL,        -- 'model' | 'shipped'
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                used_at TEXT
            );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_delight_unused ON delight_cache(audience, used);")
        con.commit()
    finally:
        con.close()


def _sig(text: str) -> str:
    norm = " ".join(text.lower().split())
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def pool_size(*, audience: str = "general", unused_only: bool = True) -> int:
    ensure_table()
    con = _con()
    try:
        q = "SELECT COUNT(*) c FROM delight_cache WHERE audience = ?"
        if unused_only:
            q += " AND used = 0"
        return con.execute(q, (audience,)).fetchone()["c"]
    finally:
        con.close()


def _store(items: list[dict[str, Any]]) -> int:
    """Insert generated delights, ignoring duplicates by signature. Returns count newly stored."""
    ensure_table()
    if not items:
        return 0
    con = _con()
    stored = 0
    try:
        for it in items:
            text = (it.get("text") or "").strip()
            if not text or len(text) < 8:
                continue
            sig = _sig(text)
            try:
                cur = con.execute(
                    "INSERT OR IGNORE INTO delight_cache (sig, kind, audience, text, source, used, "
                    "created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (sig, it.get("kind", "fact"), it.get("audience", "general"), text,
                     it.get("source", "model"), _now_iso()))
                # cursor.rowcount is PER-STATEMENT: 1 if this row was inserted, 0 if INSERT OR IGNORE
                # skipped a duplicate. (con.total_changes is CUMULATIVE for the connection, so adding it
                # each iteration overcounts , e.g. 1+2+3 instead of 3. This is the receipt-accuracy fix.)
                if cur.rowcount and cur.rowcount > 0:
                    stored += cur.rowcount
            except Exception:
                pass
        con.commit()
    finally:
        con.close()
    return stored


def _generate_batch(audience: str, topics: list[str], n: int) -> list[dict[str, Any]]:
    """Generate up to n delights via the local model across the given topics. Best-effort; returns []
    if no model is reachable or we're inside an event loop (caller can schedule it off-loop)."""
    try:
        import asyncio
        try:
            asyncio.get_running_loop()
            return []   # don't nest; background top-up should run off-loop
        except RuntimeError:
            pass
        from ..llm import ask_persona
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    picks = (topics * ((n // max(len(topics), 1)) + 1))[:n]
    for topic in picks:
        is_joke = "joke" in topic
        if is_joke:
            prompt = ("Tell me ONE short, clean dad joke. One or two lines, no preamble, no quotes "
                      "around it.")
        else:
            prompt = (f"Give me ONE genuinely interesting, TRUE fun fact: {topic}. One or two "
                      "sentences, warm and specific, no preamble. If you're not fully confident it's "
                      "true, pick a different fact you ARE confident about.")
        try:
            import asyncio as _a
            text = (_a.run(ask_persona(prompt, role="chat")) or "").strip()
        except Exception:
            text = ""
        if text and 8 <= len(text) <= 400:
            out.append({"kind": "joke" if is_joke else "fact", "audience": audience,
                        "text": text, "source": "model"})
    return out


def top_up(*, audience: str = "general", target_min: int = _TARGET_POOL_MIN,
           batch: int = _BATCH) -> dict[str, Any]:
    """If the unused pool for this audience is below target_min, generate a batch and cache it. Safe to
    call on a schedule. Returns a small report. Never raises."""
    try:
        have = pool_size(audience=audience, unused_only=True)
        if have >= target_min:
            return {"ok": True, "topped_up": False, "have": have}
        topics = _GUEST_SAFE_TOPICS if audience == "guest" else _GENERAL_TOPICS
        # mix in dad jokes ~1 in 4
        gen_topics = topics + [_DAD_JOKE_TOPIC] * max(1, len(topics) // 4)
        random.shuffle(gen_topics)
        items = _generate_batch(audience, gen_topics, batch)
        stored = _store(items)
        return {"ok": True, "topped_up": True, "generated": len(items), "stored": stored,
                "have_now": pool_size(audience=audience, unused_only=True)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def draw(*, audience: str = "general", kind: str | None = None,
         recent_sigs: set[str] | None = None) -> dict[str, Any] | None:
    """Draw one unused cached delight for this audience, mark it used, return it. Returns None if the
    cache is empty (caller falls back to the shipped curated pool). Honest about source."""
    ensure_table()
    recent_sigs = recent_sigs or set()
    con = _con()
    try:
        q = "SELECT * FROM delight_cache WHERE audience = ? AND used = 0"
        params: list[Any] = [audience]
        if kind:
            q += " AND kind = ?"
            params.append(kind)
        q += " ORDER BY created_at ASC LIMIT 25"
        rows = con.execute(q, params).fetchall()
        for r in rows:
            if r["sig"] in recent_sigs:
                continue
            con.execute("UPDATE delight_cache SET used = 1, used_at = ? WHERE sig = ?",
                        (_now_iso(), r["sig"]))
            con.commit()
            return {"text": r["text"], "kind": r["kind"], "audience": r["audience"],
                    "source": r["source"], "sig": r["sig"],
                    "caveat": ("model-generated, worth a sanity check before repeating as fact"
                               if r["source"] == "model" and r["kind"] == "fact" else None)}
        return None
    finally:
        con.close()


def stats() -> dict[str, Any]:
    ensure_table()
    con = _con()
    try:
        rows = con.execute(
            "SELECT audience, kind, COUNT(*) c, SUM(used) u FROM delight_cache GROUP BY audience, kind"
        ).fetchall()
        return {"buckets": [dict(r) for r in rows],
                "total": con.execute("SELECT COUNT(*) c FROM delight_cache").fetchone()["c"]}
    finally:
        con.close()
