from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

# Natural-language body-state capture. Voice/phone first: "log leg day, right calf felt weak,
# no back pain" / "log swim 30 min, less puffy after" / "body state tense, right side guarded".
# Deterministic (no model): detect the KIND, pull the obvious fields, store the rest as notes.
# Honest about its limits , it captures what it can parse and keeps the raw phrase as notes, so
# nothing is lost even when parsing is partial.

_DURATION = re.compile(r"(\d+)\s*(?:min|mins|minutes|m)\b", re.IGNORECASE)
_DISTANCE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m|metres|meters|km)\b", re.IGNORECASE)
_RPE = re.compile(r"\b(?:rpe|effort)\s*(\d{1,2})\b", re.IGNORECASE)
_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kg|kilos?)\b", re.IGNORECASE)
_SWIM = re.compile(r"\bswim|swam|swimming|pool|lengths?\b", re.IGNORECASE)
@lru_cache(maxsize=8)
def _physio_re_for(muscle: str) -> re.Pattern[str]:
    return re.compile(r"\bphysio|mobility|stretch|serratus|" + f"{muscle} bridge" + r"|dead ?bug|pallof|"
                      r"proprioception|rib.?over.?pelvis|activation\b", re.IGNORECASE)


def _physio_re() -> re.Pattern[str]:
    """Physio cues, including the bridge drill for the profile's primary muscle (body.primary_muscle)."""
    from .body_state import body_vocab
    return _physio_re_for(body_vocab("primary_muscle"))
_WORKOUT = re.compile(r"\b(workout|gym|lift|trained|training|session|leg day|push|pull|"
                      r"upper|lower|legs|chest|back day|arms|delts|squat|deadlift|bench)\b",
                      re.IGNORECASE)
_BODYCOMP = re.compile(r"\b(weigh|weighed|weight|body ?fat|lean mass|muscle mass|waist|hip)\b",
                       re.IGNORECASE)
_BODYSTATE = re.compile(r"\b(body ?state|feel|feeling|tense|tension|tired|sore|pain|puffy|"
                        r"puffiness|energy|mood|guarded|groggy|stiff)\b", re.IGNORECASE)


def parse_body_log(text: str) -> dict[str, Any] | None:
    """Detect kind + extract fields from a logging phrase. Returns {kind, fields} or None if it
    doesn't look like a body log at all."""
    if not text:
        return None
    low = text.lower()
    # strip a leading "log"/"record"
    phrase = re.sub(r"^\s*(log|record|note|add)\s+", "", text, flags=re.IGNORECASE).strip()

    # order matters: swim/physio are more specific than generic workout
    if _SWIM.search(low):
        return {"kind": "swim", "fields": _swim_fields(phrase, low)}
    if _physio_re().search(low):
        return {"kind": "physio", "fields": _physio_fields(phrase, low)}
    if _BODYCOMP.search(low) and _WEIGHT.search(low):
        return {"kind": "body_comp", "fields": _body_comp_fields(phrase, low)}
    if _WORKOUT.search(low):
        return {"kind": "workout", "fields": _workout_fields(phrase, low)}
    if _BODYSTATE.search(low):
        return {"kind": "body_state", "fields": _body_state_fields(phrase, low)}
    return None


def _dur(low: str) -> int | None:
    m = _DURATION.search(low)
    return int(m.group(1)) if m else None


def _has_pain(low: str) -> bool | None:
    if re.search(r"\bno (back )?pain|pain.?free|no soreness\b", low):
        return False
    if re.search(r"\bpain|hurt|sore|ache\b", low):
        return True
    return None


def _workout_fields(phrase, low) -> dict[str, Any]:
    focus = ""
    from .body_state import body_vocab
    for f in ("legs", "leg day", "lower", "upper", "push", "pull", "chest", "back", "arms", "delts",
              body_vocab("primary_muscle_group")):
        if f in low:
            focus = f
            break
    rpe = None
    m = _RPE.search(low)
    if m:
        rpe = float(m.group(1))
    pain = _has_pain(low)
    return {"focus": focus, "duration_min": _dur(low), "rpe": rpe,
            "pain_flag": bool(pain) if pain is not None else False, "notes": phrase}


def _swim_fields(phrase, low) -> dict[str, Any]:
    yoga = bool(re.search(r"\bsauna\b", low))
    # "less puffy after" / "more puffy"
    puff_after = None
    if re.search(r"less puffy|puffiness down|deflated", low):
        puff_after = 2
    elif re.search(r"more puffy|puffier", low):
        puff_after = 4
    dist = None
    m = _DISTANCE.search(low)
    if m:
        val = float(m.group(1))
        dist = int(val * 1000) if "km" in low else int(val)
    return {"duration_min": _dur(low), "distance_m": dist, "paired_sauna": yoga,
            "puffiness_after": puff_after, "notes": phrase}


