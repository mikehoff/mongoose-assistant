from __future__ import annotations
from dataclasses import dataclass

import re
from typing import Callable

from . import profile as _profile


# ---------------------------------------------------------------------------------------------------------
# Site-profile terms in routing regexes. The RTM's name and the owner's name are per-install values, so the
# patterns that mention them are built from the site profile at use time (re's own pattern cache makes the
# rebuild cheap) instead of being frozen into a module-level compile.
# ---------------------------------------------------------------------------------------------------------

def rtm_name_rx(sep: str = r"\s*") -> str:
    """The RTM's name as a regex fragment, words joined by `sep` (default: optional whitespace)."""
    words = re.findall(r"[a-z0-9]+", _profile.rtm_name().lower())
    return sep.join(re.escape(w) for w in words)


def owner_name_rx() -> str:
    """The owner's name as a regex fragment (lower case), for 'can <owner> see ...' style phrasings."""
    return re.escape(_profile.owner_name().strip().lower())


def _lexicon_alt(name: str, base: list[str]) -> str:
    """Regex alternation of a routing word list (entries are regex fragments). A profile lexicon `name`
    REPLACES the generic base, so an install's routing matches exactly the words it defines, no more."""
    own = _profile.lexicon(name, None)
    terms = [str(t) for t in (own if own else base) if str(t).strip()]
    return "|".join(dict.fromkeys(terms))


class _ProfileRegex:
    """Quacks like a compiled pattern (search/match/fullmatch/findall/finditer/sub/pattern) but builds its
    pattern from the site profile on every use, so a profile change is picked up without a restart."""

    def __init__(self, build: Callable[[], str], flags: int = 0) -> None:
        self._build = build
        self.flags = flags

    def _rx(self) -> re.Pattern[str]:
        return re.compile(self._build(), self.flags)

    @property
    def pattern(self) -> str:
        return self._build()

    def search(self, *a, **k):
        return self._rx().search(*a, **k)

    def match(self, *a, **k):
        return self._rx().match(*a, **k)

    def fullmatch(self, *a, **k):
        return self._rx().fullmatch(*a, **k)

    def findall(self, *a, **k):
        return self._rx().findall(*a, **k)

    def finditer(self, *a, **k):
        return self._rx().finditer(*a, **k)

    def sub(self, *a, **k):
        return self._rx().sub(*a, **k)


# Read-only capabilities newly surfaced in chat (previously CLI-only): the daily brief, the daily
# operating picture, a prepare-me card, an on-demand suggestion, and RTM statutory compliance. Each
# maps to the SAME service the CLI runs. Regex for the clear phrasings, semantic router for rewordings.
_READ_CAP_REGEX = {
    "brief": re.compile(r"\b(brief me|my (daily |morning )?brief|the (daily |morning )?brief"
                        r"|give me (my |the )?brief|morning brief|daily brief)\b", re.IGNORECASE),
    "daily_picture": re.compile(r"\b(daily operating picture|operating picture|the big picture"
                                r"|how('?s| is) my day (shaping|looking|going))\b", re.IGNORECASE),
    "prepare": re.compile(r"\b(prepare me|prep me|help me prep|get me ready|prepare card|prepare-me)\b",
                          re.IGNORECASE),
    "suggestion": re.compile(r"\b(surprise me|suggest something|got any suggestions|any suggestions"
                             r"|a useful suggestion)\b", re.IGNORECASE),
    "rtm_compliance": re.compile(r"\b(compliance deadlines?|statutory (deadlines?|obligations?)"
                                 r"|confirmation statement|companies house|filing deadlines?"
                                 r"|section 20|s\.?20 (notice|consultation|timeline))\b", re.IGNORECASE),
}


def looks_like_read_capability(text: str) -> str | None:
    """Return which read-only capability intent a message wants (brief | daily_picture | prepare |
    suggestion | rtm_compliance), or None. Keyword first, then the embeddings router for rewordings."""
    t = text or ""
    for intent, rx in _READ_CAP_REGEX.items():
        if rx.search(t):
            return intent
    return None  # rewordings are handled by intent_resolver, not a second semantic pass here


CAPABILITIES_INTENT = re.compile(
    r"\bwhat can (i|you) do\b|\bwhat can i do with you\b|\bwhat do you do\b"
    r"|\bwhat are (you|your) (features|capabilities|good for)\b|\bwhat are you capable of\b"
    r"|\bhow can you help( me)?\b|\bwhat are your powers\b",
    re.IGNORECASE,
)


def looks_like_capabilities(text: str) -> bool:
    """A request for the capability SHOWCASE ("what can you do / what can I do with you"). Pure keyword;
    rewordings are handled by the unified intent_resolver's 'capabilities' intent."""
    return bool(CAPABILITIES_INTENT.search(text or ""))


def _semantic_is(text: str, intent: str) -> bool:
    """Embeddings fallback for the keyword detectors: True if the UNIFIED intent resolver's single best
    intent for this message is `intent`. Routed through intent_resolver (not the deprecated
    semantic_router) so the whole brain uses ONE classifier and embeds each message once per turn.
    Cheap False when embeddings are unavailable, so detectors stay pure-keyword. Never raises."""
    try:
        from .services.intent_resolver import resolve
        return resolve(text)[0] == intent
    except Exception:
        return False

# Rule-based role classifier. Deterministic and cheap: it runs before any model
# is chosen, so it must not need a model itself. The goal is to send tool/home
# and code turns to the Qwen models (reliable tool calling, stronger code) and
# leave everything else on the Gemma persona model.
#
# This is intentionally a first pass. It is conservative: when nothing matches,
# it returns "chat", because the persona model is the safe default voice and
# misrouting a normal chat turn to a heavier model is the costlier mistake on a
# 24 GB machine. Swap this for a small classifier model later if needed; the
# interface (text -> role) stays the same.

