from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

# Body voice: speak the body-state estimate in the USER'S register, not sports-science.
#
# The correction that prompted this: "how loaded am I" / "load signal elevated" is too generic
# and clinical. The user's actual body-language is concrete , puffy, lean, held, guarded, tense,
# fried-or-just-tense, swim day or reset day, right-side weird, good body day. Two jobs:
#   1. RE-VOICE the existing estimate into that register.
#   2. Answer FOCUSED intent variants (swim-or-lift, am-I-puffy, do-I-need-physio, right-side,
#      best session) from REAL logged data , or the honest hypothesis when there's no baseline.
#
# Honesty boundary held firmly: things that need data layers we haven't built (delt/calf
# progression trends, MFP food effects, journalling/proprioception-reset flows) are NOT faked.
# The router can recognise them; this presenter says "not built yet, here's what I can tell you"
# rather than bluffing an analysis over a ledger that has no set-data or nutrition importer.


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- the general "how's my body today?" answer, re-voiced ---------------------

# Band -> a plain description of the signal. Not "moderate load", not a borrowed mood-word;
# just what the recovery signal is, in concrete English ("a bit below your usual").
_BAND_PHRASE = {
    "low": "Recovery looks fine today.",
    "moderate": "A bit below your usual, but okay to train.",
    "elevated": "You're under-recovered today.",
    "high": "You're well under-recovered today.",
}

def _band_option(band: str) -> str:
    """The suggested option for a band, worded with the profile's body vocabulary (see body_state)."""
    from .body_state import body_vocab
    swap, paired = body_vocab("gentle_swap"), body_vocab("paired_recovery")
    return {
        "low": "You're good to train as planned.",
        "moderate": f"Either a normal session you keep honest, or swap to {swap} if you'd rather go gentle.",
        "elevated": f"I'd lean {swap} or controlled upper-body over anything heavy.",
        "high": f"Reset over training: a short swim, {paired}, a walk, or a grounding/physio primer.",
    }.get(band, "")


def body_today() -> dict[str, Any]:
    """'How's my body today?' in the user's register. Re-voices the estimate; never says
    'load signal' or diagnoses."""
    from .body_readiness import load_estimate
    est = load_estimate()
    if est["mode"] == "hypothesis":
        # no baseline yet , stay honest, but in the right register
        speech = ("I can't read your body personally yet, not enough of your own history. " +
                  _hypothesis_tail(est))
        return {"speech": speech, "mode": "hypothesis", "headline": "Not enough history yet"}

    band = est["band"]
    headline = _BAND_PHRASE.get(band, "Here's your body today.")
    why = ""
    if est.get("reasons"):
        why = " " + _revoice_reasons(est["reasons"])
    helped = ""
    if est.get("protective"):
        helped = " In your favour: " + _join(est["protective"]) + "."
    option = " " + _band_option(band)
    avoid = _avoid_clause(band)
    return {"speech": headline + why + helped + option + avoid,
            "mode": "personalised", "band": band, "headline": headline}


def _revoice_reasons(reasons: list[str]) -> str:
    """Keep the honest baseline-relative reasons, but soften the framing into the user's voice."""
    # the reasons already say "HRV is X% below your recent baseline" etc. , that's good and honest.
    # just stitch them naturally.
    return "What I'm seeing: " + _join(reasons[:3]) + "."


def _avoid_clause(band: str) -> str:
    if band in ("elevated", "high"):
        return " I'd avoid heavy lower-body hinge stuff today unless you feel better after a primer."
    return ""


def _hypothesis_tail(est: dict[str, Any]) -> str:
    hyps = est.get("hypotheses", [])
    if not hyps:
        return "Log a few body-state notes or do a quick check-in and I'll start learning your patterns."
    pat = hyps[0].get("general_pattern", "")
    return (f"Generally, {pat}, but that's a population pattern, not a read on you. "
            "Tell me how you feel (a quick check-in) and I'll start learning what's true for you.")


# --- focused intents (grounded in real logs) ---------------------------------

def swim_or_lift() -> dict[str, Any]:
    """'Should I swim or lift?', answered from the estimate + how long since the last swim +
    whether swimming has helped the user before (real logged before/after)."""
    from .body_readiness import load_estimate
    from .body_state import last_of
    est = load_estimate()
    band = est.get("band")
    last_swim = last_of("swim")
    days_since = _days_since(last_swim)
    swim_helps = _swim_has_helped()

    if est["mode"] == "hypothesis":
        lead = "I don't know your baseline yet, so this is a general steer, not a personal read."
    else:
        lead = {"low": "Recovery looks fine, so lifting is on the table.",
                "moderate": "You're a bit below your usual, so either could work.",
                "elevated": "You're under-recovered, so I'd lean swim.",
                "high": "You're well under-recovered, so swim or reset over lifting."}.get(band, "")

    bits = [lead]
    if days_since is not None and days_since >= 4:
        bits.append(f"It's been {days_since} days since you swam, which might be reason enough.")
    if swim_helps:
        bits.append("Your own logs show swimming has helped your tension/puffiness before.")
    verdict = ("Lean swim today." if band in ("elevated", "high") or (days_since or 0) >= 5
               else "Either works; pick by how you feel after a warm-up.")
    bits.append(verdict)
    return {"speech": " ".join(b for b in bits if b), "band": band, "days_since_swim": days_since}


