"""Deterministic pre-router for a small set of UNAMBIGUOUS chat commands.

The local planner model is unreliable at tool selection: it over-fires on stated preferences (a
music favourite becomes a broadcast to everyone) and under-fires on clear commands. For a few command
shapes where the intent is unmistakable, we construct the exact tool call here, BEFORE the model ever
sees the turn, so those cases are deterministic.

This is deliberately conservative: it only matches a clear imperative WITH an explicit recipient. A
statement of preference ("my go-to was Madonna", "Ray of Light obviously", "I love Blackbird") never
matches, so it falls through to the normal model tool-loop unchanged. The router does NOT execute
anything: the caller (chat_with_memory) feeds a match into the propose/confirm gate, so an outward
action still asks before it sends.

Returns (tool_name, args) on a clean match, or None to fall through.
"""
from __future__ import annotations

import re

# recommend/suggest/send/share <song> to|for|with <who>
_REC_TO_RE = re.compile(
    r"^(?:recommend|suggest|send|share)\s+(?P<song>.+?)\s+(?:to|for|with)\s+(?P<who>[\w' -]+)$",
    re.IGNORECASE,
)
# tell <who> to listen to|hear|check out <song>
_REC_TELL_RE = re.compile(
    r"^tell\s+(?P<who>[\w' -]+)\s+to\s+(?:listen to|hear|check out)\s+(?P<song>.+)$",
    re.IGNORECASE,
)
# message|tell|text|dm <who>: <text>   (requires an explicit ':' or ',' after the recipient)
_MSG_RE = re.compile(
    r"^(?:message|tell|text|dm)\s+(?P<who>[\w' -]+)[:,]\s*(?P<text>.+)$",
    re.IGNORECASE,
)


def _clean(value: str) -> str:
    return (value or "").strip().strip("\"'").strip()


def deterministic_tool_route(prompt: str) -> tuple[str, dict] | None:
    """Map a clearly-imperative message to an exact (tool, args), or None to fall through.

    Order matters: the song-recommendation shapes are checked before the generic message shape so
    "tell Sam to listen to X" routes to recommend_song, not message_person (the message shape needs a
    ':'/',' after the recipient, which the recommend phrasing does not have)."""
    p = (prompt or "").strip()
    if not p or "\n" in p:
        return None

    m = _REC_TO_RE.match(p)
    if m:
        song, who = _clean(m.group("song")), _clean(m.group("who"))
        if song and who:
            return ("recommend_song", {"song": song, "to": who})

    m = _REC_TELL_RE.match(p)
    if m:
        song, who = _clean(m.group("song")), _clean(m.group("who"))
        if song and who:
            return ("recommend_song", {"song": song, "to": who})

    m = _MSG_RE.match(p)
    if m:
        who, text = _clean(m.group("who")), _clean(m.group("text"))
        if who and text:
            return ("message_person", {"to": who, "text": text})

    return None