# Code signals: fenced blocks, common code punctuation density, or explicit asks.
_CODE_FENCE = re.compile(r"```")
# Strong code signals: an explicit programming verb, a language/format name, or
# error-output terms. Any one of these is enough.
_CODE_STRONG = re.compile(
    r"\b(refactor|debug|stack ?trace|traceback|"
    r"regex|sql query|bash script|dockerfile|json schema|"
    r"python|javascript|typescript|golang|rust code|compile)\b",
    re.IGNORECASE,
)
# Soft code words that also occur in ordinary speech ("function of the spleen",
# "a class of problems"). These only count as code when paired with a coding
# verb, so the anatomy question stays in chat.
_CODE_VERB = re.compile(r"\b(write|fix|implement|refactor|debug)\b", re.IGNORECASE)
_CODE_SOFT = re.compile(r"\b(function|class|method|variable|loop|array|yaml)\b", re.IGNORECASE)
# Strong syntax signals: operators and statement forms that don't occur in
# ordinary prose. Any one of these counts toward "looks like code".
_CODE_SYNTAX = re.compile(r"=>|::|->|==|!=|;\s|\bdef |\bimport |\basync def |SELECT .*FROM|\w+\(\w*\)\s*[{:]|[{}]\s*$", re.IGNORECASE | re.MULTILINE)
# A function-call shape like name(args) or name(): also a code signal, but a
# bare "(word)" parenthetical in prose is NOT.
_CODE_CALL = re.compile(r"\b\w+\([^)]*\)\s*[:{]?|\bclass \w+|\bfunction \w+\(")

# Canonical home-control intent. This is the SINGLE source of truth shared by
# the role classifier (typed /llm/chat, mongoose chat) and the /assist Assist
# detector in tools.py, so the same natural command ("close the blinds", "set
# temperature to 19") routes to home control on every surface. Previously the two
# had drifted: the Assist detector knew open/close/blinds/curtains/temperature but
# the role router did not, so typed and voice disagreed.
HOME_INTENT = re.compile(
    r"\b(turn (on|off)|switch (on|off)|dim|brighten|brightness|lights?|lamp|plug|socket|"
    r"thermostat|heating|temperature|play|pause|volume|speaker|blinds?|curtains?|scene|"
    r"automation|lock|unlock|"
    # open/close only count as home control when paired with an openable thing,
    # so "close the blinds" matches but "close reading" / "open question" do not.
    r"(open|close)\s+(the\s+|my\s+)?(blinds?|curtains?|garage|door|gate|window|shutters?))\b",
    re.IGNORECASE,
)


# Interrogative / state-read phrasing: a question ABOUT the home ("are the lights on?", "is the garage
# open?", "what's on?") is a READ, not a command. We must not classify these as home CONTROL, or the
# planner could route a question to the actuating tool. These patterns mark a prompt as a state query.
HOME_READ_INTENT = re.compile(
    r"\b(are|is|was|were|what'?s?|which|how many|any)\b.*\b(on|off|open|closed|locked|unlocked|"
    r"running|playing|set|lit|active|home|in|upstairs|downstairs)\b"
    r"|\bwhat'?s\b.*\b(on|happening|going on)\b"
    r"|\b(is|are)\b.*\b(garage|door|gate|window|blinds?|curtains?|shutters?|lights?|heating|tv|"
    r"thermostat)\b"
    r"|\bstatus of\b|\bstate of\b",
    re.IGNORECASE,
)


def looks_like_home_read(text: str) -> bool:
    """A question about home STATE (read), as opposed to a command to change it. Used so the planner
    prefers the read-only home_state tool for questions and never actuates on a query."""
    t = text or ""
    # must mention a home-ish thing AND be phrased as a question/state lookup
    if bool(HOME_INTENT.search(t)) and bool(HOME_READ_INTENT.search(t)):
        return True
    return _semantic_is(t, "home_read")


def looks_like_home_control(text: str) -> bool:
    t = text or ""
    if not HOME_INTENT.search(t):
        # No home noun matched the keyword list; trust only the embeddings fallback, which has its
        # own control anchors kept distinct from the read anchors.
        return _semantic_is(t, "home_control")
    # A state question ("are the lights on?") mentions home things but is a READ, not a command.
    # Don't classify it as control , unless it ALSO contains an explicit imperative verb, in which
    # case the command wins (e.g. "the lights are on, turn them off").
    if looks_like_home_read(t):
        imperative = re.search(r"\b(turn|switch|dim|brighten|set|lock|unlock|open|close|play|pause|"
                               r"stop|start|run|activate|toggle)\b", t, re.IGNORECASE)
        if not imperative:
            return False
    return True


# Readiness / self-status intent: questions about whether Mongoose is working, what's
# connected, what's stale/broken, what needs the user, or what it's watching. These should be
# answered from the readiness layer (real evidence), not generic chat (which would guess).
READINESS_INTENT = re.compile(
    r"\b(are you (ready|working|ok|online|up)|"
    r"what can (i|you) (use|see|access)( now)?|"
    r"what('?s| is) (ready|working|broken|stale|connected|wrong|down|missing)|"
    r"what needs (me|doing|fixing|attention)|"
    r"what are you (watching|tracking|monitoring|seeing)|"
    r"is (gmail|email|whatsapp|home ?assistant|my phone|the scheduler|voice|mongoose) "
    r"(working|connected|ready|set ?up|online|reachable)|"
    r"(did|didn'?t|why didn'?t) (you|i) (get|run) (a |the )?brief|"
    r"why can'?t you (see|access|read)|"
    r"(system|mongoose) (status|health)|"
    r"fix what you can)\b",
    re.IGNORECASE,
)


def looks_like_readiness_query(text: str) -> bool:
    """True if the user is asking about Mongoose's own readiness/status, route to the
    readiness layer rather than answering from the model."""
    return bool(READINESS_INTENT.search(text or ""))  # semantics now owned by intent_resolver


