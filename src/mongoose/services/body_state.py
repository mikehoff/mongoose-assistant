from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

# Body State: the local canonical body/movement memory.
#
# The architecture all four design docs converge on: Mongoose OWNS a local ledger; the apps
# (Fitbit/Google Health, MyFitnessPal, Alpha Progression, Health Connect) are INPUTS to it, not
# replacements. This module is that ledger , the floor. Importers, an Android bridge, cloud
# consults and a correlation engine all layer ON TOP later; they need this to exist first.
#
# Slice 1 scope (deliberately bounded): the five core tables + voice/manual capture + a
# transparent, baseline-relative load/readiness estimate (in body_readiness.py). NO importers,
# NO Android, NO cloud, NO correlation engine yet , those slot in cleanly once the floor is real.
#
# Honesty commitments baked in from the start:
#  - The ledger records what you LOG. Consumer metrics (e.g. smart-scale muscle mass) are stored
#    with a `confidence` field and treated as DIRECTIONAL, never exact truth.
#  - Subjective state is first-class, not an afterthought , the docs are right that the lived
#    experience is the thing Mongoose has that Fitbit/MFP/Alpha individually don't.

# the kinds of entries the ledger holds (one table each)
KINDS = ("workout", "swim", "physio", "body_comp", "body_state", "journal")

# Body vocabulary slots. The body layer's parsing and phrasing are built from these generic words; a
# site profile names the person's own pattern with lexicon "body.<slot>" (a plain word or phrase, used
# as-is in parsing patterns and in speech).
_BODY_VOCAB: dict[str, str] = {
    "primary_muscle": "calf",                    # physio focus: 'right calf', 'calf bridge'
    "primary_muscle_group": "calves",            # its progression group name
    "paired_recovery": "yoga",                   # the recovery habit logged alongside a swim
    "gentle_swap": "walk/stretch",               # the gentle alternative offered instead of a session
    "posture_cue": "shoulders down",             # a posture focus a physio log can name
    "primer_name": "activation primer",          # what the pre-lift primer is called
    "physio_focus_default": "your usual activation work",
}


def body_vocab(slot: str) -> str:
    """One body vocabulary slot: the site profile's word (lexicon body.<slot>) or the generic default."""
    from .. import profile as _profile
    return str(_profile.lexicon("body." + slot, _BODY_VOCAB[slot]))


def _ensure_tables() -> None:
    from ..db import connect
    with connect() as con:
        # a strength/gym session. Detailed set data can live in `detail` (JSON) so we don't
        # over-model before importers exist; the columns are what we query/correlate on.
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_workouts (
                id TEXT PRIMARY KEY, at TEXT NOT NULL,
                kind TEXT, focus TEXT, duration_min INTEGER,
                rpe REAL, notes TEXT,
                pain_flag INTEGER DEFAULT 0,
                detail TEXT, source TEXT DEFAULT 'manual', created_at TEXT NOT NULL
            )""")
        # a swim , first-class (cardio + recovery + puffiness/state tool, per the docs)
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_swims (
                id TEXT PRIMARY KEY, at TEXT NOT NULL,
                duration_min INTEGER, distance_m INTEGER, intensity TEXT,
                paired_sauna INTEGER DEFAULT 0,
                puffiness_before INTEGER, puffiness_after INTEGER,
                tension_before INTEGER, tension_after INTEGER,
                notes TEXT, source TEXT DEFAULT 'manual', created_at TEXT NOT NULL
            )""")
        # a physio session , first-class (constraint management: alignment, asymmetry, pain)
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_physio (
                id TEXT PRIMARY KEY, at TEXT NOT NULL,
                duration_min INTEGER, focus TEXT, exercises TEXT,
                pain_before INTEGER, pain_after INTEGER,
                tension_before INTEGER, tension_after INTEGER,
                effect TEXT, notes TEXT, source TEXT DEFAULT 'manual', created_at TEXT NOT NULL
            )""")
        # body composition , stored with confidence (consumer scales are noisy/directional)
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_body_comp (
                id TEXT PRIMARY KEY, at TEXT NOT NULL,
                weight_kg REAL, body_fat_pct REAL, lean_mass_kg REAL, muscle_mass_kg REAL,
                waist_cm REAL, hip_cm REAL,
                source TEXT DEFAULT 'manual', confidence TEXT DEFAULT 'medium',
                notes TEXT, created_at TEXT NOT NULL
            )""")
        # subjective body-state , the lived-experience layer (load/tension/pain/mood/etc.)
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_body_state (
                id TEXT PRIMARY KEY, at TEXT NOT NULL,
                load INTEGER, tension INTEGER, energy INTEGER, mood INTEGER,
                pain INTEGER, puffiness INTEGER,
                right_left TEXT, back_safety INTEGER,
                sleep_hours REAL, hrv REAL, resting_hr REAL,
                tags TEXT, notes TEXT, source TEXT DEFAULT 'manual', created_at TEXT NOT NULL
            )""")
        # body-literacy journal , guided check-ins, proprioception/grounding resets, and
        # emotional-literacy entries. mode distinguishes them. This is the lived-experience bridge:
        # body sensation <-> emotion <-> what helped. NEVER a diagnosis , it stores what the USER
        # named, not what Mongoose decided they feel.
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_journal (
                id TEXT PRIMARY KEY, at TEXT NOT NULL,
                mode TEXT,
                body_signal TEXT, body_texture TEXT, emotion_words TEXT,
                need TEXT, next_step TEXT,
                reset_type TEXT, duration_min INTEGER,
                before_state INTEGER, after_state INTEGER, effect TEXT,
                text TEXT, linked_context TEXT,
                source TEXT DEFAULT 'manual', created_at TEXT NOT NULL
            )""")
        con.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --- create ------------------------------------------------------------------

