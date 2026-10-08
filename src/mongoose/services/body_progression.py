from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

# Query layer over imported Alpha Progression data. This is what body_voice calls instead of
# not_built_yet("progression") once real data is imported. Every answer is grounded in the
# imported sets/sessions/body-comp, and degrades honestly to "no training data imported yet" when
# the ledger is empty (so it never fabricates a trend).
#
# Honesty: muscle-group VOLUME and set-counts are real and exact (they're just sums of what you
# logged). Body COMPOSITION is directional (BIA hydration-sensitive) , always framed as a trend,
# never "you gained X kg of muscle".

_RECENT_DAYS = 42        # "recently" / "current plan" window
_TREND_DAYS = 90         # progression comparison window


def _now() -> datetime:
    return datetime.now(timezone.utc)


def has_training_data() -> bool:
    from ..db import connect
    try:
        with connect() as con:
            r = con.execute("SELECT COUNT(*) c FROM bs_alpha_sets").fetchone()
            return (r["c"] if r else 0) > 0
    except Exception:
        return False


def has_body_comp() -> bool:
    from .body_state import list_entries
    return any(e.get("source") == "alpha" for e in list_entries("body_comp", limit=50))


# --- muscle-group balance (overdoing quads? enough delts?) -------------------

def muscle_group_volumes(days: int = _TREND_DAYS) -> list[dict[str, Any]]:
    """Set-count and volume per muscle group over the window, real, exact sums."""
    from ..db import connect
    since = (_now() - timedelta(days=days)).isoformat()
    with connect() as con:
        rows = con.execute(
            "SELECT muscle_group, COUNT(*) sets, "
            "       ROUND(SUM(COALESCE(kg,0)*COALESCE(reps,0)),0) volume "
            "FROM bs_alpha_sets WHERE at >= ? GROUP BY muscle_group ORDER BY sets DESC",
            (since,)).fetchall()
    return [{"group": r["muscle_group"], "sets": r["sets"], "volume": r["volume"] or 0}
            for r in rows]


def group_focus(group: str) -> dict[str, Any]:
    """Answer 'are my <group> progressing' / 'am I overdoing <group>', recent sets + whether top
    weight is trending up."""
    from ..db import connect
    if not has_training_data():
        return {"have_data": False,
                "speech": "No training data imported yet, drop your Alpha Progression export in "
                          "and I'll track this for real."}
    since_recent = (_now() - timedelta(days=_RECENT_DAYS)).isoformat()
    since_prev = (_now() - timedelta(days=_RECENT_DAYS * 2)).isoformat()
    with connect() as con:
        recent = con.execute(
            "SELECT COUNT(*) sets, ROUND(AVG(kg),1) avg_kg, MAX(kg) max_kg "
            "FROM bs_alpha_sets WHERE muscle_group=? AND at>=?", (group, since_recent)).fetchone()
        prev = con.execute(
            "SELECT MAX(kg) max_kg FROM bs_alpha_sets WHERE muscle_group=? AND at>=? AND at<?",
            (group, since_prev, since_recent)).fetchone()
        # ranking among groups by recent set-count, to judge over/under-doing
        ranked = con.execute(
            "SELECT muscle_group, COUNT(*) c FROM bs_alpha_sets WHERE at>=? "
            "GROUP BY muscle_group ORDER BY c DESC", (since_recent,)).fetchall()
    recent_sets = recent["sets"] if recent else 0
    order = [r["muscle_group"] for r in ranked]
    rank = order.index(group) + 1 if group in order else None
    total_groups = len(order)

    # progression: recent max vs previous-period max
    trend = "flat"
    if recent and prev and recent["max_kg"] and prev["max_kg"]:
        if recent["max_kg"] > prev["max_kg"]:
            trend = "up"
        elif recent["max_kg"] < prev["max_kg"]:
            trend = "down"

    return {"have_data": True, "group": group, "recent_sets": recent_sets,
            "rank": rank, "total_groups": total_groups, "trend": trend,
            "recent_max_kg": recent["max_kg"] if recent else None,
            "speech": _group_speech(group, recent_sets, rank, total_groups, trend, recent)}


