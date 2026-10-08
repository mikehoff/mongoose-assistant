from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

# Body readiness / load estimate , the honest core.
#
# THREE MODES (never just a number, never a diagnosis):
#
#  1. PERSONALISED , enough of YOUR baseline exists, so the read is relative to your own median:
#     "HRV is 18% below your 28-day median; resting HR is up; sleep was short."
#
#  2. HYPOTHESIS , not enough of your baseline yet. Instead of a hollow "uncertain", name the
#     GENERAL pattern research broadly supports, EXPLICITLY flagged as not-yet-personalised and
#     framed as a falsifiable hypothesis to test against your own logs:
#     "I can't read today personally yet. Generally, short sleep + an HRV dip is associated with
#      higher next-day strain , that's a population pattern, not a read on you. Tell me how loaded
#      you feel and I'll learn whether it holds for you."
#
#  3. CONFIRMED_FOR_YOU , a hypothesis that your own check-ins have borne out over time:
#     "For you specifically, low protein has preceded worse training , held across N check-ins."
#
# Discipline (from the source docs, which cite that sleep/HRV associations are weak and
# INCONSISTENT across individuals): personal baseline over population norms; within-person change;
# transparent weighted rules, not ML; always show WHY; never diagnose; smallest useful next step.
# Research hypotheses state direction qualitatively ("research broadly associates...") , we do NOT
# fabricate specific citations or precise effect sizes.

# How much of the user's own history we need before we'll personalise a given signal.
_MIN_BASELINE_POINTS = 5      # need >= 5 prior readings of a metric to call a baseline "yours"
_BASELINE_DAYS = 28          # the window for "your normal"
_CONFIRM_MIN_CHECKINS = 4    # a hypothesis is "confirmed for you" after this many supporting check-ins


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


# --- baselines (YOUR normal, not a population norm) --------------------------

def _metric_series(metric: str, *, exclude_today: bool = True) -> list[float]:
    """Prior values of a body_state metric over the baseline window, the basis for 'your normal'."""
    from .body_state import list_entries
    since = _now() - timedelta(days=_BASELINE_DAYS)
    rows = list_entries("body_state", since=since, limit=500)
    today = _now().date()
    out = []
    for r in rows:
        v = r.get(metric)
        if v is None:
            continue
        if exclude_today:
            try:
                d = datetime.fromisoformat(r["at"].replace("Z", "+00:00")).date()
                if d == today:
                    continue
            except Exception:
                pass
        out.append(float(v))
    return out


def _baseline(metric: str) -> dict[str, Any]:
    """Return {median, n, has_baseline} for a metric. has_baseline is False when there isn't
    enough of the user's own history to personalise, which triggers HYPOTHESIS mode."""
    series = _metric_series(metric)
    med = _median(series)
    return {"median": med, "n": len(series), "has_baseline": len(series) >= _MIN_BASELINE_POINTS}


# --- the estimate ------------------------------------------------------------

def load_estimate(*, now: datetime | None = None) -> dict[str, Any]:
    """The body load/readiness estimate. Picks the honest mode based on how much of the user's
    own baseline exists, and always returns reasoning + a smallest-useful-step, never a bare score
    and never a diagnosis."""
    now = now or _now()
    from .body_state import recent_all

    # today's signals (most recent body_state log today, plus today's workouts/swims/physio)
    todays_state = _todays_state(now)
    recent = recent_all(days=3)

    # Decide mode: do we have ANY personal baseline to compare against?
    baselines = {m: _baseline(m) for m in ("hrv", "resting_hr", "sleep_hours", "load", "tension")}
    have_any_baseline = any(b["has_baseline"] for b in baselines.values())
    have_today_signals = bool(todays_state) or any(recent.get(k) for k in ("workout", "swim", "physio"))

    if have_any_baseline and have_today_signals:
        return _personalised_estimate(todays_state, recent, baselines, now)
    # not enough of the user's own data , offer research-framed hypotheses instead of "uncertain"
    return _hypothesis_estimate(todays_state, recent, baselines, now)


def _todays_state(now: datetime) -> dict[str, Any] | None:
    from .body_state import list_entries
    today = now.date()
    for r in list_entries("body_state", limit=5):
        try:
            d = datetime.fromisoformat(r["at"].replace("Z", "+00:00")).date()
            if d == today:
                return r
        except Exception:
            continue
    return None


# --- mode 1: personalised ----------------------------------------------------