# RTM operational intent: asking about the building's service-charge state , arrears, who's paid,
# money position, payments waiting to allocate, payables due, the dashboard. Routes to the RTM
# management layer (real ledger), not generic chat. Read-only: answering these never sends or pays.
# Operational intent = a QUERY about the RTM's money STATE. It must require a state/query signal
# (arrears, who owes, money position, unpaid demands, dashboard). A bare domain noun ("rtm",
# "leaseholder", "the flats") is NOT enough , that greedy match was the long-standing disaster that
# dumped the arrears report on "keep it as an outstanding rtm task". Domain nouns are modifiers here,
# never triggers. Command shapes (mark paid, keep as task, send receipt) are owned by the action
# classifier in intent_resolver and never reach this regex.
RTM_OPERATIONAL_INTENT = _ProfileRegex(lambda: (
    r"\b(arrears|overdue|in\s+recovery)\b"
    r"|\bwho\b.{0,24}\b(owe[sd]?|owing|behind|not\s+paid|hasn'?t\s+paid|in\s+arrears)\b"
    r"|\b(money|fund|cash)\s+position\b"
    r"|\bhow\s+much\b.{0,30}\b(owe[sd]?|owing|arrears|collected|paid|outstanding|demanded|in)\b"
    r"|\b(unpaid|outstanding|unallocated)\b.{0,30}\b(service\s+charge|charges|demand|demands|flats?|leaseholders?|balance)\b"
    r"|\b(demand|demands|payment)\b.{0,30}\b(review|allocate|unallocated|reconcil\w*)\b"
    r"|\b(payables?|cashflow)\b.{0,30}\b(rtm|park|service|sch1|sch2|reserve|due|scheduled)\b"
    rf"|\b(rtm|{rtm_name_rx()})\b.{{0,30}}\b(dashboard|money\s+position|fund\s+position|arrears|figures?|financials?|owe[sd]?|owing|outstanding)\b"),
    re.IGNORECASE,
)


def looks_like_rtm_operational(text: str) -> bool:
    """True if the user is asking about the RTM's operational state (arrears, payments, money,
    payables, dashboard). Routes to the management layer's real figures. Keyword match OR the
    embeddings fallback (catches rewordings like "whats the arrears situation" the regex misses)."""
    return bool(RTM_OPERATIONAL_INTENT.search(text or ""))  # semantics now owned by intent_resolver


def rtm_operational_focus(text: str) -> str:
    """Which operational view the user wants: arrears | review | payables | dashboard."""
    low = (text or "").lower()
    if "arrears" in low or "overdue" in low or "owes" in low or "owing" in low or "behind" in low or "recovery" in low:
        return "arrears"
    if "review" in low or "allocate" in low or "unallocated" in low or "reconcil" in low or ("payment" in low and "paid" not in low):
        return "review"
    if "payable" in low or "supplier" in low or "cashflow" in low or "owe" in low and "owed" not in low:
        return "payables"
    return "dashboard"


# RTM build/development intent: asking how the RTM PRODUCT build is going (the WordPress/admin
# surface), what's left to build, whether to approve the build. A different level from operational:
# this is about Mongoose building the RTM, not running it.
RTM_BUILD_INTENT = _ProfileRegex(lambda: (
    rf"\b(rtm|{rtm_name_rx()})\b.*\b(build|building|develop\w*|plan|wordpress|admin|progress|roadmap)\b"
    rf"|\b(build|develop\w*)\b.*\b(rtm|{rtm_name_rx()})\b"
    rf"|\bhow('?s| is)\b.*\b(rtm|{rtm_name_rx()})\b.*\b(coming|going|build)\b"),
    re.IGNORECASE,
)


def looks_like_rtm_build(text: str) -> bool:
    """True if the user is asking about the RTM product BUILD (WordPress/admin surface, plan
    progress), as opposed to the operational state of the block."""
    return bool(RTM_BUILD_INTENT.search(text or ""))  # semantics now owned by intent_resolver


# RTM capability/real-data intent: "what can the rtm do", "what's the rounding situation", "are the
# deeds verified", "what's the real budget". Routes to the capability/real-data knowledge.
RTM_KNOWLEDGE_INTENT = _ProfileRegex(lambda: (
    rf"\b(rtm|{rtm_name_rx()})\b.*\b(capabilit\w*|rounding|deed|deeds|verif\w*|real (data|budget)|budget|caveat)\b"
    rf"|\bwhat can\b.*\b(rtm|{rtm_name_rx()})\b.*\bdo\b"
    rf"|\b(rtm|{rtm_name_rx()})\b.*\b(capabilit|do (now|right now))\b"
    r"|\b(rounding (delta|situation|issue))\b"
    r"|\b(are|have)\b.*\bdeeds?\b.*\bverif\w*\b"
    r"|\bdeeds?\b.*\b(verif\w*|hmlr|registered)\b"
    rf"|\b(what('?s| is)\b.*\b(rounding|deed|the budget|{rtm_name_rx(' ')} budget))\b"),
    re.IGNORECASE,
)


def looks_like_rtm_knowledge(text: str) -> bool:
    """True if the user is asking about RTM capability state or the RTM's real-data facts and
    caveats (rounding, deeds, budget)."""
    return bool(RTM_KNOWLEDGE_INTENT.search(text or ""))


# Sync-checkpoint intent: "run a checkpoint", "sync with my claude", "check in".
RTM_SYNC_INTENT = re.compile(
    r"\b(run|do|start)\b.*\b(sync|checkpoint|check.?in)\b"
    r"|\b(sync (now|with|up))\b|\bcheck in with (my )?(claude|chat)\b",
    re.IGNORECASE,
)


def looks_like_sync_checkpoint(text: str) -> bool:
    """True if the user is asking to run/see a sync checkpoint."""
    return bool(RTM_SYNC_INTENT.search(text or ""))


