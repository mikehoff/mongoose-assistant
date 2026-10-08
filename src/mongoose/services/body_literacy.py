from __future__ import annotations

import json
import uuid

from typing import Any

# Body Literacy , guided flows. The three "not built yet" deflections, made real:
#   - body check-in: a short interactive flow (signal -> texture -> emotion words -> need -> step)
#   - proprioception / grounding resets: a menu of short practical practices, logged before/after
#   - emotional-literacy journalling: structured prompt types, captured as entries
#
# THE SAFETY LINE (the documents are emphatic, and this is a GATE not a nicety): this layer must
# NOT diagnose, must NOT declare what the user is feeling, must NOT push body scans when they're
# destabilising, must NOT turn journalling into homework, must NOT pathologise. It OFFERS, keeps it
# short, accepts "not now", and tracks what helps. Mongoose facilitates the user noticing and
# naming; it never asserts the CONTENT of their inner state. Same discipline as the load estimate
# ("here's the signal", not "you are X"), applied to emotion.
#
# Flows are multi-turn but resumable: state lives in app_state keyed by conversation, and each step
# is self-contained so a missed turn degrades gracefully (the user can always say "stop").

# --- flow state (resumable across turns) -------------------------------------

def _state_key(conversation_id: str) -> str:
    return f"body_flow:{conversation_id or 'default'}"


def _load_flow(conversation_id: str) -> dict[str, Any] | None:
    try:
        from ..db import get_app_state
        raw = get_app_state(_state_key(conversation_id), "")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _save_flow(conversation_id: str, state: dict[str, Any]) -> None:
    try:
        from ..db import set_app_state
        set_app_state(_state_key(conversation_id), json.dumps(state))
    except Exception:
        pass


def _clear_flow(conversation_id: str) -> None:
    try:
        from ..db import set_app_state
        set_app_state(_state_key(conversation_id), "")
    except Exception:
        pass


def flow_active(conversation_id: str) -> bool:
    return _load_flow(conversation_id) is not None


# universal escape hatches , "not now" must always work, at any step
_STOP = ("stop", "cancel", "not now", "nevermind", "never mind", "leave it", "quit", "exit",
         "enough", "skip", "no thanks", "later")


