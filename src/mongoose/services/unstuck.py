from __future__ import annotations

import json
import random
from datetime import datetime, timezone, timedelta
from typing import Any

# The unstucking / active-recovery layer.
#
# The spec, in the user's own words: "doing something in a playful way without teaching means so
# much more than taking some time off something", and "rest and recovery are key but never
# passive". This layer encodes exactly that distinction , the thing generic wellness tooling gets
# wrong when it flattens recovery into "take a break / have some downtime".
#
# So this layer NEVER says rest / break / downtime / wind down / take time off. When it notices
# the user is stuck or grinding, it offers something ACTIVE, ABSORBING and AGENDA-FREE from THEIR
# repertoire (being in water for its own sake, a walk, a hobby, tinkering,
# cooking, music, making something in the flat). Recovery as something you DO, playfully , not as
# stopping.
#
# Tone (from the user's standing preferences): dry, minimal, a little provocative, never
# therapised, never sentimental. Name the grind, offer one concrete thing. No nagging, no turning
# play into a rateable task ("do 20 min of fun, score it 1-5" would betray the whole idea).
#
# Honesty: it suggests from the user's OWN list and tracks whether a thing was actually done and
# helped (via the existing body-state effect logging), so over time it learns what genuinely
# unsticks THIS person rather than asserting what "should" work.

# The restorative repertoire , the user's actual absorbing/playful things. Each is framed as a DOING,
# never as rest. `water`/`movement` overlap with the body ledger so "did it help" can be learned.
_REPERTOIRE = [
    {"id": "water", "doing": "get in the water, not for the cardio, just to be in it",
     "tags": ["swim", "water"], "kind": "movement_play"},
    {"id": "yoga", "doing": "sit in the yoga and let it cook the tension out",
     "tags": ["yoga"], "kind": "movement_play"},
    {"id": "dog", "doing": "take the dog somewhere and let the walk go wherever it goes",
     "tags": ["dog", "walk"], "kind": "movement_play"},
    {"id": "retro", "doing": "go and tinker with something pointless on a side project",
     "tags": ["gardening", "making"], "kind": "absorbing_play"},
    {"id": "cook", "doing": "cook something you actually want to eat, slowly, no recipe pressure",
     "tags": ["cooking", "making"], "kind": "absorbing_play"},
    {"id": "music", "doing": "put something on and get lost in it for a bit",
     "tags": ["music"], "kind": "absorbing_play"},
    {"id": "make", "doing": "make or fix one small thing in the flat, for the pleasure of it",
     "tags": ["making"], "kind": "absorbing_play"},
]



def _repertoire() -> list[dict[str, Any]]:
    """The repertoire as this household lives it: each entry above, with any fields the site profile
    replaces (lexicon unstuck.repertoire_overrides, {shipped id: {field: value}}, e.g. a real pet's name
    or the person's own recovery ritual)."""
    from .. import profile as _profile
    over = _profile.lexicon("unstuck.repertoire_overrides", {}) or {}
    if not over:
        return _REPERTOIRE
    return [{**item, **over[item["id"]]} if item["id"] in over else item for item in _REPERTOIRE]


# things this layer must NEVER say (the flattened, passive framings the user rejects)
_FORBIDDEN = ("rest", "take a break", "downtime", "wind down", "time off", "have a rest",
              "relax", "switch off", "do nothing")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- the stuck detector ------------------------------------------------------

def detect_stuck(*, now: datetime | None = None) -> dict[str, Any]:
    """Read the cluster that means 'more effort is not the move': under-recovered, OR grinding
    (training without progress), OR repeated tension/guarding, OR a stretch with no play logged.
    Returns {stuck: bool, signals: [...], severity}. This is a pattern read, not a single number."""
    now = now or _now()
    signals: list[str] = []

    # 1. under-recovered (from the readiness band)
    try:
        from .body_readiness import load_estimate
        est = load_estimate(now=now)
        if est.get("band") in ("elevated", "high"):
            signals.append("under-recovered")
    except Exception:
        pass

    # 2. grinding: recent training but flat/declining progress, or a deload that reads as a slog
    try:
        from .body_progression import has_training_data
        from .body_progression_detail import training_phase
        if has_training_data():
            phase = training_phase()
            if phase.get("phase") == "detraining":
                signals.append("grinding")  # volume down but still showing up , often a slog
    except Exception:
        pass

    # 3. repeated tension/guarding in recent body-state logs
    try:
        from .body_state import list_entries
        recent = list_entries("body_state", since=now - timedelta(days=7), limit=20)
        tense = sum(1 for s in recent
                    if (s.get("tension") or 0) >= 4
                    or "guard" in (s.get("right_left") or "").lower()
                    or any(t in ("tense", "guarded", "overwhelmed") for t in (s.get("tags") or [])))
        if tense >= 2:
            signals.append("carrying tension")
    except Exception:
        pass

    # 4. all-work-no-play: nothing restorative logged in a while
    if _days_since_play(now) is not None and _days_since_play(now) >= 5:
        signals.append("no play in a while")

    stuck = len(signals) >= 1 and ("under-recovered" in signals or "grinding" in signals
                                   or "carrying tension" in signals or len(signals) >= 2)
    severity = "high" if len(signals) >= 3 else ("medium" if len(signals) == 2 else "low")
    return {"stuck": stuck, "signals": signals, "severity": severity}


