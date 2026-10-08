from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

# Richer progression analysis , the "less crude" layer.
#
# The old signal compared only the single heaviest set this 6 weeks vs the prior 6 weeks. That
# called the user's clearly-progressing delts "flat" (max-weight bounced as exercises/grips
# changed) while estimated-1RM on lateral raises rose 12.3 -> 14.6 over six months. So the fix is
# to MEASURE progression the way it actually shows up:
#   - estimated 1RM (Epley) PER EXERCISE over time, with a linear-fit slope (a trend, not two
#     endpoints) , this is the strength signal.
#   - working-volume PER EXERCISE over time , this is the hypertrophy/work signal.
# Then name the specific lifts moving vs stuck, and say what it MEANS.
#
# Honesty guardrails (these keep it trustworthy, not just confident):
#   - Estimated 1RM is an ESTIMATE (Epley formula), never a measured max. Always labelled.
#   - A trend needs enough sessions (>=MIN_SESSIONS) or it says "not enough to call yet" rather
#     than reading noise off 2 points.
#   - "Stalled"/"declining" are observations from the data, not motivational nudges.
#   - High-rep sets (>20) are capped for Epley sanity.

MIN_SESSIONS = 4          # need this many sessions of an exercise to assert a trend
TREND_DAYS = 120          # look back this far for a per-exercise trend
_EPLEY_REP_CAP = 20       # above this, Epley est-1RM is unreliable


def _now() -> datetime:
    return datetime.now(timezone.utc)


def est_1rm(kg: float, reps: float) -> float | None:
    """Epley estimated 1RM. An ESTIMATE, not a measured max. Capped at sane rep ranges."""
    if kg is None or reps is None or kg <= 0 or reps <= 0:
        return None
    r = min(reps, _EPLEY_REP_CAP)
    return round(kg * (1 + r / 30.0), 1)


def _slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of y over x. Used on (day_index, value) to get a per-day trend."""
    n = len(points)
    if n < 2:
        return 0.0
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


# --- per-exercise trend ------------------------------------------------------

def exercise_trend(exercise: str, *, days: int = TREND_DAYS) -> dict[str, Any]:
    """Estimated-1RM and volume trend for one exercise. Returns direction + magnitude over the
    window, or status 'insufficient' when there aren't enough sessions to call it."""
    from ..db import connect
    since = (_now() - timedelta(days=days)).isoformat()
    with connect() as con:
        rows = con.execute(
            "SELECT at, kg, reps FROM bs_alpha_sets WHERE exercise = ? AND at >= ? "
            "AND kg IS NOT NULL AND reps IS NOT NULL ORDER BY at", (exercise, since)).fetchall()
    # collapse to best est-1RM per session day + total volume per day
    by_day: dict[str, dict[str, float]] = {}
    for r in rows:
        day = r["at"][:10]
        e = est_1rm(r["kg"], r["reps"])
        vol = (r["kg"] or 0) * (r["reps"] or 0)
        d = by_day.setdefault(day, {"e1rm": 0.0, "vol": 0.0})
        if e and e > d["e1rm"]:
            d["e1rm"] = e
        d["vol"] += vol
    days_sorted = sorted(by_day)
    n = len(days_sorted)
    if n < MIN_SESSIONS:
        return {"exercise": exercise, "status": "insufficient", "sessions": n,
                "speech": f"{exercise}: only {n} session{'s' if n != 1 else ''} logged in range, "
                          "not enough to call a trend yet."}
    # build (day_index, value) series
    base = datetime.fromisoformat(by_day[days_sorted[0]] and days_sorted[0])
    def idx(day): return (datetime.fromisoformat(day) - base).days
    e_series = [(idx(d), by_day[d]["e1rm"]) for d in days_sorted if by_day[d]["e1rm"] > 0]
    v_series = [(idx(d), by_day[d]["vol"]) for d in days_sorted]
    e_first = e_series[0][1] if e_series else None
    e_last = e_series[-1][1] if e_series else None
    e_slope = _slope(e_series)                # est-1RM kg per day
    v_slope = _slope(v_series)
    span_days = max(1, idx(days_sorted[-1]))
    e_change = round(e_slope * span_days, 1) if e_series else 0.0

    direction = _direction(e_slope, e_first)
    # a single linear slope misses reversals (e.g. dropped then recovered). Check the recent third
    # against the earlier portion so a V-shaped recovery doesn't read as a steady decline.
    recent_note = _reversal(e_series)
    return {"exercise": exercise, "status": "ok", "sessions": n,
            "e1rm_first": e_first, "e1rm_last": e_last, "e1rm_change": e_change,
            "e1rm_per_week": round(e_slope * 7, 2), "reversal": recent_note,
            "vol_direction": _direction(v_slope, v_series[0][1] if v_series else None),
            "direction": direction,
            "speech": _exercise_speech(exercise, direction, e_first, e_last, e_change, n, recent_note)}