def log_workout(*, focus: str = "", kind: str = "", duration_min: int | None = None,
                rpe: float | None = None, pain_flag: bool = False, notes: str = "",
                detail: dict | None = None, at: datetime | None = None,
                source: str = "manual") -> dict[str, Any]:
    _ensure_tables()
    wid = uuid.uuid4().hex[:12]
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO bs_workouts(id,at,kind,focus,duration_min,rpe,notes,pain_flag,"
                    "detail,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (wid, _iso(at or _now()), kind, focus, duration_min, rpe, notes,
                     1 if pain_flag else 0, json.dumps(detail or {}), source, _iso(_now())))
        con.commit()
    return get_entry("workout", wid)


def log_swim(*, duration_min: int | None = None, distance_m: int | None = None,
             intensity: str = "", paired_sauna: bool = False,
             puffiness_before: int | None = None, puffiness_after: int | None = None,
             tension_before: int | None = None, tension_after: int | None = None,
             notes: str = "", at: datetime | None = None, source: str = "manual") -> dict[str, Any]:
    _ensure_tables()
    sid = uuid.uuid4().hex[:12]
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO bs_swims(id,at,duration_min,distance_m,intensity,paired_sauna,"
                    "puffiness_before,puffiness_after,tension_before,tension_after,notes,source,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, _iso(at or _now()), duration_min, distance_m, intensity,
                     1 if paired_sauna else 0, puffiness_before, puffiness_after,
                     tension_before, tension_after, notes, source, _iso(_now())))
        con.commit()
    return get_entry("swim", sid)


def log_physio(*, focus: str = "", exercises: str = "", duration_min: int | None = None,
               pain_before: int | None = None, pain_after: int | None = None,
               tension_before: int | None = None, tension_after: int | None = None,
               effect: str = "", notes: str = "", at: datetime | None = None,
               source: str = "manual") -> dict[str, Any]:
    _ensure_tables()
    pid = uuid.uuid4().hex[:12]
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO bs_physio(id,at,duration_min,focus,exercises,pain_before,"
                    "pain_after,tension_before,tension_after,effect,notes,source,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, _iso(at or _now()), duration_min, focus, exercises, pain_before,
                     pain_after, tension_before, tension_after, effect, notes, source, _iso(_now())))
        con.commit()
    return get_entry("physio", pid)


def log_body_comp(*, weight_kg: float | None = None, body_fat_pct: float | None = None,
                  lean_mass_kg: float | None = None, muscle_mass_kg: float | None = None,
                  waist_cm: float | None = None, hip_cm: float | None = None,
                  source: str = "manual", confidence: str = "medium", notes: str = "",
                  at: datetime | None = None) -> dict[str, Any]:
    _ensure_tables()
    bid = uuid.uuid4().hex[:12]
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO bs_body_comp(id,at,weight_kg,body_fat_pct,lean_mass_kg,"
                    "muscle_mass_kg,waist_cm,hip_cm,source,confidence,notes,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, _iso(at or _now()), weight_kg, body_fat_pct, lean_mass_kg,
                     muscle_mass_kg, waist_cm, hip_cm, source, confidence, notes, _iso(_now())))
        con.commit()
    return get_entry("body_comp", bid)


