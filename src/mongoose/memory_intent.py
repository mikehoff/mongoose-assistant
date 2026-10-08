"""Deterministic memory-intent detection, the layer that makes casual preference memory and natural
forgetting trustworthy. Runs BEFORE the model so behaviour is predictable, not dependent on the model
returning clean JSON. Robust regex/phrase patterns only (no model inference, no embeddings).

Two intents matter here:
  - remember_preference: the user casually states a like/dislike ("really fond of Hawaiian", "my
    favourite pizza is Hawaiian", "I can't stand anchovies"). We normalise it to a simple fact
    sentence and store it, then emit a visible 'remembered' event.
  - forget_memory: the user asks to forget something in natural paraphrase ("could you maybe forget
    that I liked pineapple on pizza"). We extract the target phrase so the caller can fuzzy-match and
    delete, emitting 'forgotten' or 'forget_needs_confirmation'.

The key claim-integrity rule lives in the caller: never say "I forgot that" unless a row was actually
deleted. This module only classifies and extracts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MemoryIntent:
    intent: str                       # remember_preference | forget_memory | none
    target: str = ""                  # the thing liked/disliked, or the phrase to forget
    polarity: str = ""                # likes | dislikes (for remember_preference)
    confidence: str = "high"
    source_text: str = ""
    selection: int = 0                # for "forget #1" style replies; 1-based, 0 = none
    extras: dict = field(default_factory=dict)


# --- forgetting --------------------------------------------------------------------------------

# "forget #1" / "forget the first one" / "the second one" , a reply choosing among candidates
_FORGET_SELECT = re.compile(
    r"\b(?:forget|delete|remove)?\s*(?:#\s*(\d+)|\bthe\s+(first|second|third|1st|2nd|3rd|one|two|three)\b|\bnumber\s+(\d+))",
    re.IGNORECASE)
_ORDINALS = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2,
             "third": 3, "3rd": 3, "three": 3}

# a forget request: "forget that I liked X", "could you maybe forget the X thing", "stop remembering X"
_FORGET = re.compile(
    r"\b(?:forget|stop\s+remembering|don'?t\s+remember|delete|remove)\b(?:\s+(?:that|about|the))?\s*(.*)",
    re.IGNORECASE)

# leading filler so "could you maybe forget ..." extracts a clean target
_FORGET_PREFIXES = re.compile(
    r"^(?:could\s+you\s+|can\s+you\s+|would\s+you\s+|please\s+|maybe\s+|hey\s+|um+\s+|so\s+)+", re.IGNORECASE)

# trailing words that aren't part of the target ("...the pineapple pizza thing", "...that i liked X")
_FORGET_TARGET_CLEAN = [
    (re.compile(r"^(?:that\s+)?i\s+(?:liked|like|was\s+fond\s+of|am\s+fond\s+of|prefer|preferred|love|loved|enjoy|enjoyed)\s+", re.IGNORECASE), ""),
    (re.compile(r"\bthe\s+(.*?)\s+thing\b", re.IGNORECASE), r"\1"),
    (re.compile(r"^the\s+", re.IGNORECASE), ""),
    (re.compile(r"[.?!]+$"), ""),
]


def _extract_forget_polarity_and_target(raw: str) -> tuple[str, str]:
    """Extract a polarity hint from a forget request where present.

    Examples:
      "that I liked X" -> ("likes", "X")
      "that I didn't like X" -> ("dislikes", "X")
      "that I was fond of X" -> ("likes", "X")
      "the X thing" -> ("", "X")

    This prevents a dangerous false delete where "forget that I liked X" could delete an existing
    "The user dislikes X." memory just because the referent matches.
    """
    t = _FORGET_PREFIXES.sub("", raw.strip())
    for pat, pol in [
        (re.compile(r"^(?:that\s+)?i\s+don'?t\s+(?:really\s+)?(?:like|enjoy)\s+", re.IGNORECASE), "dislikes"),
        (re.compile(r"^(?:that\s+)?i\s+(?:really\s+|absolutely\s+)?can'?t\s+stand\s+", re.IGNORECASE), "dislikes"),
        (re.compile(r"^(?:that\s+)?i\s+(?:really\s+|absolutely\s+)?(?:disliked|dislike|hated|hate|detested|detest|loathed|loathe)\s+", re.IGNORECASE), "dislikes"),
        (re.compile(r"^(?:that\s+)?i\s+(?:liked|like|was\s+fond\s+of|am\s+fond\s+of|prefer(?:red)?|love(?:d)?|enjoy(?:ed)?)\s+", re.IGNORECASE), "likes"),
    ]:
        m = pat.match(t)
        if m:
            target = t[m.end():].strip()
            for cpat, repl in _FORGET_TARGET_CLEAN[1:]:
                target = cpat.sub(repl, target).strip()
            return pol, target.strip()
    for pat, repl in _FORGET_TARGET_CLEAN:
        t = pat.sub(repl, t).strip()
    return "", t.strip()


# --- preferences -------------------------------------------------------------------------------

# Each pattern captures the TARGET (group 1). Order matters: more specific first. We strip a leading
# "could you/please/..." filler first so "please remember I love X" still parses the preference.
_LIKE_PATTERNS = [
    re.compile(r"\bmy\s+fav(?:ou?rite|e)?(?:\s+[\w-]+)?\s+(?:is|are)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bmy\s+go[- ]?to(?:\s+[\w-]+)?\s+(?:is|are)\s+(.+)", re.IGNORECASE),
    re.compile(r"\b(.+?)\s+is\s+my\s+fav(?:ou?rite|e)?\b", re.IGNORECASE),
    re.compile(r"\bi'?m\s+(?:really\s+|very\s+|so\s+)?(?:fond\s+of|into)\s+(.+)", re.IGNORECASE),
    re.compile(r"\b(?:really|very|so)\s+fond\s+of\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+(?:really|absolutely|genuinely|totally)\s+(?:like|love|enjoy)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+(?:like|love|enjoy|prefer|adore)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+usually\s+(?:pick|get|order|choose|go\s+for)\s+(.+)", re.IGNORECASE),
]

_DISLIKE_PATTERNS = [
    re.compile(r"\bi'?m\s+not\s+(?:really\s+)?into\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+(?:really\s+|absolutely\s+)?can'?t\s+stand\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+(?:really\s+|absolutely\s+)?(?:dislike|hate|detest|loathe)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+don'?t\s+(?:really\s+)?(?:like|enjoy)\s+(.+)", re.IGNORECASE),
]

_PREF_PREFIXES = re.compile(
    r"^(?:could\s+you\s+|can\s+you\s+|please\s+|just\s+|fyi\s+|btw\s+|honestly\s+|i\s+think\s+|"
    r"actually\s+|so\s+|well\s+|um+\s+|hey\s+)+", re.IGNORECASE)

# trailing chatter that shouldn't be part of the captured preference
_PREF_TRAIL = re.compile(r"\s+(?:tbh|honestly|though|really|i\s+guess|i\s+think|lol|haha)\.?$", re.IGNORECASE)


def _clean_target(raw: str) -> str:
    t = raw.strip().rstrip(".!?,")
    t = _PREF_TRAIL.sub("", t).strip()
    return t


def detect_memory_intent(text: str, awaiting_forget_choice: bool = False) -> MemoryIntent:
    """Classify a user turn for memory intent. `awaiting_forget_choice` is True when the previous
    turn offered forget candidates, so a bare "the first one" / "#2" resolves to a selection."""
    stripped = (text or "").strip()
    if not stripped:
        return MemoryIntent("none")

    # If we just asked which memory to forget, interpret a short selection reply.
    if awaiting_forget_choice:
        m = _FORGET_SELECT.search(stripped)
        if m:
            num = m.group(1) or m.group(3)
            if num and num.isdigit():
                return MemoryIntent("forget_memory", selection=int(num), source_text=stripped)
            word = (m.group(2) or "").lower()
            if word in _ORDINALS:
                return MemoryIntent("forget_memory", selection=_ORDINALS[word], source_text=stripped)

    # Forget intent (checked before preferences: "forget that I liked X" contains "I liked X").
    low = stripped.lower()
    if re.search(r"\b(?:forget|stop\s+remembering|don'?t\s+remember)\b", low) or \
       (re.search(r"\b(?:delete|remove)\b", low) and re.search(r"\b(?:memory|memories|that\s+i|the\s+\w+\s+thing)\b", low)):
        m = _FORGET.search(stripped)
        polarity, target = _extract_forget_polarity_and_target(m.group(1)) if m and m.group(1) else ("", "")
        # a selection-style forget ("forget the first one") with no prior context is low-confidence
        return MemoryIntent("forget_memory", target=target, polarity=polarity, source_text=stripped,
                            confidence="high" if target else "low")

    # Remember-preference intent. Strip filler, then try dislike (more specific) then like.
    core = _PREF_PREFIXES.sub("", stripped)
    for pat in _DISLIKE_PATTERNS:
        m = pat.search(core)
        if m:
            tgt = _clean_target(m.group(1))
            if tgt:
                return MemoryIntent("remember_preference", target=tgt, polarity="dislikes",
                                    source_text=stripped)
    for pat in _LIKE_PATTERNS:
        m = pat.search(core)
        if m:
            tgt = _clean_target(m.group(1))
            if tgt:
                return MemoryIntent("remember_preference", target=tgt, polarity="likes",
                                    source_text=stripped)

    return MemoryIntent("none")


def preference_to_fact(intent: MemoryIntent) -> str:
    """Normalise a preference intent into a simple, dedupe-friendly fact sentence."""
    verb = "likes" if intent.polarity == "likes" else "dislikes"
    target = intent.target.strip().rstrip(".")
    return f"The user {verb} {target}."