def _group_speech(group, sets, rank, total, trend, recent) -> str:
    label = group.replace("_", "/")
    if sets == 0:
        return (f"You haven't trained {label} in the last six weeks, that's the gap if you want "
                "to bring them on.")
    vol = ""
    if trend == "up":
        vol = f" Top weight is trending up (recently {recent['max_kg']}kg), so they're progressing."
    elif trend == "down":
        vol = " Top weight has dipped versus the previous block."
    else:
        vol = " Top weight is roughly flat."
    place = ""
    if rank and total:
        if rank <= 2:
            place = f" They're one of your most-trained groups right now ({rank} of {total})."
        elif rank >= total - 1:
            place = f" They're among your least-trained ({rank} of {total}), room for more if they're a priority."
    return f"{sets} sets of {label} in the last six weeks.{vol}{place}"


def overdoing(group: str) -> dict[str, Any]:
    """'Am I overdoing <group>?', high relative set-count is the signal."""
    f = group_focus(group)
    if not f.get("have_data"):
        return f
    rank, total = f.get("rank"), f.get("total_groups")
    if rank and rank <= 2:
        f["speech"] = (f"{f['recent_sets']} sets of {group} recently, that's one of your highest "
                       f"volumes ({rank} of {total}). If you're trying to avoid building it, "
                       "that's more than you need.")
    elif rank:
        f["speech"] = (f"{f['recent_sets']} sets of {group} recently, mid-pack ({rank} of "
                       f"{total}), not obviously overdone.")
    return f


# --- current plan ------------------------------------------------------------

def current_plan() -> dict[str, Any]:
    from ..db import connect
    if not has_training_data():
        return {"have_data": False,
                "speech": "No plan imported yet, import your Alpha Progression export and I'll "
                          "know your current split."}
    with connect() as con:
        rows = con.execute("SELECT exercise, muscle_group FROM bs_alpha_plan ORDER BY muscle_group").fetchall()
    if not rows:
        return {"have_data": False, "speech": "I don't have a current plan derived yet."}
    exercises = [r["exercise"] for r in rows]
    groups = sorted(set(r["muscle_group"] for r in rows if r["muscle_group"] != "other"))
    return {"have_data": True, "exercises": exercises, "groups": groups,
            "speech": f"Your current plan has {len(exercises)} exercises covering "
                      f"{', '.join(groups)}: {', '.join(exercises)}."}


def recent_training(days: int = 14) -> dict[str, Any]:
    from ..db import connect
    if not has_training_data():
        return {"have_data": False, "speech": "No training data imported yet."}
    since = (_now() - timedelta(days=days)).isoformat()
    with connect() as con:
        sessions = con.execute(
            "SELECT title, at FROM bs_alpha_sessions WHERE at>=? ORDER BY at DESC", (since,)).fetchall()
    if not sessions:
        return {"have_data": True, "speech": f"No sessions logged in the last {days} days."}
    n = len(sessions)
    last = sessions[0]["at"][:10]
    return {"have_data": True, "n": n, "last": last,
            "speech": f"{n} session{'s' if n != 1 else ''} in the last {days} days, most recent on {last}."}


# --- body composition trend (DIRECTIONAL) ------------------------------------

def muscle_trend() -> dict[str, Any]:
    """'Am I building muscle / what's the muscle trend?', DIRECTIONAL from body-comp, never exact."""
    from .body_state import list_entries
    comps = [e for e in list_entries("body_comp", since=_now() - timedelta(days=180), limit=100)
             if e.get("muscle_mass_kg") is not None]
    if len(comps) < 2:
        return {"have_data": False,
                "speech": "Not enough body-composition history imported to call a muscle trend yet."}
    comps.sort(key=lambda e: e["at"])
    first, last = comps[0], comps[-1]
    dm = last["muscle_mass_kg"] - first["muscle_mass_kg"]
    dw = (last.get("weight_kg") or 0) - (first.get("weight_kg") or 0)
    direction = "up" if dm > 0.3 else ("down" if dm < -0.3 else "roughly flat")
    # honest framing , directional, hydration caveat
    span = f"{first['at'][:10]} to {last['at'][:10]}"
    note = (f"Skeletal muscle mass reading is {direction} over {span} "
            f"({first['muscle_mass_kg']:.1f} to {last['muscle_mass_kg']:.1f}kg). "
            "Treat that as directional, BIA/scale muscle figures swing with hydration, so it's a "
            "trend, not an exact gain.")
    if dw and abs(dw) > 1:
        note += f" Weight moved {dw:+.1f}kg over the same span, so some of this is food/water too."
    return {"have_data": True, "direction": direction, "delta_kg": round(dm, 1), "speech": note}