def _physio_fields(phrase, low) -> dict[str, Any]:
    focus = ""
    from .body_state import body_vocab
    muscle = body_vocab("primary_muscle")
    for f in (f"right {muscle}", f"left {muscle}", muscle, "serratus", "lower trap", "hamstring",
              "oblique", "lumbar", "neck", body_vocab("posture_cue"), "hip flexor"):
        if f in low:
            focus = f
            break
    effect = ""
    if re.search(r"better|helped|looser|softer|released|easier", low):
        effect = "helped"
    elif re.search(r"no change|same|didn'?t help", low):
        effect = "no change"
    return {"focus": focus, "duration_min": _dur(low), "effect": effect, "notes": phrase}


def _body_comp_fields(phrase, low) -> dict[str, Any]:
    weight = None
    m = _WEIGHT.search(low)
    if m:
        weight = float(m.group(1))
    # consumer scale numbers are noisy , default confidence reflects that
    conf = "noisy" if re.search(r"scale|smart ?scale", low) else "medium"
    return {"weight_kg": weight, "source": "manual", "confidence": conf, "notes": phrase}


_STATE_TAGS = ["tense", "guarded", "tired", "groggy", "stiff", "sore", "puffy", "anxious",
               "overwhelmed", "migraine", "calm", "energised", "grounded"]


def _body_state_fields(phrase, low) -> dict[str, Any]:
    tags = [t for t in _STATE_TAGS if re.search(rf"\b{t}\b", low)]
    right_left = ""
    if re.search(r"right (side )?(guarded|tight|tense|dominant|weak)", low):
        right_left = "right guarded"
    elif re.search(r"left (side )?(guarded|tight|tense|dominant|weak)", low):
        right_left = "left guarded"
    # simple 0-5 scores if phrased "tension 4"
    tension = _named_score(low, r"tension")
    energy = _named_score(low, r"energy")
    pain = _named_score(low, r"pain")
    sleep = None
    sm = re.search(r"slept\s*(\d+(?:\.\d+)?)\s*h", low) or re.search(r"(\d+(?:\.\d+)?)\s*h(?:ours)? sleep", low)
    if sm:
        sleep = float(sm.group(1))
    return {"tags": tags, "right_left": right_left, "tension": tension, "energy": energy,
            "pain": pain, "sleep_hours": sleep, "notes": phrase}


def _named_score(low: str, name: str) -> int | None:
    m = re.search(rf"{name}\s*(\d)\b", low)
    return int(m.group(1)) if m else None


def log_from_text(text: str) -> dict[str, Any]:
    """Parse + write to the ledger. Returns {ok, kind, say} with an honest confirmation of what
    was captured (and a nudge if it could only get the freeform note)."""
    parsed = parse_body_log(text)
    if not parsed:
        from .body_state import body_vocab
        return {"ok": False, "say": "I couldn't tell what to log. Try 'log swim 30 minutes', "
                f"'log physio right {body_vocab('primary_muscle')}, helped', or 'body state: tense, right side guarded'."}
    from . import body_state as BS
    kind = parsed["kind"]
    fields = {k: v for k, v in parsed["fields"].items() if v not in (None, "", [])}
    fn = {"workout": BS.log_workout, "swim": BS.log_swim, "physio": BS.log_physio,
          "body_comp": BS.log_body_comp, "body_state": BS.log_body_state}[kind]
    fn(**fields)
    return {"ok": True, "kind": kind, "say": _confirm(kind, fields)}


def _confirm(kind: str, fields: dict[str, Any]) -> str:
    label = {"workout": "workout", "swim": "swim", "physio": "physio session",
             "body_comp": "body measurement", "body_state": "body-state note"}[kind]
    bits = []
    if fields.get("focus"):
        bits.append(fields["focus"])
    if fields.get("duration_min"):
        bits.append(f"{fields['duration_min']} min")
    if fields.get("paired_sauna"):
        from .body_state import body_vocab
        bits.append(f"with {body_vocab('paired_recovery')}")
    if fields.get("tags"):
        bits.append("feeling " + ", ".join(fields["tags"]))
    if fields.get("effect") == "helped":
        bits.append("helped")
    detail = (", " + ", ".join(bits)) if bits else ""
    return f"Logged your {label}{detail}."