def puffiness_today() -> dict[str, Any]:
    """'Am I puffy today / what helps de-puff?', from logged puffiness + the walk/stretch pattern.
    Honest: puffiness has many causes; we report the signal and what's helped, we don't diagnose
    water vs salt vs inflammation (we don't have nutrition data yet)."""
    from .body_state import last_of
    today = _todays_state()
    puff = today.get("puffiness") if today else None
    last_swim = last_of("swim")
    days_since = _days_since(last_swim)
    deplan = _depuff_plan(days_since)

    if puff is None:
        risk = "I don't have a puffiness note from you today."
    elif puff >= 4:
        risk = "You flagged feeling puffy today."
    elif puff >= 2:
        risk = "Mild puffiness noted today."
    else:
        risk = "You're not flagging much puffiness today."

    # honest about causes: we can't separate water/salt/sleep/inflammation without nutrition data
    caveat = ("I can't split the cause into water, salt, sleep or food yet, that needs nutrition "
              "logging I don't have. But here's the low-friction move:")
    return {"speech": f"{risk} {caveat} {deplan}", "puffiness": puff}


def _depuff_plan(days_since_swim: int | None) -> str:
    # State-regulation, not food restriction: puffiness advice tied to appearance plus a
    # "lighter food" nudge can reinforce body-checking or restriction, especially right after
    # admitting the cause is unknown (external audit B3). Hydration, gentle movement, yoga if
    # wanted, and rest. Nothing here asks anyone to eat less.
    if days_since_swim is None or days_since_swim >= 3:
        from .body_state import body_vocab
        return (f"water, a short swim and {body_vocab('paired_recovery')} if you fancy it, and an early-ish night. "
                "Nothing here needs any change to what you eat. That combination is usually "
                "your quickest reset.")
    return ("you swam recently, so keep it simple: water, a gentle walk, and rest when you "
            "can. No food changes needed.")


def right_side() -> dict[str, Any]:
    """'Is my right side being weird again?', from logged right_left state + recent physio focus."""
    from .body_state import list_entries
    today = _todays_state()
    rl = (today or {}).get("right_left", "")
    recent_states = list_entries("body_state", since=_now() - timedelta(days=7), limit=20)
    right_flags = sum(1 for s in recent_states if "right" in (s.get("right_left") or "").lower())
    last_physio = _last_physio_focus()

    if rl and "right" in rl.lower():
        lead = "Yes, you've flagged right-side guarding today."
    elif right_flags >= 2:
        lead = f"Not flagged today, but you've noted right-side stuff {right_flags} times this week."
    else:
        lead = "Nothing pointing to right-side weirdness today."

    step = ""
    if (rl and "right" in rl.lower()) or right_flags >= 2:
        from .body_state import body_vocab
        step = (f" Before lifting I'd do a short {body_vocab('primer_name')} ({body_vocab('primary_muscle')} bridge, "
                "serratus reach), and keep hinge patterns controlled or skip them.")
        if last_physio:
            step += f" Last physio you worked {last_physio}."
    return {"speech": lead + step, "right_flags_week": right_flags}


def best_session() -> dict[str, Any]:
    """'What's the best session today?', the estimate's option, in the user's register, plus a
    swim nudge if it's been a while."""
    t = body_today()
    from .body_state import last_of
    days_since = _days_since(last_of("swim"))
    tail = ""
    if days_since is not None and days_since >= 5:
        tail = f" And it's been {days_since} days since a swim, might be the high-ROI move."
    return {"speech": t["speech"] + tail}


def need_physio() -> dict[str, Any]:
    """'Do I need physio?', from right-side flags, tension, and time since last physio."""
    from .body_state import last_of
    today = _todays_state()
    tension = (today or {}).get("tension")
    rl = (today or {}).get("right_left", "")
    last_physio = last_of("physio")
    days_since = _days_since(last_physio)

    reasons = []
    if rl and "guard" in rl.lower():
        reasons.append("you've flagged guarding today")
    if tension is not None and tension >= 4:
        reasons.append("tension is high")
    if days_since is not None and days_since >= 5:
        reasons.append(f"it's been {days_since} days since physio")

    if reasons:
        from .body_state import body_vocab
        focus = _last_physio_focus() or body_vocab("physio_focus_default")
        return {"speech": f"Worth a short reset, {_join(reasons)}. A few minutes on {focus} "
                f"({body_vocab('primary_muscle')} bridge, serratus reach) before training would help."}
    return {"speech": "No strong physio signal today. A quick primer never hurts before legs, but "
            "you're not flagging anything that needs it."}