def _reversal(e_series: list[tuple[float, float]]) -> str | None:
    """If the recent third trends opposite to the earlier part, name it (recovering / fading)."""
    if len(e_series) < 6:
        return None
    third = max(2, len(e_series) // 3)
    earlier = e_series[:-third]
    recent = e_series[-third:]
    if len(earlier) < 2 or len(recent) < 2:
        return None
    es = _slope(earlier)
    rs = _slope(recent)
    e_first = earlier[0][1]
    if e_first == 0:
        return None
    # opposite signs and both meaningful
    if es < 0 and rs > 0 and (rs * 30 / e_first * 100) >= 2.5:
        return "recovering"
    if es > 0 and rs < 0 and (abs(rs) * 30 / e_first * 100) >= 2.5:
        return "fading recently"
    return None


def _direction(slope: float, first_val: float | None) -> str:
    """Classify a trend, scaled to the magnitude of the values so it's not noise-sensitive."""
    if first_val is None or first_val == 0:
        return "flat"
    # per-day slope as a fraction of starting value, projected over ~30 days
    monthly_pct = (slope * 30) / first_val * 100
    if monthly_pct >= 2.5:
        return "rising"
    if monthly_pct <= -2.5:
        return "falling"
    return "flat"


def _exercise_speech(exercise, direction, e_first, e_last, e_change, n, reversal=None) -> str:
    tail = ""
    if reversal == "recovering":
        tail = ", but climbing again over recent sessions"
    elif reversal == "fading recently":
        tail = ", though it's eased off in recent sessions"
    if direction == "rising":
        return (f"{exercise}: estimated 1RM up about {abs(e_change):.0f}kg over the period "
                f"({e_first:.0f} to {e_last:.0f}kg est), across {n} sessions, progressing{tail}.")
    if direction == "falling":
        return (f"{exercise}: estimated 1RM down about {abs(e_change):.0f}kg "
                f"({e_first:.0f} to {e_last:.0f}kg est) over {n} sessions, drifting down{tail}.")
    return (f"{exercise}: estimated 1RM roughly flat ({e_first:.0f} to {e_last:.0f}kg est) over "
            f"{n} sessions, holding, not building{tail}.")


# --- group rollup: name the lifts moving vs stuck ----------------------------

def group_progression(group: str, *, days: int = TREND_DAYS) -> dict[str, Any]:
    """The richer per-group answer: which exercises in this group are moving, flat or stuck."""
    from ..db import connect
    since = (_now() - timedelta(days=days)).isoformat()
    with connect() as con:
        exs = [r["exercise"] for r in con.execute(
            "SELECT exercise, COUNT(*) c FROM bs_alpha_sets WHERE muscle_group = ? AND at >= ? "
            "GROUP BY exercise ORDER BY c DESC", (group, since)).fetchall()]
    if not exs:
        return {"have_data": False,
                "speech": f"No {group.replace('_', '/')} work logged in range."}
    rising, flat, falling, thin = [], [], [], []
    for ex in exs:
        t = exercise_trend(ex, days=days)
        if t["status"] == "insufficient":
            thin.append(ex)
        elif t["direction"] == "rising":
            rising.append(t)
        elif t["direction"] == "falling":
            falling.append(t)
        else:
            flat.append(t)
    return {"have_data": True, "group": group,
            "rising": rising, "flat": flat, "falling": falling, "thin": thin,
            "speech": _group_speech(group, rising, flat, falling, thin)}


def _group_speech(group, rising, flat, falling, thin) -> str:
    label = group.replace("_", "/")
    parts = []
    if rising:
        names = ", ".join(t["exercise"] for t in rising[:3])
        parts.append(f"moving up: {names}")
    if falling:
        names = ", ".join(t["exercise"] for t in falling[:3])
        parts.append(f"drifting down: {names}")
    if flat:
        names = ", ".join(t["exercise"] for t in flat[:3])
        parts.append(f"flat/holding: {names}")
    if not parts:
        return f"{label}: not enough session history yet to call which lifts are moving."
    head = f"{label}, " + "; ".join(parts) + "."
    if thin:
        head += f" (Too few sessions to judge: {', '.join(thin[:2])}.)"
    head += " Estimated 1RMs are formula estimates, not measured maxes."
    return head


# --- meaning framing: building / maintaining / detraining --------------------

def training_phase(*, days: int = 42) -> dict[str, Any]:
    """What the recent block MEANS overall: building (volume + strength up), maintaining (steady),
    or detraining (volume down). Uses the aggregate of recent exercise trends + total volume."""
    from ..db import connect
    since = (_now() - timedelta(days=days)).isoformat()
    prev_since = (_now() - timedelta(days=days * 2)).isoformat()
    with connect() as con:
        recent_vol = con.execute(
            "SELECT ROUND(SUM(kg*reps),0) v, COUNT(*) sets FROM bs_alpha_sets "
            "WHERE at >= ? AND kg IS NOT NULL AND reps IS NOT NULL", (since,)).fetchone()
        prev_vol = con.execute(
            "SELECT ROUND(SUM(kg*reps),0) v FROM bs_alpha_sets "
            "WHERE at >= ? AND at < ? AND kg IS NOT NULL AND reps IS NOT NULL",
            (prev_since, since)).fetchone()
    rv = recent_vol["v"] or 0 if recent_vol else 0
    pv = prev_vol["v"] or 0 if prev_vol else 0
    if rv == 0:
        return {"phase": "idle", "speech": "No working sets logged recently."}
    vol_change_pct = ((rv - pv) / pv * 100) if pv else 0.0
    if vol_change_pct >= 12:
        phase = "building"
    elif vol_change_pct <= -12:
        phase = "detraining"
    else:
        phase = "maintaining"
    return {"phase": phase, "recent_volume": rv, "prev_volume": pv,
            "vol_change_pct": round(vol_change_pct, 0),
            "speech": _phase_speech(phase, vol_change_pct)}


def _phase_speech(phase, pct) -> str:
    if phase == "building":
        return (f"Your recent block looks like building, training volume is up about {abs(pct):.0f}% "
                "versus the previous six weeks.")
    if phase == "detraining":
        return (f"Your recent block looks like a deload or detraining, volume is down about "
                f"{abs(pct):.0f}% versus the previous six weeks.")
    return "Your recent block looks like maintaining, volume is roughly steady versus the previous six weeks."
