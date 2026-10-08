from __future__ import annotations

from typing import Any

# The first-run tutorial: Mongoose introducing itself and what it can do.
#
# Three design commitments (from how the user actually wants this):
#  1. Voice + phone first: a short SPOKEN self-introduction that points to tappable phone
#     examples, not a wall of text.
#  2. Grounded, not scripted: it teaches what's ACTUALLY live (via the readiness layer) and
#     TEASES what unlocks when you connect things , so it never teaches Gmail commands when
#     Gmail isn't set up.
#  3. "Do something cool" must actually be cool: on a fresh install the genuinely impressive
#     capabilities (cross-domain brief) have no data. So it does the coolest thing it CAN with
#     real data, and falls back to a clearly-LABELLED worked sample , impressive without lying.
#
# Honesty rule: when a demo uses sample data, it says so. You're meant to be impressed by a real
# capability shown on an example, never fooled into thinking you have data you don't.


# What Mongoose can do, organised so the tutorial can show "live now" vs "unlocks when connected".
# Each entry: id, the example phrase, which readiness domain/status makes it LIVE, and a one-line
# description. Capabilities with requires=None work on a bare install (no setup).
_CAPABILITIES = [
    # always available on a fresh install , no connection needed
    {"id": "ask", "say": "What's the capital of Japan?",
     "desc": "Ask me anything, I run entirely on this Mac, nothing leaves the house.",
     "requires": None, "category": "live"},
    {"id": "alarm", "say": "Wake me at 7 tomorrow.",
     "desc": "Set alarms, timers and reminders. I'll reach you on your phone.",
     "requires": None, "category": "live"},
    {"id": "readiness", "say": "Are you ready?",
     "desc": "Ask what's working, what's stale, and what needs you, I answer from real state.",
     "requires": None, "category": "live"},
    {"id": "home", "say": "Turn off the kitchen lights.",
     "desc": "Control your lights, plugs and more through Home Assistant.",
     "requires": ("home", "configured"), "category": "live_or_teaser"},
    # unlock when a source is connected
    {"id": "brief", "say": "Give me my brief.",
     "desc": "A cross-domain morning brief that ties your calendar, mail and weather together.",
     "requires": ("mail", "has_data"), "category": "teaser"},
    {"id": "search", "say": "Find the invoice from last month.",
     "desc": "Search your own email and files, locally.",
     "requires": ("mail", "has_data"), "category": "teaser"},
    {"id": "whatsapp", "say": "Any WhatsApp I should reply to?",
     "desc": "Surface messages that need a reply (once WhatsApp is linked).",
     "requires": ("messaging", "has_data"), "category": "teaser"},
]


def _live_status_by_domain() -> dict[str, str]:
    """Map each readiness domain to its 'best' status, so we can tell live from teaser."""
    from .readiness import cached_readiness
    rep = cached_readiness()
    rank = {"has_data": 5, "ready": 5, "authorised": 4, "configured": 3,
            "needs_authorisation": 2, "not_configured": 0}
    best: dict[str, str] = {}
    for it in rep.get("items", []):
        dom = it.get("domain", "")
        st = it.get("status", "")
        if rank.get(st, 0) >= rank.get(best.get(dom, ""), 0):
            best[dom] = st
    return best


def _is_live(cap: dict[str, Any], status_by_domain: dict[str, str]) -> bool:
    req = cap.get("requires")
    if req is None:
        return True
    dom, need = req
    have = status_by_domain.get(dom, "not_configured")
    rank = {"has_data": 5, "ready": 5, "authorised": 4, "configured": 3,
            "needs_authorisation": 2, "not_configured": 0}
    return rank.get(have, 0) >= rank.get(need, 0)


def tutorial_examples(*, include_private_state: bool = True) -> dict[str, Any]:
    """The adaptive example set: what's live now vs what unlocks.

    Public first-run endpoints must not reveal whether private sources (mail,
    WhatsApp, files) are already connected. When include_private_state=False we
    show only no-setup capabilities as live and treat data-backed capabilities as
    teasers. Authenticated Cockpit/device surfaces may pass True to teach from
    real readiness.
    """
    status = _live_status_by_domain() if include_private_state else {}
    live, teasers = [], []
    for cap in _CAPABILITIES:
        card = {"id": cap["id"], "try": cap["say"], "what": cap["desc"]}
        if _is_live(cap, status):
            live.append(card)
        elif cap["category"] in ("teaser", "live_or_teaser"):
            # frame teasers as "connect X to unlock"
            dom = cap["requires"][0] if cap.get("requires") else ""
            card["unlocks_with"] = {"mail": "email", "messaging": "WhatsApp",
                                    "home": "Home Assistant"}.get(dom, dom)
            teasers.append(card)
    return {"live": live, "teasers": teasers}