# Time Alerts intent: setting/cancelling/listing alarms, timers, reminders. These route to the
# time_alerts layer (deterministic time parsing + the ledger), never to generic chat.
TIME_ALERT_INTENT = re.compile(
    r"\b(set|create|start|make|add|cancel|clear|delete|remove|stop|list|show)\b.*\b"
    r"(alarm|timer|reminder|remind)s?\b"
    r"|\bwhat\b.*\b(alarm|timer|reminder)s?\b.*\b(have|set|got|there)\b"
    r"|\b(do i have|any)\b.*\b(alarm|timer|reminder)s?\b"
    r"|\b(wake me|wake us|remind me|remind us|nudge me|nudge us|nudge)\b"
    r"|\bset a (\d+\s*(?:min|minute|hour|sec|second))",
    re.IGNORECASE,
)


def looks_like_time_alert(text: str) -> bool:
    """True if the user is setting/cancelling/listing an alarm, timer or reminder."""
    return bool(TIME_ALERT_INTENT.search(text or ""))


def time_alert_action(text: str) -> str:
    """Classify the time-alert action: 'set' | 'cancel' | 'list'."""
    low = (text or "").lower()
    if re.search(r"\b(cancel|clear|delete|remove|stop)\b", low):
        return "cancel"
    if re.search(r"\b(what|list|show|do i have|any)\b", low):
        return "list"
    return "set"


# Delight , the warm companion register: a joke, a fun fact, "tell me something fun/interesting",
# "cheer me up", "lighten the mood". Routes to the delight layer (interest-aware), NOT the tutorial
# (capability tour) and NOT the raw model. Checked BEFORE tutorial so "tell me a joke" lands here.
DELIGHT_INTENT = re.compile(
    r"\b(tell me|got|have|share|hit me with|give me)\b.*\b(a )?(joke|jokes|fun fact|funny|"
    r"something (fun|funny|interesting|cool))\b"
    r"|\b(make me (laugh|smile)|cheer me up|lighten (the mood|things up|my day)|brighten my day)\b"
    r"|\b(fun fact|did you know|tell me something (i don'?t know|interesting))\b"
    r"|^\s*(a )?joke\s*$|\banother (joke|one|fact)\b",
    re.IGNORECASE,
)


def looks_like_delight(text: str) -> bool:
    """True if the user wants a light moment, a joke, a fun fact, cheering up. Interest-aware."""
    return bool(DELIGHT_INTENT.search(text or ""))


# Tutorial / self-introduction / "do something cool". These route to the tutorial layer (a
# grounded, adaptive intro), not generic chat.
TUTORIAL_INTENT = re.compile(
    r"\b(introduce yourself|who are you|what can you do|what do you do|"
    r"what are you( able to do| capable of)?|show me what you can do|"
    r"how do (i|you) (use|work)|give me a tour|tutorial|getting started|"
    r"do something (cool|impressive)|impress me|show me something)\b",
    re.IGNORECASE,
)


def looks_like_tutorial(text: str) -> bool:
    """True if the user is asking Mongoose to introduce itself / show what it can do."""
    return bool(TUTORIAL_INTENT.search(text or ""))


def tutorial_action(text: str) -> str:
    """'cool' for a demo request, else 'intro'."""
    low = (text or "").lower()
    if re.search(r"\b(cool|impressive|interesting|fun|impress me|show me something)\b", low):
        return "cool"
    return "intro"


# Body State: logging movement/physio/swim/body-comp/subjective notes, and asking about
# load/readiness ("how loaded am I", "should I lift or swim"). Routes to the body-state layer.
BODY_LOG_INTENT = re.compile(
    r"\b(log|record|note)\b.*\b(workout|gym|leg day|swim|swam|physio|mobility|stretch|"
    r"weigh|weighed|body ?state|trained|session|lift)\b"
    r"|\bbody ?state\s*[:,]"
    r"|\bweighed\s+\d|\bi weigh\s+\d"
    r"|\b(swam|did legs|trained legs|just swam|just trained)\b",
    re.IGNORECASE,
)
# Body-routing vocabulary: a generic base in code, replaced by the site profile's lexicon when it defines one
# (entries are regex fragments).
def _recovery_modalities() -> str:
    return _lexicon_alt("body.recovery_modalities", ["yoga"])


def _guarded_muscles() -> str:
    return _lexicon_alt("body.guarded_muscles", ["calf"])


def _tracked_muscles() -> str:
    return _lexicon_alt("body.tracked_muscles", ["delts", "calves", "arms", "quads"])


def _muscle_stems() -> str:
    return _lexicon_alt("body.muscle_stems", ["delt", "calf", "arm", "quad"])


def _muscle_goal_phrases() -> str:
    return _lexicon_alt("body.muscle_goal_phrases", ["target muscle"])


BODY_READINESS_INTENT = _ProfileRegex(lambda: (
    r"\b(how loaded|how recovered|am i recovered|body ?state today|how('?s| is) my body|"
    r"should i (lift|train|swim|rest)|good day (for|to) (lift|train|legs|swim)|"
    r"what('?s| is) my (load|recovery|readiness)|how('?s| is) my (load|recovery)|"
    r"am i (ready|under.?recovered)|how do i look today|stress ?load|recovery ?load|"
    r"loaded do i (look|feel)|"
    # the user's actual register , concrete body-language, not sports-science
    r"how('?s| is) my body|what('?s| is) my body state|what kind of body day|"
    r"am i good to train|do i look under.?recovered|am i fried|am i (just )?tense|"
    r"am i (tired|held|guarded|braced|cooked|running hot)|"
    r"is this a (lift|reset|swim|heavy|gentle) day|do i need (movement|rest)|"
    r"does my body (want|need)|am i puffy|puffy.?face|puffiness|de.?puff|"
    r"is this a swim day|would swim(ming)? help|should i swim or lift|"
    rf"would (?:{_recovery_modalities()})|"
    r"do i need physio|what physio|proprioception day|pre.?gym|"
    rf"is my right side|right.?side (weird|guarding)|right (?:{_guarded_muscles()}) (online|underwork)|"
    r"best session|best (training )?option|high.?roi (move|session)|least stupid (training )?option|"
    r"anything (useful|weird|i should notice|i'?m ignoring)( for my body)?|"
    r"smallest useful body)\b"),
    re.IGNORECASE,
)


