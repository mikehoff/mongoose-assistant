"""Single source of truth for Mongoose's USER-FACING capabilities: the warm "what can I do with
you" areas. Every surface that lists, routes, or promises a capability should read from HERE, so
the human showcase, the chat routing intents, and the availability hints can never drift apart.

Before this module there were three hand-maintained copies of "what Mongoose can do":
  1. the showcase text hard-coded in app._maybe_capabilities_answer,
  2. the routing anchors in services/semantic_router,
  3. the guide workflows in services/capability_guide,
plus the live-but-orphaned capability_index (built from the tool/route/CLI registries but wired to
nothing in chat). They used different words for the same thing and the showcase cheerfully promised
features that were not connected ("email triage" with no mail account). This reconciles them: one
declarative list, an honest availability check, and shared example utterances.

Each entry:
  key       stable id
  area      heading shown to the user in the showcase
  blurb     warm one-line description (kept human, not a status line)
  needs     None if always-on, else an availability token resolved by available()
  intent    the semantic_router intent that routes this capability in chat, or None
  examples  utterances that should route/match here (shared into the semantic_router anchors)

Nothing here executes anything; it is description + a read-only availability probe.
"""
from __future__ import annotations

from typing import Any

# The catalog as written: copy may carry site-profile placeholders ("{{rtm.name}}"). Read it as
# CAPABILITIES (module attribute, below) or _capabilities(), which fill them in.
_CAPABILITIES: list[dict[str, Any]] = [
    {
        "key": "home", "area": "Your house",
        "blurb": "turn the lounge lights down, set timers, reminders and alarms",
        "needs": "home", "intent": "home_control",
        # examples must route to the declared intent (home_control). Timers/alarms are a separate
        # capability handled by the time_alerts detector, and "are the lights on" is a READ, so
        # neither belongs here even though the blurb mentions them.
        "examples": ["turn the lounge lights down", "dim the bedroom lights",
                     "switch off the kitchen light"],
    },
    {
        "key": "day", "area": "Your day",
        "blurb": "a morning brief, email triage, a watch on your calendar, and draft replies you approve",
        "needs": "mail", "intent": None,
        "examples": ["what's my morning brief", "triage my inbox", "any important emails"],
    },
    {
        "key": "rtm", "area": "{{rtm.name}} (RTM)",
        "blurb": "service charges, arrears, demands and deadlines. I keep the ledger and the paperwork moving",
        "needs": None, "intent": "rtm_operational",
        "examples": ["how are the arrears looking", "who owes service charge",
                     "what's overdue at {{RTM_NAME_LOWER}}", "how much is owed on the flats"],
    },
    {
        "key": "lounge", "area": "The lounge",
        "blurb": "a shared room for you and your guests where you can poke each other and I chime in when asked",
        "needs": None, "intent": None, "examples": [],
    },
    {
        "key": "music", "area": "Music",
        "blurb": "I'm a bit obsessed. Tell me what you love and I'll remember it and lob you the odd recommendation",
        "needs": None, "intent": None,
        "examples": ["I can't stop playing this song", "recommend me something to listen to"],
    },
    {
        "key": "talk", "area": "Just talk",
        "blurb": "remember this, forget that, read my messages, or say \"raise a bug\" and it lands in your Approvals",
        "needs": None, "intent": None,
        "examples": ["remember that the boiler code is 1234", "raise a bug", "read my messages"],
    },
    {
        "key": "voice", "area": "Voice",
        "blurb": "hold the mic and talk to me; tap the speaker on a reply to hear me back",
        "needs": "voice", "intent": None, "examples": [],
    },
]

# What a missing connector should say , inviting, not a dry status line. Only shown when `needs`
# is genuinely not available, so the showcase never claims a feature that cannot work yet.
_UNMET_HINT = {
    "home": "once you connect Home Assistant",
    "mail": "once you connect your email",
    "calendar": "once you connect a calendar",
    "voice": "once voice input is set up",
}


def _integration_configured() -> dict[str, bool]:
    """name -> configured, from the setup wizard's connector view. Never raises."""
    try:
        from ..setup_wizard import integration_status
        return {(s.get("name") or ""): bool(s.get("configured")) for s in integration_status()}
    except Exception:
        return {}


def _capability_ready() -> dict[str, bool]:
    """install-capability key -> ready, from capabilities.detect_all(). Never raises."""
    try:
        from . import capabilities as caps
        return {(c.get("capability") or ""): bool(c.get("ready")) for c in caps.detect_all()}
    except Exception:
        return {}


def available(need: str | None) -> bool:
    """Truthful availability for a capability's `needs` token. Always-on capabilities (need=None)
    are always True. Connector-gated ones consult the live wizard/capability state. Conservative:
    on any error it returns True so the showcase still offers the feature rather than hiding it."""
    if not need:
        return True
    try:
        conf = _integration_configured()
        ready = _capability_ready()
        if need == "home":
            return conf.get("Home Assistant", False)
        if need == "mail":
            return (ready.get("google", False) or ready.get("microsoft", False)
                    or conf.get("Email (IMAP, read-only)", False)
                    or conf.get("Google Calendar / Tasks", False))
        if need == "calendar":
            return (ready.get("google", False) or ready.get("microsoft", False)
                    or conf.get("Google Calendar / Tasks", False)
                    or conf.get("Microsoft Calendar / To Do", False))
        if need == "voice":
            return ready.get("voice", False)
        return True
    except Exception:
        return True


def _render(obj: Any) -> Any:
    from ..settings import render_profile_copy
    if isinstance(obj, str):
        return render_profile_copy(obj)
    if isinstance(obj, list):
        return [_render(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _render(v) for k, v in obj.items()}
    return obj


def _capabilities() -> list[dict[str, Any]]:
    """The catalog with the site-profile placeholders filled in (the RTM's name, for this install)."""
    return _render(_CAPABILITIES)


def __getattr__(name: str) -> Any:
    # `capability_catalog.CAPABILITIES` (and `from ... import CAPABILITIES`) always yields the rendered
    # catalog, so no consumer can show a raw {{placeholder}}.
    if name == "CAPABILITIES":
        return _capabilities()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def examples_for(intent: str) -> list[str]:
    """Utterances declared for a routing intent, so the semantic router and the showcase share one
    phrasing source instead of drifting apart."""
    out: list[str] = []
    for c in _capabilities():
        if c.get("intent") == intent:
            out.extend(c.get("examples") or [])
    return out


def all_intents() -> set[str]:
    return {c["intent"] for c in _CAPABILITIES if c.get("intent")}


def showcase_lines() -> list[str]:
    """The warm capability bullets, rendered from the catalog with a truthful availability nudge
    appended only where a needed connector is missing. Kept human: it stays a delight surface, not
    a readiness report."""
    lines: list[str] = []
    for c in _capabilities():
        blurb = c["blurb"]
        if not available(c.get("needs")):
            hint = _UNMET_HINT.get(c.get("needs") or "", "")
            if hint:
                blurb = f"{blurb} ({hint})"
        lines.append(f"• {c['area']}: {blurb}.")
    return lines


def owner_showcase() -> str:
    """The full warm "what can I do with you" answer, rendered from the catalog. Shared by the phone
    device path (app._maybe_capabilities_answer) and the IRC/voice chat path (llm.chat_with_memory)
    so the two never diverge."""
    body = "\n".join(showcase_lines())
    return ("Oh, loads. Here's the fun bit 🦫\n\n"
            f"{body}\n\n"
            "Want to start somewhere? Try \"remind me to call the plumber at 5\", or just tell me a "
            "song you can't stop playing.")