# --- honestly-not-built-yet (recognise, don't fake) --------------------------

def not_built_yet(topic: str) -> dict[str, Any]:
    """For intents that need layers we haven't built. Progression is now REAL when Alpha data is
    imported (we route there first); this remains the honest fallback for nutrition, and for
    progression when nothing has been imported yet."""
    if topic == "progression":
        # if training data exists, this shouldn't be reached , but answer for real if so
        from .body_progression import has_training_data, muscle_trend
        if has_training_data():
            return muscle_trend()
    msg = {
        "progression": ("I can't track per-muscle progression yet, import your Alpha Progression "
                        "export (sessions + sets CSV) and I'll track volume, top weights and your "
                        "muscle trend for real. I can log sessions and how they felt meanwhile."),
        "nutrition": ("I don't have your food data yet, MFP import isn't wired in this version. "
                      "So I can't tell you if it's protein, salt or carbs. I can log how you feel "
                      "and what helps."),
        "journalling": ("The guided emotional-literacy and proprioception-reset flows aren't built "
                        "yet, that's a planned slice. For now I can log a body-state note if you "
                        "tell me how you feel and where."),
    }.get(topic, "That part isn't built yet, but I can log how your body feels right now.")
    return {"speech": msg, "not_built": topic}


def progression_answer(text: str) -> dict[str, Any]:
    """Route a progression-style question to the right real answer over imported Alpha data. Uses
    the richer detail layer (per-exercise est-1RM trends, moving/stuck, phase), not the crude
    max-weight signal. Falls back to the honest not-built message when nothing is imported."""
    from .body_progression import (has_training_data, current_plan, recent_training,
                                   muscle_trend, overdoing)
    from . import body_progression_detail as PD
    if not has_training_data():
        return not_built_yet("progression")
    low = (text or "").lower()
    import re as _re
    from .body_state import body_vocab
    muscle, muscle_group = body_vocab("primary_muscle"), body_vocab("primary_muscle_group")
    group_map = {"delt": "delts", "shoulder": "delts", muscle: muscle_group, "quad": "quads",
                 "ham": "hamstrings", "bicep": "biceps", "tricep": "triceps", "arm": "biceps",
                 "chest": "chest", "back": "back_lats", "lat": "back_lats", "calf": "calves",
                 "calve": "calves", "core": "core_proprio"}
    named = None
    for k, v in group_map.items():
        if k in low:
            named = v
            break
    # "overdoing X" is a volume-balance question, not a progression-trend one , keep it distinct
    if _re.search(r"overdo|too much|too many|avoid (building|getting)", low):
        return overdoing(named or "quads")
    if _re.search(r"\bplan\b|current split|what am i (doing|running)", low):
        return current_plan()
    if _re.search(r"recently|last (week|few)|what have i (trained|done)", low):
        # recent training + what phase the block is
        rt = recent_training()
        phase = PD.training_phase()
        rt["speech"] = rt["speech"] + " " + phase["speech"]
        return rt
    if _re.search(r"muscle trend|building (the right )?muscle|gaining muscle|muscle or water", low):
        # body-comp trend + strength phase, the fuller picture
        mt = muscle_trend()
        phase = PD.training_phase()
        mt["speech"] = phase["speech"] + " " + mt["speech"]
        return mt
    if named:
        # the rich per-group breakdown: which lifts moving vs stuck
        return PD.group_progression(named)
    # generic "am I overdoing / progressing" with no named group , give phase + a quick scan
    phase = PD.training_phase()
    return {"speech": phase["speech"] + f" Ask about a specific area (delts, {muscle_group}, hamstrings...) "
            "and I'll tell you which lifts are moving or stuck.", "have_data": True}


# --- helpers -----------------------------------------------------------------

def _todays_state() -> dict[str, Any] | None:
    from .body_state import list_entries
    today = _now().date()
    for r in list_entries("body_state", limit=5):
        try:
            d = datetime.fromisoformat(r["at"].replace("Z", "+00:00")).date()
            if d == today:
                return r
        except Exception:
            continue
    return None


def _days_since(entry: dict[str, Any] | None) -> int | None:
    if not entry:
        return None
    try:
        dt = datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
        return (_now() - dt).days
    except Exception:
        return None


def _swim_has_helped() -> bool:
    from .body_state import list_entries
    for s in list_entries("swim", limit=20):
        pb, pa = s.get("puffiness_before"), s.get("puffiness_after")
        tb, ta = s.get("tension_before"), s.get("tension_after")
        if (pb is not None and pa is not None and pa < pb) or \
           (tb is not None and ta is not None and ta < tb):
            return True
    return False


def _last_physio_focus() -> str:
    from .body_state import last_of
    p = last_of("physio")
    return (p or {}).get("focus", "") if p else ""


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