def looks_like_body_log(text: str) -> bool:
    """True if the user is logging a body/movement/state entry."""
    return bool(BODY_LOG_INTENT.search(text or ""))


def looks_like_body_readiness(text: str) -> bool:
    """True if the user is asking about body state / recovery / readiness / what session, OR
    asking about a body topic we haven't built yet (so we can answer honestly rather than letting
    it fall through to generic chat)."""
    return bool(BODY_READINESS_INTENT.search(text or "")) or bool(_BODY_NOT_BUILT.search(text or ""))


# Genuinely not-built-yet topics: per-muscle progression (needs sets import) and nutrition/food
# effects (needs MFP import). These still route to an honest "not built yet". The journalling /
# proprioception flows are NOW BUILT (body_literacy), so they're no longer here.
_BODY_NOT_BUILT = _ProfileRegex(lambda: (
    rf"\b(are my ({_tracked_muscles()})|am i (building|gaining) (the right )?muscle|"
    r"muscle or (just )?water|overdoing quads|waist (thicker|getting)|hypertrophy signal|"
    rf"(?:{_muscle_goal_phrases()})|my (current )?plan|current split|what am i (running|doing|training)|"
    r"what have i trained|muscle trend|am i progressing|are my .* progressing|"
    r"did i (under.?eat|hit (enough )?protein)|is protein|low.?carb|sweets|lactose|gluten|"
    r"wreck(ed)? my face|water retention|food correction)\b"),
    re.IGNORECASE,
)

# The guided body-literacy flows (NOW BUILT): body check-in, proprioception/grounding reset,
# emotional-literacy journalling. These start a multi-turn flow.
BODY_FLOW_INTENT = re.compile(
    r"\b(start (a |an |the )?(body|emotional.?literacy|feelings?) (check.?in|journal)|"
    r"body check.?in|do a check.?in|"
    r"start (a |the )?(grounding|proprioception|body) reset|grounding reset|proprioception reset|"
    r"pre.?gym (primer|reset)|give me (a )?(primer|reset)|"
    r"help me (name|feel|get back into my body|unbrace|get grounded|ground)|"
    r"name what i'?m feeling|help me (work out |figure out )?what (i'?m feeling|this (feeling|is))|"
    r"journal( this| about)?|write (this )?down|process this)\b",
    re.IGNORECASE,
)


def looks_like_body_flow(text: str) -> bool:
    """True if the user wants to START a guided body-literacy flow."""
    return bool(BODY_FLOW_INTENT.search(text or ""))


# Unstucking / active-recovery. The user asking to be unstuck, or what would help when grinding.
# Distinct from readiness ("how's my body") , this is "I'm stuck, more effort isn't working".
UNSTUCK_INTENT = re.compile(
    r"\b(i'?m stuck|i am stuck|feeling stuck|i feel stuck|unstick me|help me get unstuck|"
    r"i'?m grinding|i am grinding|i'?m in a rut|i am in a rut|spinning my wheels|"
    r"can'?t (get going|get moving|focus)|cannot (get going|get moving|focus)|"
    r"what would (actually )?help|what should i do (to|when) (reset|unstick|recover)|"
    r"i need to (get out of|break out of)|what unsticks me|what helps me reset|"
    r"i'?ve been pushing (too )?hard|i have been pushing (too )?hard|"
    r"i'?m pushing too hard|i can'?t (stop|switch off)|i cannot (stop|switch off))\b",
    re.IGNORECASE,
)


def looks_like_unstuck(text: str) -> bool:
    """True if the user is stuck/grinding and wants the active-recovery nudge (not a readiness read)."""
    return bool(UNSTUCK_INTENT.search(text or ""))


def unstuck_action(text: str) -> str:
    """'what_helps' if asking what tends to help them, else 'unstick' (give the nudge now)."""
    low = (text or "").lower()
    if re.search(r"what (unsticks|helps).*me|what tends to help|what'?s helped", low):
        return "what_helps"
    return "unstick"


def body_flow_kind(text: str) -> dict[str, str]:
    """Classify which flow + (optional) sub-kind to start."""
    low = (text or "").lower()
    if re.search(r"reset|primer|ground|unbrace|feel my|get back into|proprioception", low):
        kind = ""
        if "primer" in low or "pre-gym" in low or "pre gym" in low:
            kind = "primer"
        elif "60" in low or "sixty" in low:
            kind = "60s"
        elif "5" in low or "five" in low or "balance" in low:
            kind = "5min"
        elif "3" in low or "three" in low:
            kind = "3min"
        return {"flow": "reset", "kind": kind}
    if re.search(r"journal|write (this )?down|process this|after", low):
        sub = ""
        if "difficult" in low or "interaction" in low or "after" in low or "argument" in low:
            sub = "difficult"
        return {"flow": "journal", "kind": sub}
    if re.search(r"name|feel|figure out|work out what", low):
        # "name what I'm feeling" -> the naming journal; "body check-in" -> check-in
        if "check" in low:
            return {"flow": "checkin", "kind": ""}
        return {"flow": "journal", "kind": "naming"}
    return {"flow": "checkin", "kind": ""}