def log_body_state(*, load: int | None = None, tension: int | None = None,
                   energy: int | None = None, mood: int | None = None, pain: int | None = None,
                   puffiness: int | None = None, right_left: str = "", back_safety: int | None = None,
                   sleep_hours: float | None = None, hrv: float | None = None,
                   resting_hr: float | None = None, tags: list[str] | None = None,
                   notes: str = "", at: datetime | None = None,
                   source: str = "manual") -> dict[str, Any]:
    _ensure_tables()
    bid = uuid.uuid4().hex[:12]
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO bs_body_state(id,at,load,tension,energy,mood,pain,puffiness,"
                    "right_left,back_safety,sleep_hours,hrv,resting_hr,tags,notes,source,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, _iso(at or _now()), load, tension, energy, mood, pain, puffiness,
                     right_left, back_safety, sleep_hours, hrv, resting_hr,
                     ",".join(tags or []), notes, source, _iso(_now())))
        con.commit()
    return get_entry("body_state", bid)


def log_journal(*, mode: str, body_signal: str = "", body_texture: str = "",
                emotion_words: list[str] | None = None, need: str = "", next_step: str = "",
                reset_type: str = "", duration_min: int | None = None,
                before_state: int | None = None, after_state: int | None = None,
                effect: str = "", text: str = "", linked_context: str = "",
                at: datetime | None = None, source: str = "manual") -> dict[str, Any]:
    """Log a body-literacy entry: a guided check-in, a proprioception/grounding reset, or an
    emotional-literacy note. Stores what the USER named, never an inferred diagnosis."""
    _ensure_tables()
    jid = uuid.uuid4().hex[:12]
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO bs_journal(id,at,mode,body_signal,body_texture,emotion_words,"
                    "need,next_step,reset_type,duration_min,before_state,after_state,effect,text,"
                    "linked_context,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (jid, _iso(at or _now()), mode, body_signal, body_texture,
                     ",".join(emotion_words or []), need, next_step, reset_type, duration_min,
                     before_state, after_state, effect, text, linked_context, source, _iso(_now())))
        con.commit()
    return get_entry("journal", jid)


# --- read --------------------------------------------------------------------

_TABLE = {"workout": "bs_workouts", "swim": "bs_swims", "physio": "bs_physio",
          "body_comp": "bs_body_comp", "body_state": "bs_body_state", "journal": "bs_journal"}


def get_entry(kind: str, entry_id: str) -> dict[str, Any] | None:
    _ensure_tables()
    table = _TABLE.get(kind)
    if not table:
        return None
    from ..db import connect
    with connect() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE id = ?", (entry_id,)).fetchone()
    return _row(kind, row) if row else None


def list_entries(kind: str, *, since: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
    _ensure_tables()
    table = _TABLE.get(kind)
    if not table:
        return []
    from ..db import connect
    q = f"SELECT * FROM {table}"
    params: list[Any] = []
    if since:
        q += " WHERE at >= ?"
        params.append(_iso(since))
    q += " ORDER BY at DESC LIMIT ?"
    params.append(limit)
    with connect() as con:
        rows = con.execute(q, params).fetchall()
    return [_row(kind, r) for r in rows]


def recent_all(days: int = 14) -> dict[str, list[dict[str, Any]]]:
    """Everything logged in the last N days, by kind. The basis for the day's picture / a future
    consult pack."""
    since = _now() - timedelta(days=days)
    return {k: list_entries(k, since=since, limit=200) for k in KINDS}


def last_of(kind: str) -> dict[str, Any] | None:
    items = list_entries(kind, limit=1)
    return items[0] if items else None


def _row(kind: str, row) -> dict[str, Any]:
    d = dict(row)
    d["_kind"] = kind
    if "tags" in d and d.get("tags"):
        d["tags"] = d["tags"].split(",")
    elif "tags" in d:
        d["tags"] = []
    if "emotion_words" in d and d.get("emotion_words"):
        d["emotion_words"] = d["emotion_words"].split(",")
    elif "emotion_words" in d:
        d["emotion_words"] = []
    if "detail" in d and d.get("detail"):
        try:
            d["detail"] = json.loads(d["detail"])
        except Exception:
            pass
    return d