def _days_since_play(now: datetime) -> int | None:
    """Days since anything restorative was logged (a swim, yoga, or a logged restorative action)."""
    from .body_state import list_entries
    newest = None
    for kind in ("swim", "physio"):
        for e in list_entries(kind, limit=5):
            try:
                dt = datetime.fromisoformat(e["at"].replace("Z", "+00:00"))
                if newest is None or dt > newest:
                    newest = dt
            except Exception:
                continue
    # also count anything this layer recorded as done
    for entry in _repertoire_log():
        try:
            dt = datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
            if newest is None or dt > newest:
                newest = dt
        except Exception:
            continue
    if newest is None:
        return None
    return (now - newest).days


# --- the suggestion (active, playful, agenda-free) ---------------------------

def unstick(*, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """The intervention. If stuck (or force), name the grind dryly and offer ONE concrete active
    thing from the user's repertoire, weighted toward what's actually helped before. Never says
    rest/break. Returns {speech, suggestion, signals}."""
    now = now or _now()
    state = detect_stuck(now=now)
    if not state["stuck"] and not force:
        return {"stuck": False,
                "speech": "Nothing's flagging you as stuck right now. Carry on.",
                "signals": state["signals"]}

    suggestion = _pick_suggestion()
    speech = _compose(state["signals"], suggestion, state["severity"])
    out = {"stuck": True, "signals": state["signals"], "severity": state["severity"],
           "suggestion": suggestion["id"], "speech": speech}
    _assert_clean(speech)  # guardrail: never shipped the forbidden framings
    return out


def _pick_suggestion() -> dict[str, Any]:
    """Pick from the repertoire, weighted toward things that have actually helped this person.
    A little randomness so it isn't the same nudge every time (but never nags, see scheduler)."""
    helped = _helped_weights()
    repertoire = _repertoire()
    weights = [1.0 + helped.get(item["id"], 0.0) * 2.0 for item in repertoire]
    return random.choices(repertoire, weights=weights, k=1)[0]


def _compose(signals: list[str], suggestion: dict[str, Any], severity: str) -> str:
    """Dry, minimal, a little provocative. Name what's happening, offer the one thing. The reframe
    (this IS the recovery, done actively) is implicit in the framing, never spelled out earnestly."""
    # name the pattern, plainly, without therapising
    naming = {
        "under-recovered": "You're under-recovered",
        "grinding": "You're grinding",
        "carrying tension": "You've been carrying tension",
        "no play in a while": "It's been all work for a few days",
    }
    lead_bits = [naming[s] for s in signals if s in naming]
    if not lead_bits:
        lead = "You could use a change of gear"
    elif len(lead_bits) == 1:
        lead = lead_bits[0]
    else:
        lead = lead_bits[0] + ", and " + lead_bits[1].lower()
    # the offer , active, concrete, no "rest". Phrase it cleanly whether or not it starts with a verb.
    offer = suggestion["doing"]
    if offer.lower().startswith(("go ", "get ", "take ", "sit ", "put ", "make ", "cook ")):
        offer_clause = offer[0].upper() + offer[1:]
    else:
        offer_clause = "Go " + offer
    # a light provocation about the grind, varied
    nudge = random.choice([
        "More effort isn't the move here.",
        "Pushing harder won't fix this one.",
        "The answer isn't another set.",
        "This doesn't get solved by trying harder at it.",
    ])
    return f"{lead}. {nudge} {offer_clause}."


# --- learning: did it help? --------------------------------------------------

def record_did(suggestion_id: str, *, helped: bool | None = None,
               now: datetime | None = None) -> dict[str, Any]:
    """Record that the user did a suggested thing (and optionally whether it helped). This is how
    the layer learns what genuinely unsticks THIS person, NOT a forced rating. helped is optional
    and only set if the user volunteers it."""
    log = _repertoire_log()
    log.append({"id": suggestion_id, "at": (now or _now()).isoformat(),
                "helped": helped})
    _save_repertoire_log(log)
    return {"ok": True, "recorded": suggestion_id, "helped": helped}


def _helped_weights() -> dict[str, float]:
    """How often each repertoire item has helped when done, the basis for weighting suggestions."""
    counts: dict[str, list[int]] = {}
    for e in _repertoire_log():
        if e.get("helped") is None:
            continue
        counts.setdefault(e["id"], []).append(1 if e["helped"] else 0)
    return {k: (sum(v) / len(v)) for k, v in counts.items() if v}


def what_helps() -> dict[str, Any]:
    """'What actually unsticks me?', honest reflection of the logged effect data, or a note that
    there isn't enough yet."""
    weights = _helped_weights()
    if not weights:
        return {"have_data": False,
                "speech": "Not enough logged yet to know what unsticks you specifically. Next time "
                          "you do one of these and it helps, tell me and I'll start noticing."}
    ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    top = [i for i, w in ranked if w >= 0.5]
    names = {item["id"]: item["doing"].split(",")[0] for item in _repertoire()}
    if top:
        helps = ", ".join(names.get(i, i) for i in top[:3])
        return {"have_data": True, "speech": f"What's tended to help you: {helps}."}
    return {"have_data": True,
            "speech": "Mixed so far, nothing's clearly landed as the thing that unsticks you yet."}


# --- storage -----------------------------------------------------------------

def _repertoire_log() -> list[dict[str, Any]]:
    try:
        from ..db import get_app_state
        raw = get_app_state("unstuck_log", "")
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _save_repertoire_log(log: list[dict[str, Any]]) -> None:
    try:
        from ..db import set_app_state
        set_app_state("unstuck_log", json.dumps(log[-200:]))
    except Exception:
        pass


def _assert_clean(speech: str) -> None:
    """Guardrail: this layer must NEVER emit the flattened passive framings. If it ever does, that's
    a bug worth failing loudly in tests."""
    low = speech.lower()
    for bad in _FORBIDDEN:
        if bad in low:
            raise AssertionError(f"unstuck layer emitted forbidden passive framing: {bad!r}")