def intro_speech(*, include_private_state: bool = True) -> str:
    """The short spoken self-introduction. Names what Mongoose is, gives 2-3 things to try right
    now, and teases one thing that unlocks. Short, it points to the phone for the full list."""
    ex = tutorial_examples(include_private_state=include_private_state)
    live = ex["live"]
    lead = ("I'm Mongoose, your assistant. I run entirely on this Mac, so your data stays in the "
            "house. Here are a few things you can try right now.")
    tries = [f'"{c["try"]}"' for c in live[:3]]
    body = " You can say: " + _join(tries) + "." if tries else ""
    tease = ""
    if ex["teasers"]:
        t = ex["teasers"][0]
        tease = (f" And once you connect your {t.get('unlocks_with', 'accounts')}, I can do more, "
                 f"like {t['what'][0].lower()}{t['what'][1:]}")
    close = " Want to see something? Just say: do something cool."
    return lead + body + tease + close


def intro_for_phone(*, include_private_state: bool = True) -> dict[str, Any]:
    """Phone-shaped tutorial: a headline, tappable 'try this' cards (live), and a 'unlocks when
    you connect' section. The card walkthrough the spoken intro points to."""
    ex = tutorial_examples(include_private_state=include_private_state)
    return {
        "headline": "Meet Mongoose",
        "subhead": "Your assistant, running on your Mac. Tap an example to try it.",
        "try_now": ex["live"],
        "unlocks": ex["teasers"],
        "cool_prompt": "Mongoose, do something cool",
    }


# --- "do something cool" -----------------------------------------------------

async def do_something_cool(*, allow_private_data: bool = True) -> dict[str, Any]:
    """The party trick.

    Authenticated surfaces may use real cross-domain data. Public first-run
    endpoints MUST NOT: /tutorial is reachable before auth so it can only return
    the labelled sample. This prevents a LAN caller from learning private
    calendar/mail/weather context through the demo endpoint.
    """
    if not allow_private_data:
        return _cool_sample()
    # 1) best real option: a cross-domain link (the genuinely impressive thing)
    try:
        from .daily_picture import daily_operating_picture
        import asyncio as _a_bound
        # The party trick must NEVER hang. The daily picture probes connected services (HA,
        # weather, mail), and any one of them can stall on a dead address; unbounded, that
        # turned "do something cool" into an infinite spinner. Bounded: a slow probe simply
        # drops us to the partial/sample paths below, which are instant.
        dop = await _a_bound.wait_for(daily_operating_picture(), timeout=6.0)
        links = dop.get("cross_domain_links", [])
        if links:
            top = links[0]
            return {"speech": "Here's something I noticed by connecting the dots across your day: "
                    + top.get("text", "") + " That's the kind of thing I do, I watch how your "
                    "calendar, mail, messages and weather interact, so you don't have to.",
                    "is_sample": False, "kind": "cross_domain", "detail": top}
        # 2) a real brief if there's any data at all
        shape = dop.get("todays_shape", {})
        if shape.get("events_ahead") or shape.get("weather"):
            bits = []
            if shape.get("weather"):
                bits.append(shape["weather"])
            evs = shape.get("events_ahead") or []
            if evs:
                bits.append(f"you've got {len(evs)} thing(s) coming up")
            return {"speech": "Here's your day at a glance: " + "; ".join(bits) + ". I pull this "
                    "together from everything connected, every morning, automatically.",
                    "is_sample": False, "kind": "brief", "detail": shape}
    except Exception:
        pass
    # 2.5) PARTLY configured but not enough indexed/synced data yet for a real nudge. This is the
    # common real case the user hit: IMAP + Home Assistant are configured (HA even tested green), but
    # no mail has synced and no calendar is connected, so there are no cross-domain links YET. Saying
    # "Nothing's connected yet" there is just wrong. Name what's connected and what's needed.
    try:
        connected = _connected_domains()
        if connected:
            names = ", ".join(connected)
            # Don't just explain the gap , DO something about it. The reason there's no cross-domain
            # nudge is usually "configured but not synced yet", so actually kick off the sync now, and
            # tell the user what to do to make it real. Best-effort, never blocks the response.
            actions = []
            imap_connected = any("IMAP" in n or "Email" in n for n in connected)
            if imap_connected:
                actions.append({"label": "Sync mail now", "endpoint": "/cockpit/mail/sync", "method": "POST"})
                try:
                    # Fire-and-forget a sync so data starts arriving immediately. A DAEMON
                    # thread, deliberately: asyncio.to_thread rides the default executor,
                    # which asyncio.run() JOINS at loop close, so a one-shot CLI/tutorial
                    # call would sit through a full IMAP connect timeout (20s on a dead
                    # host) before exiting. The response never reads this result: results
                    # land in the DB for next time, so nothing may ever wait on it.
                    from .crawler import crawl_imap_fresh
                    import threading as _th
                    _th.Thread(target=crawl_imap_fresh, args=("INBOX",), daemon=True).start()
                except Exception:
                    pass
            if any("Home Assistant" in n for n in connected):
                actions.append({"label": "Open Home Assistant controls", "tab": "system"})
            # suggest connecting a calendar if none is present (calendar+mail is the marquee combo)
            if not any("Calendar" in n or "Google" in n or "Microsoft" in n for n in connected):
                actions.append({"label": "Connect a calendar", "tab": "setup"})
            return {
                "speech": (f"You've connected {names}. The reason I can't yet make a cross-domain "
                           "nudge is just that the data hasn't synced, so I've started a sync now. "
                           + ("Once your mail finishes syncing" if imap_connected else "Once data arrives")
                           + (", and once a calendar is connected, " if not any("Calendar" in n or "Google" in n or "Microsoft" in n for n in connected) else ", ")
                           + "I'll link them into one useful prompt, like: " + _cool_sample()["detail"].get("note", "")),
                "is_sample": True, "kind": "partial",
                "actions": actions,
                "detail": {"connected": connected, "sync_started": imap_connected,
                           "note": "sources connected; sync kicked off, calendar+mail is the marquee combo"},
            }
    except Exception:
        pass
    # 3) nothing connected yet , a LABELLED worked sample (impressive, honest)
    return _cool_sample()


