"""Unified intent resolver: ONE classifier for the whole chat brain.

The problem this replaces: chat_with_memory routed via ~20 independent regex detectors checked in a
fixed order, so a message went to whichever detector's keywords happened to hit FIRST, not to the
intent it best matched. That is why "build the next RTM iteration" and "build the next WordPress
plugin" diverged, and why every added capability made routing more brittle.

The model here: score EVERY candidate intent for a message (a keyword signal plus embedding
similarity to curated anchors), then pick the single best by confidence with a margin over the
runner-up. One decision, globally, instead of a first-match race. Keyword hits are strong evidence
but no longer automatically win over a semantically closer intent; two keyword matches are broken by
meaning, not by list order.

Design:
  - Each intent declares: a keyword detector (reuse the existing role_router regexes), anchor
    phrasings (how a person actually asks), whether it is owner-only, and a small priority used ONLY
    to break genuine ties.
  - resolve(text, guest) returns (intent | None, confidence, why). None means "no intent is
    confident enough" and the caller falls through to the model/tool loop, exactly as today.
  - Degrades gracefully: with no embeddings model, scoring falls back to keyword-only, so behaviour
    is at worst the old keyword routing, never worse.

This module makes the DECISION. The handlers (rtm_operational_focus, time_alert_action, the plugin
authoring flow, etc.) are unchanged; chat_with_memory dispatches on the resolved intent.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Callable


def _site_terms() -> tuple[str, str, str]:
    """(RTM name regex, block-number alternation, block-code letters alternation) from the site profile.
    With the fictional default: ('example\\s*court', '1|3', 'ec'). Cached per profile selection in
    2-second buckets: resolve() runs several times a turn and a profile accessor re-reads settings."""
    import os
    import time
    return _site_terms_for((os.environ.get("MONGOOSE_PROFILE_FILE"), os.environ.get("MONGOOSE_DB_PATH"),
                            os.environ.get("MONGOOSE_DATA_DIR"), int(time.monotonic() // 2)))


@lru_cache(maxsize=8)
def _site_terms_for(_key: tuple) -> tuple[str, str, str]:
    from .. import profile as _profile
    name = r"\s*".join(re.escape(w.lower()) for w in _profile.rtm_name().split())
    bl = _profile.blocks()
    nums = "|".join(re.escape(n) for n in sorted({b["number"] for b in bl}, key=len, reverse=True))
    letters = "|".join(re.escape(x) for x in sorted({re.sub(r"^[0-9]+", "", b["code"]).lower() for b in bl},
                                                     key=len, reverse=True))
    return name, nums, letters


@lru_cache(maxsize=4)
def _site_res_for(name: str, nums: str, letters: str) -> "tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]":
    plugin_noun = re.compile(rf"\b(plugin|wordpress|wp\b|{name}|leaseholder\s+portal|rtm\s+portal|"
                             r"service\s+charges?\s+page|communications?\s+page|the\s+site|admin\s+site|"
                             r"rtm\s+build|spec|specification|requirements?|build\s+brief)\b", re.I)
    build_rtm = re.compile(r"\b(build|rebuild|deploy|author|ship|scaffold|improve|implement)\s+(the\s+)?"
                           rf"(next\s+)?(wordpress\s+|wp\s+)?(rtm|{name})\b", re.I)
    doc_signal = re.compile(rf"\b((?:{nums})\s?(?:{letters})\s?f?\s?\d|flat\s+\d|leaseholder|{name}|"
                            r"service\s+charge)\b", re.I)
    return plugin_noun, build_rtm, doc_signal


def _site_res() -> "tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]":
    """(_PLUGIN_NOUN, _BUILD_RTM_CMD, _DOC_RTM_SIGNAL), compiled for the active site profile."""
    return _site_res_for(*_site_terms())


# rtm_plugin is a COMMAND to build/change the RTM product surface. Its keyword signal: a plugin/site
# noun AND a build/author/deploy verb. This is what stops "rebuild the example court site" and "the
# plugin is hollow, make it real" from being stolen by the operational/knowledge intents. The noun
# regex (_site_res()[0]) includes the RTM company's name from the site profile.
# Verbs include gerund/plural forms (build/building/builds) and ITERATION verbs (keep/continue/resume/
# finish) so "keep building the plugin" / "continue the RTM build" / "finish the requirements" read as
# the authoring command, not a status query. Iteration verbs are safe here because _plugin_cmd requires
# a plugin NOUN alongside them, so "keep the lights on" cannot match.
_PLUGIN_VERB = re.compile(r"\b(build(?:s|ing)?|rebuild(?:s|ing)?|author(?:s|ing)?|write|rewrite|"
                          r"improve|make|ship|deploy|upload|push|scaffold|real|working|useful|better|"
                          r"redo|implement(?:s|ing)?|continue|keep|carry\s+on|resume|iterate|finish|"
                          r"complete|work\s+on)\b", re.I)


# A build COMMAND is imperative; a "what's left to build / how's the build going / is it ready" is a
# STATUS query and belongs to rtm_build, not the authoring flow. Exclude those shapes.
_PLUGIN_STATUS = re.compile(r"\b(what'?s\s+left|how'?s|how\s+is|going|coming\s+along|ready\s+yet|"
                            r"progress|status\s+of|what\s+is\s+left)\b", re.I)

# Bare "build/rebuild/deploy the RTM" (no "plugin"/"site" noun) is THE ambiguous phrase M hit: it used
# to fall to the read-only status path (an empty build plan). Consolidation decision: an imperative
# build of "RTM"/"example court" IS the plugin-authoring command (there is one RTM build, and it is the
# live plugin). Status queries are still caught by _PLUGIN_STATUS above, so "how's the RTM build going"
# stays on the status path. The build-command regex is _site_res()[1].


def _plugin_cmd(text: str) -> bool:
    t = text or ""
    if _PLUGIN_STATUS.search(t):
        return False
    plugin_noun, build_rtm, _doc = _site_res()
    if build_rtm.search(t):
        return True
    return bool(plugin_noun.search(t) and _PLUGIN_VERB.search(t))


# Guest invite: "give me a pair/invite/access link for Sam", "invite Sam", "login details for X". M
# says "pair link" and (by voice) "repair link"; all mean the same guest-access link. This is a real
# owner action the chat brain should DO, not hallucinate about ("software fix / shared document").
_INVITE_TERM = re.compile(r"\b(guest|invite|invitation|pair(?:ing)?|repair|log[\s-]?in|login)\b", re.I)
_LINK_TERM = re.compile(r"\b(link|code|details|invite|url|qr)\b", re.I)


def _guest_invite_cmd(text: str) -> bool:
    t = text or ""
    if re.search(r"\binvite\s+\w+", t, re.I) or re.search(r"\badd\s+\w+\s+as\s+a?\s*guest", t, re.I):
        return True
    return bool(_INVITE_TERM.search(t) and _LINK_TERM.search(t))


# Password reset as a CHAT action so it works on the phone (which has no reset UI, only chat). "reset
# Sam's password", "reset the password for sam", "give X a new password". Requires reset + a
# credential word so it does not fire on "reset the scheduler".
_RESET_PW_RE = re.compile(r"\breset\b.{0,20}\b(password|passcode|login|credential|pass)\b|"
                          r"\b(password|login|credential)\b.{0,20}\breset\b|"
                          r"\bgive\b.{0,30}\bnew\s+password\b|\bforgot\b.{0,20}\bpassword\b", re.I)


def _reset_pw_cmd(text: str) -> bool:
    return bool(_RESET_PW_RE.search(text or ""))


# RTM document GENERATION as a chat action: "generate a receipt for 3ECF9", "make Morgan's welcome
# letter", "draft the cover email for flat 5". A produce-verb + a document noun. RTM-specific nouns
# (receipt, welcome letter, cover email, demand) fire on their own; the generic 'letter'/'welcome' needs
# an RTM signal (a flat id / leaseholder / example court) so it does not steal "write a letter to my mum".
_DOC_VERB = re.compile(r"\b(generate|create|make|produce|draft|prepare|issue|write|send|e?-?mail|print|download|get\s+me)\b", re.I)
_DOC_STRONG = re.compile(r"\b(receipt|welcome\s+letter|cover(?:ing)?\s+e?-?mail|service\s+charge\s+demand|demand\s+letter)\b", re.I)
# The RTM signal regex (flat id / leaseholder / company name) is _site_res()[2], from the site profile.


def _rtm_doc_cmd(text: str) -> bool:
    t = text or ""
    # A FACTUAL question about what a document CONTAINS (the bank account, amount, reference) is NOT a
    # request to GENERATE one, so it must fall through to the knowledge/statement tools. Bug the MCP
    # connector hit: "what bank account is on the demands" was hijacked into "which flat? I'll generate
    # a receipt". Guard runs before the verb check because "get me / show the account on the demand"
    # carries a doc verb yet is still a question.
    if re.search(r"\b(what|which|where|whose|how\s+much|when|do|does|is|are|tell\s+me)\b", t, re.I) and \
       re.search(r"\b(account|bank|sort\s*code|payment\s+reference|pays?\s+into|paid\s+into|amount|balance|owes?|due)\b", t, re.I):
        return False
    if not _DOC_VERB.search(t):
        return False
    if _DOC_STRONG.search(t):
        return True
    if re.search(r"\b(welcome|letter)\b", t, re.I) and _site_res()[2].search(t):
        return True
    return False


# Fortune cookie: a delightful card. "generate fortune cookie", "make Jo a fortune", "give me a
# fortune". Fires on the phrase "fortune cookie" alone, or on "fortune" with a produce/gift verb.
_FORTUNE_RE = re.compile(r"\bfortune\b", re.I)
_FORTUNE_VERB = re.compile(r"\b(generate|make|create|give|gift|draw|produce|get|want|need|another|bake|conjure|summon)\b", re.I)


def _fortune_cmd(text: str) -> bool:
    t = text or ""
    if not _FORTUNE_RE.search(t):
        return False
    # A QUESTION about an existing cookie, or one addressed to a person to open theirs, is NOT a request
    # to bake a new one: "Jo what's in your fortune cookie?", "what does my fortune say?", "open your
    # cookie". Only bake when an explicit make/give verb is also present. (Bug: "Jo what's in your
    # fortune cookie???" used to bake a cookie because any mention of the words triggered it.)
    asks = re.search(r"what'?s|\bwhat\s+does\b|\bwhat\s+is\b|\binside\b|\bopen\b|\bcrack\b|\breveal\b|\bsays?\b|\?", t, re.I)
    if asks and not _FORTUNE_VERB.search(t):
        return False
    if _FORTUNE_VERB.search(t):
        return True
    # A bare "(a/my) fortune cookie" request with no interrogative is still a bake.
    return bool(re.search(r"\bfortune\s+cookie\b", t, re.I) and not asks)


def _kw(name: str) -> Callable[[str], bool]:
    """Late-bound keyword detector from role_router, so this module has no import cycle and a missing
    detector degrades to 'no keyword signal' rather than crashing."""
    def check(text: str) -> bool:
        try:
            from .. import role_router as rr
            fn = getattr(rr, name, None)
            return bool(fn(text)) if fn else False
        except Exception:
            return False
    return check


def _rx(const_name: str) -> Callable[[str], bool]:
    """PURE keyword signal from a role_router regex constant. Used instead of the looks_like_* wrappers
    for intents whose wrapper now carries a semantic fallback: the resolver owns all semantics, so its
    keyword layer must be regex-only or it double-counts (and the old router, blind to new intents like
    rtm_plugin, mis-fires)."""
    def check(text: str) -> bool:
        try:
            from .. import role_router as rr
            rx = getattr(rr, const_name, None)
            return bool(rx.search(text or "")) if rx is not None else False
        except Exception:
            return False
    return check


# ── ACTION-FIRST command classification ─────────────────────────────────────────────────────────
# The long-standing routing disaster came from keying intents off DOMAIN NOUNS ("rtm", "leaseholder")
# rather than the ACTION. A message's verb decides what it IS: "keep/add/note" captures a task;
# "mark ... paid" records a payment; "send ... receipt" emails one; "how much / who owes / arrears"
# queries the money view. The domain noun is only a MODIFIER. This deterministic pass runs BEFORE any
# embedding score, so short commands route by structure, not fuzzy similarity (which scored these
# 0.3 , 0.6 and abstained, letting a greedy bare-word "rtm" fallback dump the arrears report).
_CAPTURE_TASK = re.compile(
    r"\bkeep\s+(it|this|that|these|them)\b.{0,30}\b(task|to-?do|note|list|outstanding)\b"
    r"|\b(add|make|create|note|jot|log|save|put|start)\b.{0,28}\b(task|to-?do|note)\b"
    r"|\b(note|jot|write)\s+(this|that|it|down)\b"
    r"|\bmake\s+a\s+note\b"
    r"|^\s*(new|another)\s+(task|to-?do|note)\b"
    r"|\b(outstanding|open|new)\s+(rtm\s+)?task\b", re.I)
_TASK_LIST = re.compile(
    r"\b(what|which|show|list|any|how\s+many|see|view)\b.{0,30}\b(tasks?|to-?dos?)\b"
    r"|\bmy\s+(rtm\s+)?(tasks?|to-?do\s*list)\b", re.I)
_MARK_PAID = re.compile(
    r"\b(mark|record|set|log|register|note|flag)\b.{0,40}\bpaid\b"
    r"|\b(mark|record|log)\b.{0,40}\bpayment\b.{0,20}\b(received|in|as\s+received)\b", re.I)
_SEND_RECEIPT = re.compile(
    r"\b(send|email|resend|fire\s+off|shoot\s+over)\b.{0,48}\breceipts?\b", re.I)
_LIST_QUERY = re.compile(r"\b(what|which|show|list|any|how\s+many|see|view)\b", re.I)


def _action_intent(text: str) -> str | None:
    """Deterministic ACTION classifier for command utterances. Returns an intent name or None.
    Ordered by specificity: a send-receipt is never read as a generic document, a task listing is
    never read as a capture, and a task capture is never read as an arrears query."""
    t = text or ""
    if _SEND_RECEIPT.search(t):
        return "rtm_send_receipt"
    if _MARK_PAID.search(t):
        return "rtm_mark_paid"
    if _TASK_LIST.search(t) and _LIST_QUERY.search(t):
        return "task_list"
    if _CAPTURE_TASK.search(t) and not _LIST_QUERY.search(t):
        return "task_capture"
    return None


# The intent registry. `anchors` are curated positive phrasings; `keyword` is the existing detector
# used as a strong signal; `owner_only` gates owner-private intents; `priority` breaks exact ties only.
INTENTS: dict[str, dict[str, Any]] = {
    # RTM product BUILD as a COMMAND: author/improve/build/deploy the WordPress plugin. This is the
    # flow M wants "build the next RTM iteration" to hit (not an empty build-plan).
    "rtm_plugin": {
        "owner_only": True, "priority": 5,
        "keyword": _plugin_cmd,
        "anchors": [
            "build RTM", "build the RTM", "rebuild the RTM",
            "build the next iteration of the RTM plugin",
            "build the next RTM iteration",
            "build the next rtm version",
            "make the RTM product better and ship it",
            "rebuild the {rtm_name} site",
            "improve the WordPress plugin and build it",
            "the plugin is hollow, make it real",
            "deploy the RTM plugin to WordPress",
            "build and ship the leaseholder portal plugin",
        ],
    },
    # RTM document GENERATION (a chat action): produce a branded receipt / welcome letter / cover email
    # from real data and hand back a download link. Distinct from rtm_plugin (build the plugin) and
    # rtm_operational (arrears status). High priority so "generate a receipt" is not read as a query.
    "rtm_document": {
        "owner_only": True, "priority": 5,
        "keyword": _rtm_doc_cmd,
        "anchors": [
            "generate a receipt for flat 9", "make the receipt for {flat_example}",
            "draft the cover email for the Barnes receipt", "create the welcome letter for flat 8",
            "produce a service charge receipt", "send the leaseholder their receipt",
        ],
    },
    # RTM chat ACTIONS (send an existing receipt, record a payment). Distinct from rtm_document
    # (generate a doc). Priority 6 so a clear "send ... receipt" beats the document generator on ties.
    "rtm_send_receipt": {
        "owner_only": True, "priority": 6,
        "keyword": lambda t: _action_intent(t) == "rtm_send_receipt",
        "anchors": ["send the leaseholder their receipt", "email flat 3 their receipt",
                    "resend petrovs receipt", "send Barnes his receipt"],
    },
    "rtm_mark_paid": {
        "owner_only": True, "priority": 6,
        "keyword": lambda t: _action_intent(t) == "rtm_mark_paid",
        "anchors": ["mark the flat as paid", "record the payment as received",
                    "mark Barnes paid", "log Petrov's payment"],
    },
    # Task capture + listing: a to-do Mongoose owns. Capture is what "keep it as a task" / "add a
    # task" / "note this down" / an untimed "remind me to X" all become, instead of misrouting.
    "task_capture": {
        "owner_only": True, "priority": 5,
        "keyword": lambda t: _action_intent(t) == "task_capture",
        "anchors": ["keep it as a task", "add a task to call the plumber",
                    "make a note to renew the insurance", "keep this as an rtm task"],
    },
    "task_list": {
        "owner_only": True, "priority": 5,
        "keyword": lambda t: _action_intent(t) == "task_list",
        "anchors": ["what are my outstanding tasks", "show my to-do list",
                    "list my rtm tasks", "what tasks do I have"],
    },
    # Fortune cookie card: a delightful little feature, allowed for EVERYONE (owner and guests, private
    # and public/lounge chat), so owner_only is False. High priority so "make Jo a fortune" is not read
    # as something else.
    "fortune": {
        "owner_only": False, "priority": 5,
        "keyword": _fortune_cmd,
        "anchors": [
            "generate a fortune cookie", "make me a fortune cookie", "give me a fortune",
            "make Sam a fortune", "generate fortune cookie for Sam", "bake a fortune cookie",
        ],
    },
    # Capability showcase: "what can you do / what can I do with you" -> the warm feature list, NOT
    # the tour. Priority above tutorial and readiness so it wins "what can you do".
    "capabilities": {
        "owner_only": True, "priority": 3,
        "keyword": _kw("_never"),
        "anchors": [
            "what can you do", "what can I do with you", "what are your features",
            "how can you help me", "what are you capable of", "what are you good for",
        ],
    },
    # RTM build STATUS (a query, not a command): how is the build going / what is left.
    "rtm_build": {
        "owner_only": True, "priority": 1,
        "keyword": _rx("RTM_BUILD_INTENT"),
        "anchors": [
            "how is the RTM build going",
            "what is left to build for {rtm_name}",
            "is the RTM admin site ready yet",
            "progress on the {rtm_name} build",
        ],
    },
    "rtm_operational": {
        "owner_only": True, "priority": 3,
        "keyword": _rx("RTM_OPERATIONAL_INTENT"),
        "anchors": [
            "how are the arrears looking", "who owes service charge",
            "what is the money position for the block", "what service charges are overdue",
            "how much is owed on the flats", "any unpaid service charges",
        ],
    },
    "rtm_knowledge": {
        "owner_only": True, "priority": 2,
        "keyword": _kw("looks_like_rtm_knowledge"),
        "anchors": [
            "are the deeds verified", "what is the rounding situation",
            "what is the real budget for the block", "have the deeds been registered at HMLR",
        ],
    },
    "rtm_compliance": {
        "owner_only": True, "priority": 2,
        "keyword": _kw("_never"),
        "anchors": [
            "what are my compliance deadlines", "when is the confirmation statement due",
            "companies house filing deadlines", "statutory obligations coming up for the rtm",
        ],
    },
    # Benchmark: the overnight model-benchmark results. Carried over from semantic_router (the one
    # intent it had that this resolver lacked) so the two classifiers can collapse into this one.
    "benchmark": {
        "owner_only": True, "priority": 1,
        "keyword": _kw("_never"),
        "anchors": [
            "benchmark results", "how did the model benchmarks do", "show me the latest benchmark",
            "how are the models scoring", "what came out of the benchmarking",
        ],
    },
    "sync_checkpoint": {
        "owner_only": True, "priority": 2,
        "keyword": _kw("looks_like_sync_checkpoint"),
        "anchors": ["run a sync checkpoint", "sync now", "are we in sync", "checkpoint the state"],
    },
    "guest_invite": {
        "owner_only": True, "priority": 4,
        "keyword": _guest_invite_cmd,
        "anchors": [
            "give me a pair link for Sam", "create a guest invite for Sam", "invite link for Sam",
            "guest access link", "make a login link for my guest", "add Sam as a guest",
            "send Sam an invite", "generate a guest invite", "a link for my guest to log in",
        ],
    },
    "password_reset": {
        "owner_only": True, "priority": 4,
        "keyword": _reset_pw_cmd,
        "anchors": [
            "reset a password", "reset sam's password", "reset the password for my guest",
            "give someone a new password", "they forgot their password", "reset the login for",
        ],
    },
    "time_alert": {
        "owner_only": True, "priority": 4,
        "keyword": _kw("looks_like_time_alert"),
        "anchors": [
            "set a timer for ten minutes", "remind me to call the plumber at five",
            "set an alarm for seven am", "cancel all my timers", "what alarms do I have",
        ],
    },
    "readiness": {
        "owner_only": True, "priority": 1,
        "keyword": _rx("READINESS_INTENT"),
        "anchors": [
            "are you working", "is everything connected", "is everything working",
            "are your connectors ok", "what is broken right now",
            "is gmail connected", "system status", "what needs my attention",
        ],
    },
    "home_control": {
        "owner_only": True, "priority": 4,
        "keyword": _rx("HOME_INTENT"),
        "anchors": ["turn the lounge lights off", "dim the bedroom lights", "switch on the kitchen light",
                    "play some music in the lounge", "put on some jazz", "play some music"],
    },
    "home_read": {
        "owner_only": True, "priority": 2,
        "keyword": _rx("HOME_INTENT"),
        "anchors": ["are the lights on", "what is the temperature in the lounge", "is anything still on"],
    },
    "brief": {
        "owner_only": True, "priority": 3,
        "keyword": _kw("_never"),
        "anchors": ["give me my daily brief", "brief me on today", "what do I need to know today"],
    },
    "daily_picture": {
        "owner_only": True, "priority": 2,
        "keyword": _kw("_never"),
        "anchors": ["how is my day shaping up", "the big picture across everything", "my daily operating picture"],
    },
    "prepare": {
        "owner_only": True, "priority": 3,
        "keyword": _kw("_never"),
        "anchors": ["prepare me for my meeting", "help me get ready for my next appointment",
                    "brief me before the trustee meeting", "get me ready for this conversation"],
    },
    "suggestion": {
        "owner_only": True, "priority": 1,
        "keyword": _kw("_never"),
        "anchors": ["surprise me", "suggest something useful", "any suggestions for me"],
    },
    "tutorial": {
        "owner_only": True, "priority": 1,
        "keyword": _kw("looks_like_tutorial"),
        "anchors": ["show me around", "do something cool", "give me a tour", "introduce yourself"],
    },
    "delight": {
        "owner_only": True, "priority": 1,
        "keyword": _kw("looks_like_delight"),
        "anchors": ["tell me a fun fact", "tell me a joke", "cheer me up"],
    },
    "unstuck": {
        "owner_only": True, "priority": 2,
        "keyword": _kw("looks_like_unstuck"),
        "anchors": ["I am stuck", "I keep grinding and getting nowhere", "help me get unstuck"],
    },
    "body_state": {
        "owner_only": True, "priority": 3,
        "keyword": _kw("looks_like_body_log"),
        "anchors": ["log my swim", "how loaded am I", "record my physio session", "how recovered am I today"],
    },
}

_THRESHOLD = 0.60
_MARGIN = 0.05
_KEYWORD_WEIGHT = 0.60  # a keyword hit alone lands at 0.60 (above threshold); semantic can lift it


def _semantic_scores(text: str) -> dict[str, float]:
    """Best anchor cosine per intent, or {} when embeddings are unavailable. This module owns its own
    anchor taxonomy (INTENTS[*].anchors) and its own cache: it is the single classifier for the brain."""
    if "_ANCHOR_VECS" not in globals() or _ANCHOR_VECS is None:  # type: ignore[name-defined]
        _build_anchor_cache()
    if not _ANCHOR_VECS:  # type: ignore[name-defined]
        return {}
    from .embeddings import cosine, embed
    q = embed([text or ""])
    if not q:
        return {}
    qv = q[0]
    out: dict[str, float] = {}
    for intent, vecs in _ANCHOR_VECS.items():  # type: ignore[name-defined]
        out[intent] = max((cosine(qv, v) for v in vecs), default=0.0)
    return out


_ANCHOR_VECS: dict[str, list] | None = None


def _anchor_text(anchor: str) -> str:
    """Fill the site tokens an anchor may carry: {rtm_name} (the RTM company's name, lower case, as a
    person types it) and {flat_example} (a real flat id), both from the site profile."""
    if "{" not in anchor:
        return anchor
    from .. import profile as _profile
    return (anchor.replace("{rtm_name}", _profile.rtm_name().lower())
            .replace("{flat_example}", f"{_profile.block_codes()[-1]}F9"))


def _build_anchor_cache() -> None:
    global _ANCHOR_VECS
    from .embeddings import embed
    flat, spans = [], []
    for intent, spec in INTENTS.items():
        a = [_anchor_text(x) for x in (spec.get("anchors") or [])]
        start = len(flat)
        flat.extend(a)
        spans.append((intent, start, len(flat)))
    vecs = embed(flat) if flat else None
    if not vecs:
        _ANCHOR_VECS = None
        return
    _ANCHOR_VECS = {intent: vecs[a:b] for intent, a, b in spans}


def resolve(text: str, *, guest: bool = False) -> tuple[str | None, float, str]:
    """Return (intent, confidence, why) for a message, or (None, score, why) when nothing is
    confident enough. Combines a keyword signal with embedding similarity and picks the single best
    intent by a margin over the runner-up. Never raises."""
    t = (text or "").strip()
    if len(t) < 2:
        return (None, 0.0, "empty")
    try:
        # Comms-lookup veto: "read me the voicemail from the leaseholders", "who called me today", "any
        # messages from sam" belong to the tool loop (it searches voicemails/messages). They must not be
        # swallowed by an intent handler, so a comms lookup defers entirely (falls through to the model
        # + tools), mirroring the existing guard.
        if _kw("looks_like_comms_lookup")(t):
            return (None, 0.0, "comms lookup -> tool loop")
        # ACTION-FIRST: a command's verb decides it deterministically, before any fuzzy embedding
        # score. This is the fix for the routing disaster , task capture / mark-paid / send-receipt
        # now have real intents and win by structure, so "keep it as an rtm task" can never be read
        # as an arrears query again.
        act = _action_intent(t)
        if act is not None and not (guest and INTENTS.get(act, {}).get("owner_only")):
            return (act, 0.95, f"action:{act}")
        sem = _semantic_scores(t)
        scored: list[tuple[str, float, str]] = []
        for intent, spec in INTENTS.items():
            if guest and spec.get("owner_only"):
                continue
            kw = bool(spec.get("keyword", lambda _x: False)(t))
            s = sem.get(intent, 0.0)
            # Combined: a keyword hit is strong evidence (>= threshold on its own); semantic lifts it
            # and, crucially, differentiates two keyword matches by meaning.
            score = max(s, (_KEYWORD_WEIGHT if kw else 0.0)) + (0.25 * s if kw else 0.0)
            scored.append((intent, round(min(score, 1.0), 3), "kw+sem" if kw else "sem"))
        if not scored:
            return (None, 0.0, "no candidates")
        scored.sort(key=lambda x: (x[1], INTENTS[x[0]].get("priority", 0)), reverse=True)
        top, top_s, why = scored[0]
        runner_intent, runner, _ = scored[1] if len(scored) > 1 else ("", 0.0, "")
        if top_s < _THRESHOLD:
            return (None, top_s, f"below threshold ({top_s})")
        if (top_s - runner) >= _MARGIN:
            return (top, top_s, f"{why} (runner-up {round(runner,3)})")
        # Close scores: not low-confidence noise (both cleared threshold), so break the tie by intent
        # priority rather than punting to the model. Same-priority near-ties stay ambiguous -> None.
        if INTENTS[top].get("priority", 0) > INTENTS.get(runner_intent, {}).get("priority", 0):
            return (top, top_s, f"{why} tie-break by priority over {runner_intent}")
        return (None, top_s, f"ambiguous: {top}={top_s} vs {runner_intent}={runner}")
    except Exception as exc:  # never break chat routing
        return (None, 0.0, f"error:{exc}")
