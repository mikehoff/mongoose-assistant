"""Rule-engagement likelihood: deterministic short-circuits that learn to stand down.

The chat path contains many rule-driven short-circuits (RTM status cards, readiness, the plugin
bridge, alarm parsing, and so on). They are deterministic on purpose, but a misfire used to be a
trap: the rule answers wrongly, the person says "what? that's not what I asked" or rewords the
same request, and the identical rule fires identically again. The model never got a turn to
repair the conversation.

This module makes engagement a LIKELIHOOD instead of a binary:

  - Every rule consults assess() before firing and records when it does fire.
  - If the previous assistant turn in this conversation came from the same rule, and the new
    message shows CONFUSION ("what?", "no, I meant", "nie o to chodzi", "???"), or is the SAME
    PROMPT verbatim (normalised), or is the SAME GIST reworded (content-token similarity), the
    rule's score collapses and it stands down, so the message reaches the actual model.
  - Standing down also sets a short suppression window for that rule in that conversation, so a
    third rewording does not resurrect it seconds later. Windows expire; rules recover on their
    own, and a genuinely fresh request minutes later engages normally.

Fail-open by design: if anything in here breaks, rules behave exactly as before (they engage).
A likelihood layer must never be the thing that takes the product down.

State lives in app_state as small JSON blobs per conversation: no schema migration, cheap to
read on every turn, and self-expiring by timestamp comparison.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

# How long after a rule fires its own feedback signals apply (the person reacting to the card
# they just got), and how long a stood-down rule stays suppressed before recovering.
FEEDBACK_WINDOW_S = 15 * 60
SUPPRESSION_S = 10 * 60
ENGAGE_THRESHOLD = 0.5

# Confusion and pushback, English and Polish, matched against the WHOLE new message. Kept
# deliberately conservative: these must indicate reacting-to-the-last-reply, not general chat.
_CONFUSION_RE = re.compile(
    r"^(what|huh|why|no+|wtf|eh)\W*$"
    r"|\?{2,}"
    r"|\bthat'?s not what i\b|\bnot what i (asked|meant|wanted)\b"
    r"|\bi didn'?t ask\b|\bwhy are you (telling|showing)\b|\bstop (showing|telling)\b"
    r"|\bwrong answer\b|\byou misunderstood\b|\bthat wasn'?t\b|\btry again\b"
    r"|\bnie o to( mi)? chodzi\b|\bnie pytam\b|\bnie to\b|\bco\?\s*$|\bo co ci chodzi\b"
    r"|\bzle zrozumia\b|\bnie rozumiesz\b",
    re.IGNORECASE)

_WORD_RE = re.compile(r"[a-z0-9\u00c0-\u017f]+", re.IGNORECASE)
_STOP = {"the", "a", "an", "to", "of", "and", "or", "is", "it", "i", "me", "my", "you", "please",
         "can", "could", "would", "do", "does", "for", "on", "in", "with", "that", "this",
         "prosze", "czy", "mi", "na", "w", "z", "o"}


def _norm(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _content_tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def _similarity(a: str, b: str) -> float:
    """Same-gist similarity. Prefers multilingual sentence embeddings when the optional local
    model is present (so 'how is the RTM build going?' and 'jak idzie budowa RTM?' register as
    the same gist across languages); falls back to content-token Jaccard otherwise. The
    embedding scale runs hotter than Jaccard, so it is mapped conservatively: only clearly
    paraphrase-level cosine counts."""
    # Short-utterance guard: acknowledgements ("ok", "yes please", "thanks") embed close to
    # each other, and without this guard every polite "ok" within the feedback window would
    # read as a reworded re-attempt, EXTENDING the suppression window so a stood-down rule
    # never regains confidence. Jaccard was implicitly strict here (no shared tokens, score
    # 0); the semantic path must not be looser. Acknowledgements carry at most one content
    # token ("ok", "yes please", "thanks"); genuine short questions ("is everything ready?")
    # carry two or more, so two is the line.
    ta_g, tb_g = _content_tokens(a), _content_tokens(b)
    if len(ta_g) < 2 or len(tb_g) < 2:
        if not ta_g or not tb_g:
            return 0.0
        return len(ta_g & tb_g) / len(ta_g | tb_g)
    try:
        from .embeddings import semantic_similarity
        cos = semantic_similarity(a, b)
        if cos is not None:
            # 0.80+ cosine on this model is paraphrase territory; map so the existing 0.6
            # engagement threshold means the same thing it always did.
            return max(0.0, (cos - 0.5) / 0.5)
    except Exception:
        pass
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _state_key(conversation_id: str) -> str:
    return f"rule_engagement:{conversation_id or 'default'}"


def _load(conversation_id: str) -> dict[str, Any]:
    try:
        from ..db import get_app_state
        raw = get_app_state(_state_key(conversation_id))
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save(conversation_id: str, state: dict[str, Any]) -> None:
    try:
        from ..db import set_app_state
        set_app_state(_state_key(conversation_id), json.dumps(state))
    except Exception:
        pass


def record_engagement(rule_id: str, prompt: str, conversation_id: str = "") -> None:
    """Called when a rule actually fires, so the NEXT turn's signals know what to react to."""
    state = _load(conversation_id)
    state["last"] = {"rule": rule_id, "prompt_norm": _norm(prompt), "prompt": (prompt or "")[:400],
                     "at": time.time()}
    _save(conversation_id, state)


def assess(rule_id: str, prompt: str, conversation_id: str = "",
           base_score: float = 1.0) -> dict[str, Any]:
    """Score whether rule_id should engage for this prompt right now.

    Returns {engage, score, reason}. Deterministic and inspectable: the reason string says
    exactly which signal moved the score, so a misbehaving rule can be diagnosed from a log
    line instead of a debugger.
    """
    now = time.time()
    score = float(base_score)
    reason = "base"
    state = _load(conversation_id)

    # Active suppression window for this rule?
    sup = (state.get("suppressed") or {}).get(rule_id)
    if sup and float(sup) > now:
        return {"engage": False, "score": 0.0,
                "reason": f"suppressed for another {int(float(sup) - now)}s after standing down"}

    last = state.get("last") or {}
    if last.get("rule") == rule_id and (now - float(last.get("at") or 0)) <= FEEDBACK_WINDOW_S:
        p_norm = _norm(prompt)
        if _CONFUSION_RE.search(prompt or ""):
            score *= 0.05
            reason = "confusion signal right after this rule fired"
        elif p_norm and p_norm == last.get("prompt_norm"):
            score *= 0.05
            reason = "same prompt repeated verbatim after this rule fired"
        else:
            sim = _similarity(prompt, last.get("prompt") or "")
            if sim >= 0.6:
                score *= 0.15
                reason = f"same gist reworded (similarity {sim:.2f}) after this rule fired"

    engage = score >= ENGAGE_THRESHOLD
    if not engage:
        suppressed = state.get("suppressed") or {}
        suppressed[rule_id] = now + SUPPRESSION_S
        state["suppressed"] = suppressed
        _save(conversation_id, state)
    return {"engage": engage, "score": round(score, 3), "reason": reason}


def rule_ok(rule_id: str, prompt: str, conversation_id: str = "") -> bool:
    """The one-line guard every short-circuit uses: assess, and if engaging, record the fire.

    Fail-open: any internal error means the rule behaves exactly as it did before this layer
    existed.
    """
    try:
        verdict = assess(rule_id, prompt, conversation_id)
        if verdict["engage"]:
            record_engagement(rule_id, prompt, conversation_id)
            return True
        return False
    except Exception:
        return True