def _wants_stop(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(low == s or low.startswith(s + " ") or low.endswith(" " + s) for s in _STOP)


# --- starting a flow ---------------------------------------------------------

def start_checkin(conversation_id: str) -> dict[str, Any]:
    """Begin a body-literacy check-in. Short, body-first, optional at every step."""
    state = {"flow": "checkin", "step": "signal", "data": {}, "id": uuid.uuid4().hex[:8]}
    _save_flow(conversation_id, state)
    return {"speech": "Okay, quick check-in, you can stop any time. Where do you feel the most "
            "signal in your body right now?", "flow": "checkin", "step": "signal"}


def start_reset(conversation_id: str, *, kind: str = "") -> dict[str, Any]:
    """Begin a proprioception/grounding reset. If kind unspecified, offer the menu."""
    if not kind:
        state = {"flow": "reset", "step": "choose", "data": {}, "id": uuid.uuid4().hex[:8]}
        _save_flow(conversation_id, state)
        return {"speech": "What have you got time for? A 60-second feet-and-breath reset, a "
                "3-minute wall-press and rib-over-pelvis check, a 5-minute balance reset, or the "
                "pre-gym primer.", "flow": "reset", "step": "choose"}
    return _begin_reset_practice(conversation_id, kind)


def start_journal(conversation_id: str, *, kind: str = "") -> dict[str, Any]:
    """Begin an emotional-literacy journal entry. Offers the prompt types if unspecified."""
    if not kind:
        state = {"flow": "journal", "step": "choose", "data": {}, "id": uuid.uuid4().hex[:8]}
        _save_flow(conversation_id, state)
        return {"speech": "What kind of entry? Naming a feeling, bridging a body sensation to an "
                "emotion, after a difficult interaction, or just freeform.", "flow": "journal",
                "step": "choose"}
    state = {"flow": "journal", "step": _first_journal_step(kind), "data": {"kind": kind},
             "id": uuid.uuid4().hex[:8]}
    _save_flow(conversation_id, state)
    return {"speech": _journal_prompt(_first_journal_step(kind)), "flow": "journal",
            "step": state["step"]}


# --- advancing a flow (called when a flow is active) -------------------------

def advance(conversation_id: str, text: str) -> dict[str, Any]:
    """Feed the user's reply into the active flow. Returns {speech, done}. Honors 'not now'."""
    state = _load_flow(conversation_id)
    if not state:
        return {"speech": "", "done": True, "no_flow": True}

    if _wants_stop(text):
        _clear_flow(conversation_id)
        # if a check-in/journal had partial content, still save what was named (nothing is lost)
        saved = _save_partial(state, conversation_id)
        tail = " I've kept what you noted." if saved else ""
        return {"speech": "No problem, stopping there." + tail, "done": True}

    flow = state.get("flow")
    if flow == "checkin":
        return _advance_checkin(conversation_id, state, text)
    if flow == "reset":
        return _advance_reset(conversation_id, state, text)
    if flow == "journal":
        return _advance_journal(conversation_id, state, text)
    _clear_flow(conversation_id)
    return {"speech": "", "done": True}


# --- check-in flow -----------------------------------------------------------

def _advance_checkin(conversation_id, state, text) -> dict[str, Any]:
    step = state["step"]
    data = state["data"]
    if step == "signal":
        data["body_signal"] = text.strip()
        state["step"] = "texture"
        _save_flow(conversation_id, state)
        return {"speech": "What's the texture of it, tight, hot, heavy, shaky, numb, restless, "
                "guarded?", "done": False}
    if step == "texture":
        data["body_texture"] = text.strip()
        state["step"] = "emotion"
        _save_flow(conversation_id, state)
        return {"speech": "If it had an emotion word, what are the nearest one to three?",
                "done": False}
    if step == "emotion":
        data["emotion_words"] = _split_words(text)
        state["step"] = "need"
        _save_flow(conversation_id, state)
        return {"speech": "Got it. What do you need right now, if anything?", "done": False}
    if step == "need":
        data["need"] = text.strip()
        state["step"] = "offer"
        _save_flow(conversation_id, state)
        # OFFER choices , never prescribe
        return {"speech": "Thanks for naming that. Useful next step, your pick: a 3-minute "
                "grounding reset, a journal note to remember the pattern, or just leave it here?",
                "done": False}
    if step == "offer":
        low = text.strip().lower()
        from .body_state import log_journal
        # persist the check-in regardless of the choice
        log_journal(mode="checkin", body_signal=data.get("body_signal", ""),
                    body_texture=data.get("body_texture", ""),
                    emotion_words=data.get("emotion_words", []), need=data.get("need", ""),
                    source="flow")
        _clear_flow(conversation_id)
        if "reset" in low or "ground" in low or "3" in low:
            return _begin_reset_practice(conversation_id, "3min", prefix="Saved. ")
        if "journal" in low or "note" in low or "write" in low:
            return start_journal(conversation_id, kind="freeform")
        return {"speech": "Saved. That's a useful thing to have noticed.", "done": True}
    _clear_flow(conversation_id)
    return {"speech": "", "done": True}


# --- reset flow --------------------------------------------------------------

_RESET_MENU = {
    "60s": ("60-second reset", 1,
            "Stand with both feet planted. Notice heel, big toe, little toe on each side. Shift "
            "your weight slowly left and right, five times. Breathe out slowly on each shift."),
    "3min": ("3-minute reset", 3,
             "Wall press for twenty seconds, hands flat, ribs down. Then breathe out slowly and "
             "check rib-over-pelvis, stack, don't flare. Five slow breaths. Notice whether the "
             "tighter side softens or braces."),
    "5min": ("5-minute balance reset", 5,
             "Single-leg balance, eyes open, thirty seconds each side. Slow head turns while you "
             "balance. Then quadruped rocking, slow, ten times. Notice which side feels more sure."),
    "primer": ("pre-gym primer", 8,
               "Tabletop leg lowering, then dead bug, slow. Single-leg calf bridge, feel the "
               "working calf take it. Serratus reach, ribs down. Finish with a Pallof press hold "
               "each side. You're priming control, not tiring out."),
}


def _reset_menu() -> dict[str, tuple[str, int, str]]:
    """_RESET_MENU with any instructions the site profile words for the person's own body (lexicon
    body.reset_scripts, {reset key: instructions})."""
    from .. import profile as _profile
    scripts = _profile.lexicon("body.reset_scripts", {}) or {}
    return {k: ((name, mins, scripts[k]) if k in scripts else (name, mins, text))
            for k, (name, mins, text) in _RESET_MENU.items()}


def _begin_reset_practice(conversation_id, kind, prefix="") -> dict[str, Any]:
    key = _normalise_reset(kind)
    menu = _reset_menu()
    name, mins, instructions = menu.get(key, menu["3min"])
    # ask for a quick before-rating so we can learn what helps , optional
    state = {"flow": "reset", "step": "after", "data": {"reset_type": key, "duration_min": mins},
             "id": uuid.uuid4().hex[:8]}
    _save_flow(conversation_id, state)
    return {"speech": f"{prefix}{name}. {instructions} Tell me when you're done, and whether you "
            "feel more settled, the same, or more braced.", "done": False}


def _advance_reset(conversation_id, state, text) -> dict[str, Any]:
    step = state["step"]
    data = state["data"]
    if step == "choose":
        key = _normalise_reset(text)
        return _begin_reset_practice(conversation_id, key)
    if step == "after":
        effect = _read_effect(text)
        from .body_state import log_journal
        log_journal(mode="reset", reset_type=data.get("reset_type", ""),
                    duration_min=data.get("duration_min"), effect=effect, source="flow")
        _clear_flow(conversation_id)
        if effect == "better":
            return {"speech": "Good, I'll remember that one helped. Logged.", "done": True}
        if effect == "worse":
            return {"speech": "Noted, that one didn't settle you today. I'll remember that too. "
                    "No need to push it.", "done": True}
        return {"speech": "Logged. Useful to have a record either way.", "done": True}
    _clear_flow(conversation_id)
    return {"speech": "", "done": True}


# --- journal flow ------------------------------------------------------------

_JOURNAL_KINDS = {
    "naming": ["happened", "body_did", "emotion", "need", "step"],
    "bridge": ["signal", "texture", "emotion", "step"],
    "difficult": ["hit", "wanted", "protected", "need"],
    "freeform": ["freeform"],
}

_JOURNAL_PROMPTS = {
    "happened": "What happened?",
    "body_did": "What did your body do?",
    "emotion": "Three possible emotion words?",
    "need": "What do you need?",
    "step": "Smallest useful next step?",
    "signal": "Where's the signal in your body?",
    "texture": "What's its texture?",
    "hit": "What part of you felt hit?",
    "wanted": "What did you want to say or do?",
    "protected": "What were you protecting?",
    "freeform": "Go ahead, say or write whatever you want to remember. I'll keep it.",
}


def _first_journal_step(kind: str) -> str:
    return _JOURNAL_KINDS.get(kind, _JOURNAL_KINDS["freeform"])[0]


def _journal_prompt(step: str) -> str:
    return _JOURNAL_PROMPTS.get(step, "Go ahead.")


def _advance_journal(conversation_id, state, text) -> dict[str, Any]:
    step = state["step"]
    data = state["data"]
    kind = data.get("kind", "freeform")

    if step == "choose":
        chosen = _normalise_journal_kind(text)
        data["kind"] = chosen
        state["data"] = data
        state["step"] = _first_journal_step(chosen)
        _save_flow(conversation_id, state)
        return {"speech": _journal_prompt(state["step"]), "done": False}

    # record this step's answer
    data.setdefault("answers", {})[step] = text.strip()
    steps = _JOURNAL_KINDS.get(kind, _JOURNAL_KINDS["freeform"])
    idx = steps.index(step) if step in steps else len(steps) - 1
    if idx + 1 < len(steps):
        state["step"] = steps[idx + 1]
        state["data"] = data
        _save_flow(conversation_id, state)
        return {"speech": _journal_prompt(state["step"]), "done": False}

    # done , assemble and save
    _save_journal_entry(kind, data.get("answers", {}), conversation_id)
    _clear_flow(conversation_id)
    return {"speech": "Saved. That's a good one to be able to look back on.", "done": True}


def _save_journal_entry(kind: str, answers: dict[str, str], conversation_id: str | None = None) -> None:
    from .body_state import log_journal
    emotion = _split_words(answers.get("emotion", "")) if answers.get("emotion") else []
    text = " | ".join(f"{k}: {v}" for k, v in answers.items())
    log_journal(mode=f"journal:{kind}",
                body_signal=answers.get("signal", "") or answers.get("happened", ""),
                body_texture=answers.get("texture", ""),
                emotion_words=emotion, need=answers.get("need", ""),
                next_step=answers.get("step", ""), text=text, source="flow")
    # For a GUEST conversation, ALSO persist the verbatim privately (encrypted at rest, readable only
    # back to that guest) and distil non-sensitive facts so Mongoose grows familiar with them over time.
    # Owner entries do not need this , the owner's journal is the owner's own data on the owner's Mac.
    try:
        cid = conversation_id or ""
        if cid.startswith("guest:"):
            from .journal_privacy import record_guest_journal
            record_guest_journal(cid, text, mode=f"journal:{kind}", learn=True)
    except Exception:
        pass


# --- partial save (nothing named is lost on stop) ----------------------------

def _save_partial(state: dict[str, Any], conversation_id: str | None = None) -> bool:
    flow = state.get("flow")
    data = state.get("data", {})
    if flow == "checkin" and (data.get("body_signal") or data.get("emotion_words")):
        from .body_state import log_journal
        log_journal(mode="checkin", body_signal=data.get("body_signal", ""),
                    body_texture=data.get("body_texture", ""),
                    emotion_words=data.get("emotion_words", []), need=data.get("need", ""),
                    source="flow_partial")
        return True
    if flow == "journal" and data.get("answers"):
        _save_journal_entry(data.get("kind", "freeform"), data["answers"], conversation_id)
        return True
    return False


# --- normalisers -------------------------------------------------------------

def _normalise_reset(text: str) -> str:
    low = (text or "").lower()
    if "primer" in low or "pre-gym" in low or "pre gym" in low or "gym" in low:
        return "primer"
    if "5" in low or "five" in low or "balance" in low:
        return "5min"
    if "60" in low or "sixty" in low or "minute" in low and "3" not in low and "three" not in low:
        return "60s"
    if "3" in low or "three" in low or "wall" in low or "rib" in low:
        return "3min"
    return "3min"


def _normalise_journal_kind(text: str) -> str:
    low = (text or "").lower()
    if "nam" in low or "feel" in low:
        return "naming"
    if "bridg" in low or "sensation" in low or "body" in low:
        return "bridge"
    if "difficult" in low or "interaction" in low or "argument" in low or "after" in low:
        return "difficult"
    return "freeform"


def _read_effect(text: str) -> str:
    low = (text or "").lower()
    if any(w in low for w in ("better", "settled", "calmer", "softer", "good", "more settled",
                              "grounded", "easier", "released")):
        return "better"
    if any(w in low for w in ("worse", "braced", "more braced", "tense", "tenser", "bad",
                              "didn't help", "no better")):
        return "worse"
    return "same"


def _split_words(text: str) -> list[str]:
    import re
    parts = re.split(r"[,/]| and | or ", text or "")
    return [p.strip() for p in parts if p.strip()][:4]
