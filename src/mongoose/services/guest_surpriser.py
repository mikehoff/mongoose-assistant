"""Guest surpriser, the RESTRICTED surpriser for trusted guests.

The owner surpriser (surpriser.py / pick_surprise) reads Alex's real world: calendar, email,
cross-domain correlations, device state. A guest must see NONE of that. So this is a SEPARATE entry
point that, by construction, never calls candidate_suggestions() and never touches owner data, there is
no code path from here to Alex's private state. Isolation by separation, not by filtering (filtering is
leak-prone; a missing branch is not).

What a guest surprise may contain, in priority order:
  1. An unread message Alex has left this specific guest (via the mailbox, which already enforces that a
     guest only ever sees their OWN thread).
  2. A delight from the GUEST well of the delight cache (neutral facts/jokes; never owner-personal
     material), or the curated guest_delights library as fallback.
  3. A light entertainment (would-you-rather / riddle / compliment) from guest_delights.

Everything here is keyed on the guest's own device scope. A guest surprise is always safe to show.
"""
from __future__ import annotations

import random
from typing import Any


def _owner_message_for(guest_scope: str) -> dict[str, Any] | None:
    """An unread message from Alex to THIS guest, if any. Uses the mailbox, which only ever returns the
    guest's own thread. Returns the most recent unread, or None."""
    try:
        from .mailbox import unread_for
        msgs = unread_for(guest_scope, limit=5)
        if msgs:
            from .. import profile as _profile
            host = _profile.owner_name()
            m = msgs[0]
            return {
                "kind": "owner_message",
                "text": m.get("body", ""),
                "from": host,
                "message_id": m.get("id"),
                "why": f"A message {host} left for you.",
            }
    except Exception:
        pass
    return None


def _guest_delight() -> dict[str, Any] | None:
    """A neutral delight for a guest: the guest well of the cache first, then the curated guest library.
    Never owner-personal."""
    # 1) scaled guest cache
    try:
        from .delight import delight as _delight
        d = _delight(allow_model=False, audience="guest")
        if d and d.get("text"):
            return {"kind": "delight", "text": d["text"], "delight_kind": d.get("kind"),
                    "source": d.get("source"), "caveat": d.get("caveat"),
                    "why": "A little something to brighten the moment."}
    except Exception:
        pass
    # 2) curated guest library fallback
    try:
        from .guest_delights import random_fact
        f = random_fact()
        if f and f.get("text"):
            return {"kind": "delight", "text": f["text"], "style": f.get("style"),
                    "why": "A fun fact for you."}
    except Exception:
        pass
    return None


def _guest_lounge_prompt() -> dict[str, Any] | None:
    """A guest-safe prompt based on the shared #mongoose lounge. Uses only public room
    messages and consented guest cards; never owner data or private guest threads."""
    try:
        from .lounge_surpriser import lounge_snapshot, compose_lounge_surprise
        text = compose_lounge_surprise(lounge_snapshot())
        if text:
            return {"kind": "lounge_prompt", "text": text,
                    "why": "A little nudge for the shared #mongoose room."}
    except Exception:
        pass
    return None


def _guest_entertainment() -> dict[str, Any] | None:
    """A light interactive bit (would-you-rather / riddle / compliment / mood) from the guest library."""
    try:
        from .guest_delights import entertain
        roll = random.random()
        if roll < 0.35:
            e = entertain("would_you_rather")
        elif roll < 0.6:
            e = entertain("riddle")
        elif roll < 0.8:
            e = entertain("compliment")
        else:
            e = entertain("mood")
        text = e.get("text") or e.get("question") or ""
        if text:
            out = {"kind": e.get("kind", "entertain"), "text": text, "why": "Just for fun."}
            if e.get("answer"):
                out["answer"] = e["answer"]
            return out
    except Exception:
        pass
    return None


def guest_surprise(guest_scope: str, *, prefer: str = "auto") -> dict[str, Any]:
    """Pick ONE guest-safe surprise. guest_scope is the 'guest:<device_id>' string. By construction this
    only ever returns delights, entertainment, or an owner→guest message, never owner data.

    Priority: an unread message from Alex wins (it's personal and waiting); otherwise a delight or a
    light entertainment. prefer can bias toward 'delight' or 'entertainment'."""
    if not (guest_scope or "").startswith("guest:"):
        # Defensive: this entry point is ONLY for guests. Never serve it for the owner scope.
        return {"surfaced": False, "reason": "guest_surprise is only for guest scopes"}

    # 1) Owner message first , always surface a waiting message from Alex.
    msg = _owner_message_for(guest_scope)
    if msg:
        return {"surfaced": True, "audience": "guest", "suggestion": msg}

    # 2) Delight vs entertainment, with an explicit lounge mode for the shared IRC room.
    if prefer == "lounge":
        pick = _guest_lounge_prompt() or _guest_delight() or _guest_entertainment()
    elif prefer == "entertainment":
        pick = _guest_entertainment() or _guest_delight()
    elif prefer == "delight":
        pick = _guest_delight() or _guest_entertainment()
    else:
        pick = (_guest_delight() if random.random() < 0.65 else _guest_entertainment()) \
            or _guest_delight() or _guest_entertainment()

    if pick:
        return {"surfaced": True, "audience": "guest", "suggestion": pick}
    return {"surfaced": False, "reason": "nothing to surface"}


def guest_can_do() -> dict[str, Any]:
    """What Mongoose tells a guest they can do, surfaced so guests know the light, low-stakes help
    available to them. This is informational only; the actual capabilities live in guest_tasks.py and
    are scope-isolated from Alex's heavy tools/scheduler."""
    try:
        from .guest_tasks import capabilities_blurb
        return capabilities_blurb()
    except Exception:
        # Fallback static blurb if guest_tasks isn't present.
        return {
            "intro": "I can be a low-key helper while you're here.",
            "can_do": [
                "a quick mood lift, a calming breathing exercise, or a moment to talk something through",
                "a light focus or ADHD nudge: break a task down, set a short timer, a gentle check-in",
                "a quick bit of research or a fun fact",
                "a simple plan for your day or a short to-do",
                "flag something that's broken (a light, the wifi, a tablet) for the owner to fix",
            ],
            "cannot_do": [
                "anything with the owner's emails, calendar, files, or home controls (those stay private)",
            ],
        }