def body_readiness_focus(text: str) -> str:
    """Classify which focused body answer to give. Returns one of: swim_or_lift, puffy,
    right_side, physio, best_session, today (general), or a not-built topic prefixed 'nb:'."""
    low = (text or "").lower()
    # not-built topics first , recognise honestly
    if _BODY_NOT_BUILT.search(low):
        if re.search(rf"{_muscle_stems()}|muscle|waist|hypertrophy|40 ?kg|plan|split|"
                     r"trained|training|progress", low):
            return "nb:progression"
        if re.search(r"eat|protein|carb|sweets|lactose|gluten|face|water retention|food", low):
            return "nb:nutrition"
        return "nb:journalling"
    if re.search(r"swim or lift|should i swim|swim day|would swim", low):
        return "swim_or_lift"
    if re.search(r"puffy|puffiness|de.?puff", low):
        return "puffy"
    if re.search(rf"right side|right.?side|right (?:{_guarded_muscles()})", low):
        return "right_side"
    if re.search(r"physio|proprioception|pre.?gym|primer", low):
        return "physio"
    if re.search(r"best (session|option)|high.?roi|least stupid|what session", low):
        return "best_session"
    return "today"


# Tool / home-control / reasoning signals. These are turns where Mongoose is
# expected to *do* something or route to a tool, where Gemma's weaker tool
# calling would cost reliability.
_TOOL_KEYWORDS = _ProfileRegex(lambda: (
    r"\b(remind me|set (a )?(timer|alarm|reminder)|"
    r"schedule|calendar|book (a|an|the|me)\b|"
    r"search my (email|inbox|mail|calendar|files?|notifications?)|"
    r"send (a )?(notification|alert)|"
    # person-to-person messaging via the mailbox (owner<->guest). "tell Sam ...", "send Sam a
    # message", "message Sam that ...", "let <name> know ...", "leave <name> a message".
    # A negative lookahead excludes me/us/you/everyone-less self/AI-directed phrasing like
    # "tell me a joke" so normal chat isn't misrouted.
    r"(send|leave) (a )?(message|note|text) (to |for )?(?!me\b|us\b|you\b)\w+|"
    r"(send|leave) (?!me\b|us\b|you\b)\w+ (a )?(message|note|text)\b|"
    r"(send|message|text) (?!me\b|us\b|you\b)\w+ (a )?(message|note|text|hello|hi|that\b|to\b)|"
    r"\b(tell|message|text|let)\s+(?!me\b|us\b|you\b|him\b|her\b|them\b)\w+\s+(that |to |i |we |a message|a note|know\b)|"
    r"\bsend\s+(?!me\b|us\b|you\b)\w+\s+(a\s+)?(message|note|text|word)|"
    r"\bpass (on |along )?(a )?(message|note) (to )?\w+|"
    r"what'?s on|am i free|availability|"
    # task/todo and integration status queries need the tool planner
    r"(my )?(tasks?|to.?do|todo|task list)|"
    r"(show|list|get|check) (my )?(tasks?|to.?do|todo)|"
    r"(folders?|lists?) (in |of )?(todo|to.?do|tasks?)|"
    r"(integrations?|connectors?) (working|active|set up|configured|connected)|"
    r"(what|which) integrations?|"
    # connector/service health , by name or generically: "is gmail working", "are my connectors ok",
    # "check home assistant", "is my email connected", "why won't my calendar sync"
    r"(is|are|why( is|n'?t| are| won'?t| isn'?t)?|check|test)\b.{0,40}\b(gmail|google ?(calendar|drive)|"
    r"calendar|drive|outlook|microsoft|to.?do|imap|mailbox|email|e-?mail|home ?assistant|\bha\b|ollama|"
    r"brain|connectors?|integrations?|connection)\b.{0,30}\b(working|connected|ok|okay|up|down|"
    r"sync(ing|ed)?|broken|set up|configured|reachable|usable|live|status)|"
    # bare "check/test <connector>" or "<connector> connection" (verb or noun carries the intent)
    r"(check|test|probe)\b.{0,20}\b(gmail|google ?(calendar|drive)|calendar|drive|outlook|microsoft|"
    r"to.?do|imap|mailbox|home ?assistant|\bha\b|ollama|brain|connectors?|integrations?)\b|"
    r"\b(gmail|google ?(calendar|drive)|calendar|drive|microsoft|to.?do|imap|home ?assistant|ollama)\b "
    r"connection\b|"
    r"\b(connectors?|connections?) (status|health|state|ok|okay)|"
    r"are my connectors|how are my connectors|"
    # self-reflection / self-audit of the brain's own wiring
    r"(check|audit|reflect on|inspect|verify) (your|its|the) own (wiring|tools|capabilities|setup)|"
    r"audit yourself|self.?reflect|self.?audit|are all your tools (reachable|wired|available)|"
    # voicemail lookup is a normal data-search tool; protocol-v3 voicemail is only the HA/Pi staging lane.
    r"(any|new|latest|recent|what|check|show|list|read|search|find|look ?up|do i have|have i got|are there|is there|do i)\b.{0,30}\b(voice ?mails?|voicemails?|missed calls?)|"
    r"(voice ?mails?|voicemails?|missed calls?)\b.{0,30}\b(any|new|latest|recent|from|left|waiting)|"
    rf"({rtm_name_rx(' ?')}|rtm)\b.{{0,30}}\b(voice ?mails?|voicemails?|missed calls?)|"
    # household coordination (protocol v3): bus, staged voicemail, peer presence, catch-up
    r"(check|read|catch ?up( on)?|reconcile) (the )?(bus|coordination)|"
    r"(check|read|peek|drain|reconcile|catch ?up)\b.{0,40}\b(protocol.?v3|v3|claude[_ -]?bus|bus|coordination|staged|staging|pi|home assistant|\bha\b)\b.{0,40}\b(voice ?mails?|voicemails?)|"
    r"(protocol.?v3|v3|claude[_ -]?bus|bus|coordination|staged|staging|pi|home assistant|\bha\b)\b.{0,40}\b(voice ?mails?|voicemails?)|"
    r"are (chat|code|the other agents?|the peers?) (there|online|up|present)|"
    r"(coordination|protocol.?v3|the bus) (status|state)|"
    r"(test|check|verify) (protocol.?v3|v3) (readiness|handshake|transport|peers?)|"
    # RTM product-build provenance/source of truth: where the spec lives, whether chat or UI is canonical
    rf"(rtm|{rtm_name_rx(' ?')}).{{0,60}}(source of truth|canonical|build spec|main spec|provenance)|"
    rf"(where'?s|where is|what is).{{0,60}}(rtm|{rtm_name_rx(' ?')}).{{0,60}}(spec|source of truth|canonical)|"
    # integration journeys, not just current health
    r"(integration|connector).{0,40}(journey|lifecycle|enable|disable|setup flow|set.?up flow)|"
    # privacy and context continuity audits: these need evidence from state, not vibes
    r"(guest|guests).{0,50}(privacy|boundary|boundaries|isolation|state|private|leak|access)|"
    rf"(can|could).{{0,30}}({owner_name_rx()}|owner).{{0,30}}(read|see|access).{{0,30}}(guest|guests)|"
    r"(conversation|chat|context|memory).{0,60}(continuity|health|persist|persistence|survive|load.?bearing|reload|restart)|"
    r"(check|audit|verify).{0,40}(conversation|chat).{0,40}(context|continuity|history)|"
    # home state reads: questions ABOUT the home (not commands)
    r"(is|are)\s+(the\s+|my\s+)?(garage|door|gate|window|blinds?|curtains?|shutters?)\s+(open|closed|locked|unlocked)|"
    r"what'?s\s+(on|playing|happening)\s+(at\s+home|upstairs|downstairs)?|"
    r"system status|check status)\b"),
    re.IGNORECASE,
)

