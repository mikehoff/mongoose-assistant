"""DEPRECATED (consolidation Phase 1, 2026-07-08): the unified `intent_resolver` is now the single
classifier for the whole brain, and it carries this module's one unique intent (`benchmark`). Live
routing no longer calls this module: both former callers (`role_router._semantic_is` and
`app._semantic_intent_is`) go through `intent_resolver.resolve`. This file is kept for its isolated
tests during the grace period and will be removed in a later cleanup. Do not add new callers.

Embeddings-based intent fallback: when a chat message IS a capability request but worded in
a way the keyword detectors miss, route it to that capability anyway instead of handing the
clueless model a question it cannot answer.

This sits BEHIND the regex detectors (role_router + the app.py benchmark/capabilities hooks): a
detector fires if its own regex matches OR if this classifier's single best intent (argmax cosine
over curated anchor phrases) is that detector's intent and clears the intent threshold. classify()
returns exactly ONE winner, so two detectors can never both claim the same message semantically,
there is no double-routing.

Why anchors, not the generic capability_index: the index's per-capability text is thin (esp. for
the RTM/arrears domain, where matches were mediocre). Curated anchors are hand-written positives
for each intent, so a reworded "how are the arrears looking" lands at cosine ~0.8 against
"how are the arrears" rather than ~0.3 against a sparse index row. Anchors are also cross-lingual
for free (the model is multilingual), so a Polish rewording of the same intent still routes.

Local-first contract, strictly kept: if embeddings are unavailable (no model on disk, or no
onnxruntime/tokenizers), classify() returns (None, 0.0) and every caller falls back to pure
keyword behaviour: identical to before this module existed. Absence is a supported state.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from .embeddings import cosine, embed

# Curated positive phrasings per intent. Keep them DISTINCTIVE across intents: the classifier is a
# global argmax, so overlap between two intents' anchors is what causes a wrong route. Each list is
# a spread of how a real person asks for that capability, not synonyms of one sentence.
_ANCHORS: dict[str, list[str]] = {
    # RTM operational: real ledger figures (arrears, money position, payments to review, payables).
    # The owner's high-value domain, flagged as clearly failing.
    "rtm_operational": [
        "how are the arrears looking",
        "who owes service charge",
        "which leaseholders are behind on payments",
        "what is the money position for the block",
        "show me the payments to review",
        "what is overdue at example court",
        "how much is owed on the flats",
        "any unpaid service charges",
        "whats the arrears situation",
    ],
    # RTM build: how the RTM PRODUCT build is progressing (the admin/site), not the ledger.
    "rtm_build": [
        "how is the RTM build going",
        "what is left to build for example court",
        "how is the example court website coming along",
        "progress on the RTM admin site",
        "is the RTM build ready",
        "is the example court admin site done yet",
        "have you finished building the rtm",
    ],
    # Readiness: Mongoose's own health/connectivity, NOT its feature list.
    "readiness": [
        "are you working",
        "is everything connected",
        "is everything up and running",
        "are your connectors ok",
        "what is broken right now",
        "is gmail connected",
        "is the scheduler running",
        "system status",
        "what needs my attention",
        "is anything down",
    ],
    # Capabilities: what Mongoose can DO for the user (features/abilities), NOT health. Kept apart
    # from readiness deliberately , the owner objected that "what can I do with you" gave a health report.
    "capabilities": [
        "what can I do with you",
        "what can you do",
        "what are your features",
        "what are you able to help me with",
        "how can you help me",
        "what are you good for",
        "what are you capable of",
        "what are your powers",
    ],
    # Benchmark: the overnight model benchmark results.
    "benchmark": [
        "benchmark results",
        "how did the model benchmarks do",
        "show me the latest benchmark",
        "how are the models scoring",
        "what came out of the benchmarking",
    ],
    # Home control: acting on the lights/switches (write). Read-state lives in home_read.
    "home_control": [
        "turn off the lounge lights",
        "dim the bedroom lights",
        "switch on the kitchen light",
        "set the lights to warm white",
        "turn the lamp off",
    ],
    # Home read: asking the current state of the home, no action.
    "home_read": [
        "are the lights on",
        "what is the temperature in the lounge",
        "is anything still switched on",
        "what is the state of the house",
    ],
    # Daily brief: the cross-domain rundown Mongoose assembles.
    "brief": [
        "give me my daily brief",
        "brief me on today",
        "what is my morning brief",
        "run my brief",
        "what do I need to know today",
    ],
    # Daily operating picture: the at-a-glance shape of the day across every domain.
    "daily_picture": [
        "how is my day shaping up",
        "give me the big picture across everything",
        "my daily operating picture",
        "what does my day look like across all areas",
    ],
    # Prepare-me card: get me ready for a specific event, person, meeting or task.
    "prepare": [
        "prepare me for my meeting",
        "help me get ready for the call with sam",
        "prep me for tomorrow's appointment",
        "give me a prepare card for the trustee meeting",
    ],
    # Suggestion / surpriser: an unprompted useful nudge on demand.
    "suggestion": [
        "surprise me",
        "suggest something useful",
        "have you got any suggestions for me",
        "tell me something worth knowing right now",
    ],
    # RTM statutory compliance: company secretary deadlines (accounts, confirmation statement, s.20).
    "rtm_compliance": [
        "what are my compliance deadlines",
        "any statutory deadlines coming up",
        "when is the confirmation statement due",
        "companies house filing deadlines",
        "what statutory obligations are coming up for the rtm",
    ],
}

# Default acceptance threshold. A reworded anchor typically scores 0.65 to 0.9; unrelated chatter
# sits well below 0.5 (verified: benchmark vs arrears cosine ~0.18). 0.60 with a small margin over
# the runner-up keeps false routes rare. Per-intent overrides can tighten a risky intent.
_THRESHOLD = 0.60
_MARGIN = 0.05
_THRESHOLD_BY_INTENT: dict[str, float] = {
    # readiness and capabilities are near neighbours ("what's working" vs "what can you do"); demand
    # a touch more confidence so they do not steal each other's messages.
    "readiness": 0.62,
    "capabilities": 0.62,
}

_ANCHOR_VECS: dict[str, list[list[float]]] | None = None
_ANCHORS_UNAVAILABLE = False


def _ensure_anchor_vecs() -> dict[str, list[list[float]]] | None:
    """Embed every anchor once and cache. Returns None (and latches) when embeddings are absent, so
    repeated calls do not re-attempt a load that already failed."""
    global _ANCHOR_VECS, _ANCHORS_UNAVAILABLE
    if _ANCHOR_VECS is not None:
        return _ANCHOR_VECS
    if _ANCHORS_UNAVAILABLE:
        return None
    # Merge the base anchors with the utterances declared in the capability catalog, so the router
    # and the human showcase draw their phrasings from one reconciled source. Catalog is optional:
    # if it cannot be imported the base anchors still stand.
    merged: dict[str, list[str]] = {k: list(v) for k, v in _ANCHORS.items()}
    try:
        from . import capability_catalog as cat
        for intent in cat.all_intents():
            if intent in merged:
                for ex in cat.examples_for(intent):
                    if ex not in merged[intent]:
                        merged[intent].append(ex)
    except Exception:
        pass
    flat: list[str] = []
    spans: list[tuple[str, int, int]] = []
    for intent, phrases in merged.items():
        start = len(flat)
        flat.extend(phrases)
        spans.append((intent, start, len(flat)))
    vecs = embed(flat)
    if not vecs:
        _ANCHORS_UNAVAILABLE = True
        return None
    _ANCHOR_VECS = {intent: vecs[a:b] for intent, a, b in spans}
    return _ANCHOR_VECS


@lru_cache(maxsize=256)
def _classify_cached(text: str, min_score: float | None) -> tuple[str | None, float]:
    return _classify_impl(text, min_score)


def classify(text: str, *, min_score: float | None = None) -> tuple[str | None, float]:
    """Return (best_intent, score) for a message, or (None, 0.0) when nothing clears its threshold
    or embeddings are unavailable. Single global argmax: the returned intent is the one whose best
    anchor is closest to the message, and only if it beats the runner-up intent by _MARGIN and
    clears its own threshold. Never raises. Cached: within one chat turn every detector calls this
    on the same prompt, so the message is embedded once, not once per detector."""
    return _classify_cached((text or "").strip(), min_score)


def _classify_impl(t: str, min_score: float | None) -> tuple[str | None, float]:
    if len(t) < 3:
        return (None, 0.0)
    try:
        anchors = _ensure_anchor_vecs()
        if not anchors:
            return (None, 0.0)
        q = embed([t])
        if not q:
            return (None, 0.0)
        qv = q[0]
        # Best cosine per intent.
        best: list[tuple[str, float]] = []
        for intent, vecs in anchors.items():
            score = max((cosine(qv, v) for v in vecs), default=0.0)
            best.append((intent, score))
        best.sort(key=lambda x: x[1], reverse=True)
        top_intent, top_score = best[0]
        runner = best[1][1] if len(best) > 1 else 0.0
        floor = min_score if min_score is not None else _THRESHOLD_BY_INTENT.get(top_intent, _THRESHOLD)
        if top_score >= floor and (top_score - runner) >= _MARGIN:
            return (top_intent, round(top_score, 3))
        return (None, round(top_score, 3))
    except Exception:
        return (None, 0.0)


def is_intent(text: str, intent: str) -> bool:
    """Convenience for a detector: True if the semantic winner for this message is `intent`."""
    got, _ = classify(text)
    return got == intent


def debug(text: str) -> dict[str, Any]:
    """Per-intent scores for tuning/inspection (used by tests, not the hot path)."""
    anchors = _ensure_anchor_vecs()
    if not anchors:
        return {"available": False, "scores": {}}
    q = embed([text or ""])
    if not q:
        return {"available": False, "scores": {}}
    qv = q[0]
    scores = {intent: round(max((cosine(qv, v) for v in vecs), default=0.0), 3)
              for intent, vecs in anchors.items()}
    winner, score = classify(text)
    return {"available": True, "winner": winner, "score": score,
            "scores": dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))}
