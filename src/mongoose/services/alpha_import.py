from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Any

# Alpha Progression importer , built against the user's REAL export formats (248 sessions, 4515
# sets, body composition, activities), not assumed shapes. This is what turns "are my delts
# progressing" / "am I overdoing quads" / "what's my muscle trend" from honest not_built_yet
# deflections into real answers from real data.
#
# Honest limits held (the calibration doc is explicit):
#  - Body composition (BIA/smart-scale) is HYDRATION-SENSITIVE , stored directional, never exact
#    "you gained X kg of muscle". The query layer always frames it as a trend.
#  - Limb-level left/right muscle mass is SPARSE in the real data (many scans omit it), so we do
#    NOT use it to assert right-side asymmetry; that stays grounded in logged feeling + physio.
#  - Swimming is sparse, so swim<->puffiness correlation still needs more data (unchanged).
#
# The importer is idempotent-ish: it tags imported rows with source="alpha" and a content hash so
# re-importing the same export doesn't duplicate.

# canonical muscle groups (matches the user's real muscle_group_summary)
MUSCLE_GROUPS = ("biceps", "back_lats", "delts", "quads", "core_proprio", "triceps",
                 "hamstrings", "calves", "chest", "calves", "other")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str) -> str | None:
    """Alpha dates come as '2026-06-08 3:36 pm' or ISO '2026-06-08 15:36:00'. Return ISO date."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    # ISO with offset
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _f(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ensure_alpha_tables() -> None:
    from ..db import connect
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_alpha_sessions (
                id TEXT PRIMARY KEY, at TEXT, title TEXT, duration_min INTEGER,
                n_exercises INTEGER, n_sets INTEGER, imported_at TEXT
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_alpha_sets (
                id TEXT PRIMARY KEY, at TEXT, exercise TEXT, equipment TEXT, muscle_group TEXT,
                set_no INTEGER, kg REAL, reps REAL, rir REAL, imported_at TEXT
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS bs_alpha_plan (
                exercise TEXT PRIMARY KEY, muscle_group TEXT, day TEXT, imported_at TEXT
            )""")
        con.commit()


# --- exercise -> muscle group (so we can answer per-group questions) ----------

_GROUP_RULES = [
    ("delts", r"lateral raise|shoulder press|face pull|rear delt|overhead press|arnold"),
    ("back_lats", r"pulldown|pullover|row|pull.?up|chin.?up|lat "),
    ("hamstrings", r"leg curl|romanian|rdl|deadlift|good morning"),
    ("triceps", r"pushdown|triceps|skull|dip|overhead triceps"),
    ("biceps", r"curl"),
    ("chest", r"chest|bench|pec|fly|incline press|push.?up"),
    ("quads", r"leg press|leg extension|squat|lunge|hack"),
    ("calves", r"calf|hip thrust|hip adduction|bridge|hip abduction"),
    ("calves", r"calf|calves"),
    ("core_proprio", r"pallof|plank|ab |crunch|dead.?bug|core|rotation|anti.?rotation"),
]


def muscle_group_for(exercise: str) -> str:
    low = (exercise or "").lower()
    # Bulgarian split squat is quad/calf dominant , quads bucket but note calves too; keep simple
    for group, pat in _GROUP_RULES:
        if re.search(pat, low):
            return group
    return "other"


# --- importers ---------------------------------------------------------------

def import_sessions_csv(text: str) -> dict[str, Any]:
    _ensure_alpha_tables()
    from ..db import connect
    reader = csv.DictReader(io.StringIO(text))
    n = 0
    with connect() as con:
        for row in reader:
            at = _parse_dt(row.get("dt") or row.get("date") or "")
            if not at:
                continue
            sid = _hash("sess", row.get("title", ""), at)
            dur = _dur_min(row.get("duration", ""))
            con.execute("INSERT OR REPLACE INTO bs_alpha_sessions"
                        "(id,at,title,duration_min,n_exercises,n_sets,imported_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (sid, at, row.get("title", ""), dur,
                         _int(row.get("n_exercises")), _int(row.get("n_sets")), _now_iso()))
            n += 1
        con.commit()
    return {"ok": True, "imported": n, "kind": "sessions"}


def import_sets_csv(text: str) -> dict[str, Any]:
    _ensure_alpha_tables()
    from ..db import connect
    reader = csv.DictReader(io.StringIO(text))
    n = 0
    with connect() as con:
        for row in reader:
            at = _parse_dt(row.get("dt") or row.get("date") or "")
            ex = row.get("exercise", "")
            if not at or not ex:
                continue
            sid = _hash("set", ex, at, row.get("set_no", ""), row.get("order", ""))
            con.execute("INSERT OR REPLACE INTO bs_alpha_sets"
                        "(id,at,exercise,equipment,muscle_group,set_no,kg,reps,rir,imported_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (sid, at, ex, row.get("equipment", ""), muscle_group_for(ex),
                         _int(row.get("set_no")), _f(row.get("kg")), _f(row.get("reps")),
                         _f(row.get("rir")), _now_iso()))
            n += 1
        con.commit()
    return {"ok": True, "imported": n, "kind": "sets"}


def import_body_comp_csv(text: str) -> dict[str, Any]:
    """Body composition, stored in the existing bs_body_comp ledger table, DIRECTIONAL. The real
    export is wide (100 metrics); we take the load-bearing ones and keep confidence='noisy'."""
    from . import body_state as BS
    reader = csv.DictReader(io.StringIO(text))
    n = 0
    for row in reader:
        at = _parse_dt(row.get("dt") or row.get("date") or "")
        if not at:
            continue
        weight = _f(row.get("Weight"))
        smm = _f(row.get("Skeletal Muscle Mass"))
        bf = _f(row.get("Fat mass Perc") or row.get("Fat Mass Perc"))
        if weight is None and smm is None and bf is None:
            continue
        BS.log_body_comp(weight_kg=weight, body_fat_pct=bf, muscle_mass_kg=smm,
                         source="alpha", confidence="noisy",
                         at=datetime.fromisoformat(at))
        n += 1
    return {"ok": True, "imported": n, "kind": "body_comp"}