# Natural file-finding phrases that should route to the unified search/find tool,
# even without the literal "search my files". The audit (#10) flagged that
# "find the tribunal bundle" or "where's my insurance certificate" fell through to
# chat. We require a find/locate verb PLUS either a possessive ("my/the/that") or a
# document-ish noun, so "find peace" / "where is France" do NOT match.
_FIND_VERB = r"(find|locate|look for|pull up|dig up|where('?s| is| are| did i (put|save))|do i have|did i (get|receive|save))"
_DOC_NOUN = (r"(file|files|document|documents|doc|docs|attachment|attachments|"
             r"email|emails|e-?mail|message|messages|pdf|pdfs|spreadsheet|spreadsheets|"
             r"invoice|invoices|receipt|receipts|certificate|certificates|"
             r"statement|statements|contract|contracts|letter|letters|report|reports|"
             r"bundle|scan|scans|photo|photos|picture|pictures|note|notes|backup|export)")
_FILE_FIND = re.compile(
    rf"\b{_FIND_VERB}\b.{{0,40}}?\b(my|the|that|a|an|our)\b.{{0,30}}?\b{_DOC_NOUN}\b"
    rf"|\b{_FIND_VERB}\b.{{0,20}}?\b{_DOC_NOUN}\b"
    rf"|\bsearch (for|through|across)\b.{{0,30}}?\b{_DOC_NOUN}\b",
    re.IGNORECASE | re.DOTALL,
)

# Social / ritual intent. Routes the new social layer through the normal tool loop
# so "what changed?", "physio check", "journal me", and "draft a check-in to Sam"
# are tool-routed in chat/voice, not just on the CLI. Ritual triggers are specific
# enough to match directly; WhatsApp intents need a draft/ask verb plus whatsapp/a
# message/a contact cue.
_SOCIAL_INTENT = re.compile(
    r"\bwhat'?s? changed\b|\bwhat has changed\b|\banything (new|changed)\b"
    r"|\bphysio check\b|\bproprioception\b|\bbreathing (minute|exercise)\b"
    r"|\bjournal me\b|\bresilience check\b|\bcheck in with\b|\bwho should i (check in|message)\b"
    r"|\bdraft (a |an )?(soft |quick |small )?(check.?in|message|reply|text|whatsapp)\b"
    r"|\b(what did|what's|whats) .{0,20}? (say|said|message|texted)\b"
    r"|\bwho (have i|haven'?t i) (not )?(replied|responded|messaged)\b",
    re.IGNORECASE,
)

# Research / investigation intent. Routes to the tool loop so the planner can call
# the read-only `research` tool (which retrieves local sources and returns grounded
# notes the owner phrases). Phrasings like "research X", "look up X", "compare X",
# "what's the latest on X", "investigate X", "evidence for X".
_RESEARCH_INTENT = re.compile(
    r"\bresearch\b|\blook up\b|\blook into\b|\binvestigate\b"
    r"|\bcompare\b.{0,40}?\b(vs|versus|or|and|against|to)\b"
    r"|\bcompare (sources|the|these|options)\b|\bevidence (for|of|that)\b"
    r"|\bwhat'?s? the latest\b|\blatest on\b|\bfind (current|the latest|recent)\b"
    r"|\bdig into\b|\bgather (evidence|sources|info)\b",
    re.IGNORECASE,
)


def _looks_like_code(text: str) -> bool:
    if _CODE_FENCE.search(text):
        return True
    if _CODE_STRONG.search(text):
        return True
    # Soft code words only count when paired with a coding verb, so
    # "write a function" is code but "function of the spleen" is not.
    if _CODE_SOFT.search(text) and _CODE_VERB.search(text):
        return True
    # Require real code structure: an operator/statement form, or a function
    # definition/call shape. A bare parenthetical like "(music)" no longer
    # counts, so prose with brackets is not misrouted to the code model.
    if _CODE_SYNTAX.search(text):
        return True
    if _CODE_CALL.search(text):
        return True
    return False