def _personalised_estimate(state, recent, baselines, now) -> dict[str, Any]:
    """Weighted, transparent rules over the user's OWN baseline. Points accrue with reasons; the
    output leads with WHY, not the number."""
    points = 0.0
    reasons: list[str] = []
    protective: list[str] = []

    def cmp_to_baseline(metric, value, higher_is_worse, label):
        nonlocal points
        b = baselines.get(metric, {})
        if value is None or not b.get("has_baseline") or b.get("median") is None:
            return
        med = b["median"]
        if med == 0:
            return
        delta_pct = (value - med) / abs(med) * 100
        worse = delta_pct > 0 if higher_is_worse else delta_pct < 0
        mag = abs(delta_pct)
        if worse and mag >= 8:
            inc = 2 if mag >= 18 else 1
            points += inc
            direction = "above" if value > med else "below"
            reasons.append(f"{label} is {abs(round(delta_pct))}% {direction} your recent baseline")

    if state:
        cmp_to_baseline("hrv", state.get("hrv"), higher_is_worse=False, label="HRV")
        cmp_to_baseline("resting_hr", state.get("resting_hr"), higher_is_worse=True, label="resting heart rate")
        cmp_to_baseline("sleep_hours", state.get("sleep_hours"), higher_is_worse=False, label="sleep")
        # subjective signals are first-class
        if (state.get("tension") or 0) >= 4:
            points += 1
            reasons.append("you logged high tension")
        if (state.get("pain") or 0) >= 3:
            points += 1
            reasons.append("you logged pain")
        for tag in (state.get("tags") or []):
            if tag.lower() in ("migraine", "overwhelmed", "derealised", "tense", "guarded"):
                points += 2
                reasons.append(f"you noted feeling {tag.lower()}")
                break

    # training load: a hard session or pain flag yesterday/today
    for w in recent.get("workout", []):
        if (w.get("rpe") or 0) >= 8 or w.get("pain_flag"):
            points += 1
            reasons.append("a hard or painful training session recently")
            break

    # protective actions reduce load (the docs: notice what HELPS, not just load)
    if recent.get("swim"):
        points -= 1
        protective.append("a recent swim")
    if recent.get("physio"):
        points -= 1
        protective.append("a recent physio session")
    if any(s.get("paired_sauna") for s in recent.get("swim", [])):
        from .body_state import body_vocab
        points -= 1
        protective.append(body_vocab("paired_recovery"))

    points = max(0.0, points)
    band = _band(points)
    return {
        "mode": "personalised",
        "band": band,
        "confidence": "medium",
        "reasons": reasons,
        "protective": protective,
        "next_step": _next_step(band, state, recent),
        "disclaimer": "This is an inference from your own patterns, not a diagnosis.",
        "speech": _speak_personalised(band, reasons, protective, state, recent),
    }


def _band(points: float) -> str:
    if points <= 1:
        return "low"
    if points <= 3:
        return "moderate"
    if points <= 5:
        return "elevated"
    return "high"


# --- mode 2: research-framed hypothesis -------------------------------------

# General patterns research broadly supports. STRENGTH is qualitative on purpose , we don't
# fabricate citations or effect sizes. Each is a HYPOTHESIS to test against the user's own data.
_HYPOTHESES = [
    {"id": "sleep_strain",
     "when": lambda s: (s or {}).get("sleep_hours") is not None and s["sleep_hours"] < 6.5,
     "pattern": "short sleep is broadly associated with higher next-day strain and lower training quality",
     "strength": "fairly consistent in the literature"},
    {"id": "hrv_stress",
     "when": lambda s: (s or {}).get("hrv") is not None,
     "pattern": "a drop in HRV relative to your own norm is often associated with higher stress load",
     "strength": "supported in aggregate, but weak and inconsistent between individuals"},
    {"id": "protein_recovery",
     "when": lambda s: True,
     "pattern": "adequate protein is broadly associated with better recovery and adaptation",
     "strength": "well supported"},
    {"id": "swim_sauna_state",
     "when": lambda s: True,
     "pattern": "light low-impact movement and heat exposure are often associated with reduced tension and perceived puffiness",
     "strength": "mixed evidence, plausible mechanism"},
]


def _hypothesis_estimate(state, recent, baselines, now) -> dict[str, Any]:
    """Not enough of the user's baseline to personalise. Offer the general patterns research
    supports, EXPLICITLY as not-yet-personalised hypotheses, and invite a check-in to start
    learning whether they hold for this person."""
    # which hypotheses are relevant to what little we know today
    relevant = [h for h in _HYPOTHESES if h["when"](state)]
    # promote any that the user's own check-ins have ALREADY started confirming
    confirmed = _confirmed_hypotheses()
    chosen = relevant[:2]

    hyps = []
    for h in chosen:
        hyps.append({
            "id": h["id"],
            "general_pattern": h["pattern"],
            "evidence_strength": h["strength"],
            "personalised": h["id"] in confirmed,
            "note": ("your own check-ins so far are consistent with this" if h["id"] in confirmed
                     else "not yet tested against your data"),
        })

    needed = _what_baseline_is_missing(baselines)
    return {
        "mode": "hypothesis",
        "band": "not_personalised",
        "confidence": "low",
        "hypotheses": hyps,
        "baseline_needed": needed,
        "next_step": "Tell me how loaded you feel right now (0-5), a quick check-in starts building "
                     "your baseline so I can read today personally instead of in general.",
        "disclaimer": "These are general research patterns, not a read on you. They're hypotheses "
                      "I'll test against your own logs over time.",
        "speech": _speak_hypothesis(hyps, needed),
    }