def import_current_plan_csv(sessions_text: str) -> dict[str, Any]:
    """Derive the current plan from the most recent sessions' exercises. The real export's latest
    plan is the 'New plan' 2-day split; we read the exercises actually performed recently."""
    _ensure_alpha_tables()
    from ..db import connect
    # use the most recent ~6 weeks of sets to define "current plan"
    with connect() as con:
        rows = con.execute(
            "SELECT DISTINCT exercise, muscle_group FROM bs_alpha_sets "
            "WHERE at >= (SELECT MAX(at) FROM bs_alpha_sets) "
            "  AND at >= date((SELECT MAX(at) FROM bs_alpha_sets), '-42 days') "
            "ORDER BY exercise"
        ).fetchall()
        n = 0
        for r in rows:
            con.execute("INSERT OR REPLACE INTO bs_alpha_plan(exercise,muscle_group,day,imported_at)"
                        " VALUES(?,?,?,?)", (r["exercise"], r["muscle_group"], "", _now_iso()))
            n += 1
        con.commit()
    return {"ok": True, "imported": n, "kind": "plan"}


def import_all(*, sessions: str | None = None, sets: str | None = None,
               body_comp: str | None = None) -> dict[str, Any]:
    """Import a full Alpha export bundle. Returns a per-file summary."""
    out: dict[str, Any] = {}
    if sessions:
        out["sessions"] = import_sessions_csv(sessions)
    if sets:
        out["sets"] = import_sets_csv(sets)
        out["plan"] = import_current_plan_csv(sets)
    if body_comp:
        out["body_comp"] = import_body_comp_csv(body_comp)
    return {"ok": True, "summary": out}


# --- helpers -----------------------------------------------------------------

def _dur_min(s: str) -> int | None:
    m = re.search(r"(\d+)\s*min", s or "")
    return int(m.group(1)) if m else None


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _hash(*parts: str) -> str:
    import hashlib
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --- MyFitnessPal nutrition import -------------------------------------------
# Wired now (it was previously "not built"): the standard MFP "Nutrition-Summary" CSV export has one
# row per meal with a Date column, plus Calories / Carbohydrates (g) / Protein (g) / Fat (g) /
# Sodium (mg) / Sugar (g) / Fiber. We aggregate to one row per day in bs_mfp_days, which is what the
# body-data nudge and any future feel<->nutrition correlation read. Held honest: this is logged
# intake as the owner recorded it, not a claim about what they actually ate.

def _ensure_mfp_table() -> None:
    from ..db import connect
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bs_mfp_days (
                date TEXT PRIMARY KEY,
                calories REAL, carbs_g REAL, protein_g REAL, fat_g REAL,
                sodium_mg REAL, sugar_g REAL, fiber_g REAL,
                meals INTEGER, imported_at TEXT
            )
            """
        )


def _mfp_date(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # last resort: ISO-ish prefix
    iso = _parse_dt(s)
    return iso[:10] if iso else None


def _mfp_col(row: dict, *names: str) -> float | None:
    """Pull a numeric value from whichever of the candidate column names MFP used (their headers vary
    slightly by locale/version)."""
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for n in names:
        for key, val in lower.items():
            if key == n or key.startswith(n):
                f = _f(str(val))
                if f is not None:
                    return f
    return None


def import_mfp_csv(text: str) -> dict[str, Any]:
    """Import a MyFitnessPal nutrition export (CSV). Aggregates meal rows into per-day totals.

    Returns the number of days imported. Idempotent per day: re-importing replaces that day's totals.
    """
    _ensure_mfp_table()
    reader = csv.DictReader(io.StringIO(text))
    days: dict[str, dict[str, float]] = {}
    meal_counts: dict[str, int] = {}
    for row in reader:
        d = _mfp_date(row.get("Date") or row.get("date") or "")
        if not d:
            continue
        agg = days.setdefault(d, {"calories": 0.0, "carbs_g": 0.0, "protein_g": 0.0,
                                  "fat_g": 0.0, "sodium_mg": 0.0, "sugar_g": 0.0, "fiber_g": 0.0})
        meal_counts[d] = meal_counts.get(d, 0) + 1
        for field, names in (
            ("calories", ("calories", "energy")),
            ("carbs_g", ("carbohydrates", "carbs")),
            ("protein_g", ("protein",)),
            ("fat_g", ("fat (g)", "fat", "total fat")),
            ("sodium_mg", ("sodium",)),
            ("sugar_g", ("sugar",)),
            ("fiber_g", ("fiber", "fibre")),
        ):
            v = _mfp_col(row, *names)
            if v is not None:
                agg[field] += v
    if not days:
        return {"ok": False, "error": "no dated rows found; is this a MyFitnessPal nutrition CSV export?",
                "kind": "mfp"}
    from ..db import connect
    with connect() as con:
        for d, agg in days.items():
            con.execute(
                "INSERT OR REPLACE INTO bs_mfp_days"
                "(date,calories,carbs_g,protein_g,fat_g,sodium_mg,sugar_g,fiber_g,meals,imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (d, agg["calories"], agg["carbs_g"], agg["protein_g"], agg["fat_g"],
                 agg["sodium_mg"], agg["sugar_g"], agg["fiber_g"], meal_counts.get(d, 0), _now_iso()))
        con.commit()
    return {"ok": True, "imported": len(days), "kind": "mfp"}