def classify_role(text: str) -> str:
    """Map a user prompt to a logical model role: 'code', 'tool', or 'chat'.

    Order matters: code is checked first because a coding request can also
    contain words like "schedule" or "search"; the presence of code structure
    is the stronger signal. Tool/home intent is checked next. Everything else
    is chat.
    """
    return classify_role_detailed(text).role


@dataclass
class RoleDecision:
    """A routing decision with its confidence and the rules that fired, so routing
    is explainable and ambiguous cases can be handled deliberately rather than
    silently acting. `needs_confirmation` is True when the heuristic matched weakly:
    the caller may confirm with the user or fall back to chat (no tool side effects)
    rather than guess."""
    role: str               # 'chat' | 'tool' | 'code'
    confidence: float       # 0.0 - 1.0
    matched_rules: list[str]
    needs_confirmation: bool = False


# Soft/ambiguous action phrases: these LEAN tool but aren't decisive on their own
# ("sort this out", "deal with it", "handle that"). We surface them as low-confidence
# tool with needs_confirmation, never a silent action.
_SOFT_ACTION = re.compile(
    r"\b(sort (this|that|it) out|deal with (this|that|it)|handle (this|that|it)|"
    r"take care of (this|that|it)|do something about)\b", re.IGNORECASE)


# RTM bank-feed / provider-playbook intent. This routes questions about Metro/Xero/Crunch/Plaid/etc
# to the grounded local playbook instead of the persona model guessing from memory.
RTM_BANK_FEED_INTENT = _ProfileRegex(lambda: (
    rf"\b(rtm|{rtm_name_rx()}|metro)\b.{{0,60}}\b(bank ?feed|open banking|aisp|ais|xero|crunch|plaid|pandle|gocardless|moneyhub|yapily|clear ?books|truelayer|finverse)\b"
    r"|\b(bank ?feed|open banking|aisp|ais)\b.{0,60}\b(xero|crunch|plaid|pandle|gocardless|moneyhub|yapily|clear ?books|truelayer|finverse|metro)\b"
    r"|\b(xero|crunch|plaid|pandle|gocardless|moneyhub|yapily|clear ?books|truelayer|finverse)\b.{0,80}\b(service charge|reserve|metro|rtm|trust account|community current|feed|provider|playbook)\b"),
    re.IGNORECASE,
)


def looks_like_rtm_bank_feed(text: str) -> bool:
    return bool(RTM_BANK_FEED_INTENT.search(text or ""))


# Comms lookup intent: the owner wants to hear/read/find a VOICEMAIL or check who called. This must
# win over the RTM voice short-circuits, because RTM_OPERATIONAL_INTENT matches bare words like
# "leaseholder" or the RTM's name, which would otherwise hijack "read me the voicemail from the
# leaseholders" into the RTM ledger view instead of searching voicemails. Kept tight: the mere
# presence of "voicemail", or an explicit who-called / missed-calls phrasing, or "play the recording".
# It deliberately does NOT match a command to place a call ("call Alex") or an RTM money question.
_COMMS_LOOKUP_INTENT = re.compile(
    r"\bvoice[\s-]?mails?\b"
    r"|\bwho\s+(?:called|rang|has\s+called|'?s\s+called)\b"
    r"|\b(?:missed|recent|any|new|unheard|latest|old)\s+calls?\b"
    r"|\b(?:play|listen\s+to|hear)\b[^.?!]{0,30}?\brecordings?\b",
    re.IGNORECASE | re.DOTALL,
)


def looks_like_comms_lookup(text: str) -> bool:
    """True if the user wants to retrieve a voicemail or call record (not place a call). Used to route
    to the voicemail/search tool and to stop the RTM voice short-circuits from swallowing the query."""
    return bool(_COMMS_LOOKUP_INTENT.search(text or ""))


def classify_role_detailed(text: str) -> RoleDecision:
    """Heuristic classification with confidence and matched rules. The heuristic is
    the first gate (cheap, inspectable, deterministic); a caller can escalate only
    the ambiguous (needs_confirmation) cases to the resident model if it wants, but
    that is the caller's choice, the classifier itself never calls a model."""
    if not text or not text.strip():
        return RoleDecision("chat", 0.99, ["empty"])

    matched: list[str] = []
    if _looks_like_code(text):
        return RoleDecision("code", 0.95, ["code_structure"])

    home = bool(HOME_INTENT.search(text))
    tool_kw = bool(_TOOL_KEYWORDS.search(text))
    file_find = bool(_FILE_FIND.search(text))
    social = bool(_SOCIAL_INTENT.search(text))
    research = bool(_RESEARCH_INTENT.search(text))
    rtm_bank_feed = bool(RTM_BANK_FEED_INTENT.search(text))
    comms = bool(_COMMS_LOOKUP_INTENT.search(text))
    if rtm_bank_feed:
        matched.append("rtm_bank_feed_intent")
    if comms:
        matched.append("comms_lookup")
    if home:
        matched.append("home_intent")
    if tool_kw:
        matched.append("tool_keyword")
    if file_find:
        matched.append("file_find")
    if social:
        matched.append("social_intent")
    if research:
        matched.append("research_intent")

    if home or tool_kw or rtm_bank_feed or comms:
        # Explicit tool/home/bank-feed/voicemail phrasing: high confidence.
        return RoleDecision("tool", 0.95, matched)
    if file_find or social or research:
        # Natural file-finding / social / research intent: confident but a touch
        # below explicit commands. Routes to the tool loop so the planner can call
        # the relevant read tool (find / social / research).
        return RoleDecision("tool", 0.8, matched)
    if _SOFT_ACTION.search(text):
        # Leans tool but genuinely ambiguous: flag for confirmation, don't act.
        return RoleDecision("tool", 0.45, ["soft_action"], needs_confirmation=True)

    return RoleDecision("chat", 0.9, ["default_chat"])