def _what_baseline_is_missing(baselines) -> list[str]:
    missing = []
    for m, label in (("sleep_hours", "sleep"), ("hrv", "HRV"), ("resting_hr", "resting heart rate"),
                     ("load", "how loaded you feel"), ("tension", "tension")):
        b = baselines.get(m, {})
        if not b.get("has_baseline"):
            need = _MIN_BASELINE_POINTS - b.get("n", 0)
            if need > 0:
                missing.append(f"{label} ({need} more log{'s' if need != 1 else ''})")
    return missing


# --- mode 3: confirmed-for-you (calibration learning) -----------------------

def _confirmed_hypotheses() -> set[str]:
    """Hypotheses the user's own calibration check-ins have borne out. A check-in stores, for a
    given hypothesis, whether the day matched (e.g. short sleep AND high felt-load). Once enough
    supporting check-ins accrue, the hypothesis is 'confirmed for you'."""
    try:
        import json
        from ..db import get_app_state
        raw = get_app_state("body_hypothesis_support", "")
        if not raw:
            return set()
        support = json.loads(raw)
        return {hid for hid, n in support.items() if n >= _CONFIRM_MIN_CHECKINS}
    except Exception:
        return set()


def record_checkin(*, felt_load: int, hypotheses_present: list[str] | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """A calibration check-in: the user says how loaded they actually feel (0-5). We store it as a
    body_state log AND, for each hypothesis that was 'present' today, update whether the felt-load
    matched, so hypotheses get confirmed or weakened against THIS person over time."""
    from .body_state import log_body_state
    log_body_state(load=felt_load, at=now, source="checkin")
    # update hypothesis support: a high felt-load supports load-raising hypotheses
    try:
        import json
        from ..db import get_app_state, set_app_state
        raw = get_app_state("body_hypothesis_support", "")
        support = json.loads(raw) if raw else {}
        for hid in (hypotheses_present or []):
            # felt_load >= 3 is treated as "the strain hypothesis held today"
            support[hid] = support.get(hid, 0) + (1 if felt_load >= 3 else 0)
        set_app_state("body_hypothesis_support", json.dumps(support))
    except Exception:
        pass
    return {"ok": True, "felt_load": felt_load}


# --- phrasing ----------------------------------------------------------------

def _next_step(band, state, recent) -> str:
    if band in ("elevated", "high"):
        from .body_state import body_vocab
        return (f"Do the low-friction option: a short walk, an easy swim or {body_vocab('paired_recovery')}, or one "
                "breathing/proprioception reset. I'd hold off on a heavy lower-body session unless "
                "you feel better after moving.")
    if band == "moderate":
        return "A lighter or recovery-first version of today's plan would fit. See how you feel after a warm-up."
    return "You look okay to train as planned. Keep an eye on how the first working sets feel."


def _speak_personalised(band, reasons, protective, state, recent) -> str:
    head = {"low": "Your load signal looks low today.",
            "moderate": "Your load signal looks moderate today.",
            "elevated": "Your load signal looks elevated today.",
            "high": "Your load signal looks high today."}[band]
    why = ""
    if reasons:
        why = " Why: " + _join(reasons[:4]) + "."
    prot = ""
    if protective:
        prot = " In your favour: " + _join(protective) + "."
    step = " " + _next_step(band, state, recent)
    return head + why + prot + step


def _speak_hypothesis(hyps, needed) -> str:
    lead = ("I don't have enough of your own baseline yet to read today personally, so let me be "
            "clear this is general, not a read on you.")
    body = ""
    if hyps:
        parts = []
        for h in hyps:
            tag = " (and your check-ins so far line up with this)" if h["personalised"] else ""
            parts.append(f"{h['general_pattern']}, {h['evidence_strength']}{tag}")
        body = " " + _join(parts, sep="; ") + "."
    invite = (" Tell me how loaded you feel right now, 0 to 5, and I'll start learning whether "
              "these hold for you specifically.")
    return lead + body + invite


def _join(items: list[str], sep: str = ", ") -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if sep == ", " and len(items) == 2:
        return f"{items[0]} and {items[1]}"
    if sep == ", ":
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    return sep.join(items)