def _connected_domains() -> list[str]:
    """Human names of integrations that are actually configured (so 'do something cool' can tell the
    difference between a bare install and 'configured but not data-rich yet'). Best-effort, and it
    must NOT run network verifiers (this is on the interactive path), so it judges config/token
    presence directly rather than calling integration_status (which verifies)."""
    out: list[str] = []
    try:
        from ..setup_wizard import INTEGRATIONS, effective_env  # type: ignore
    except Exception:
        return out
    try:
        env = effective_env()
    except Exception:
        return out
    for integ in INTEGRATIONS:
        try:
            auth = integ.auth_state(env) if getattr(integ, "auth_state", None) else None
            if auth is not None:
                if auth == "signed_in":
                    out.append(integ.name)
            elif getattr(integ, "configured_when", None) is not None:
                if integ.configured_when(env):
                    out.append(integ.name)
            else:
                required = [f for f in integ.fields if not f.optional]
                ok = all(env.get(f.key) for f in required) if required else any(env.get(f.key) for f in integ.fields)
                if ok:
                    out.append(integ.name)
        except Exception:
            continue
    return [n for n in out if n]


def _cool_sample() -> dict[str, Any]:
    """A worked example of the marquee capability, explicitly flagged as a sample so it's never
    mistaken for real data. Shows WHAT Mongoose will do once things are connected."""
    sample = ("Say it's 8am on a Tuesday. I see a 10am dentist appointment in your calendar, an "
              "email from the surgery an hour ago confirming it, and rain forecast at 9:30. So "
              "instead of three separate notifications, I tell you once: \"Leave by 9:15 for the "
              "dentist, there's rain, and the surgery emailed to confirm.\"")
    return {
        "speech": ("Nothing's connected yet, so let me show you the kind of thing I'll do, with a "
                   "made-up example: " + sample + " That's me connecting your calendar, mail and "
                   "weather into one useful nudge. Connect an account and this becomes real."),
        "is_sample": True, "kind": "sample",
        "detail": {"note": "illustrative example, not your real data"},
    }


# --- one-time trigger --------------------------------------------------------

_K_INTRO_SEEN = "tutorial_intro_seen"


def intro_seen() -> bool:
    try:
        from ..db import get_app_state
        return get_app_state(_K_INTRO_SEEN, "false") == "true"
    except Exception:
        return False


def mark_intro_seen() -> None:
    try:
        from ..db import set_app_state
        set_app_state(_K_INTRO_SEEN, "true")
    except Exception:
        pass


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"
