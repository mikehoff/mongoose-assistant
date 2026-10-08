"""Read live Home Assistant state, what's on, what's open, what a thing is doing right now.

This is the READ counterpart to home_router's control path. It only ever GETs from HA (/api/states),
never calls a service, so it is strictly read-only and safe for the brain to use on any plain question
("are the lounge lights on?", "what's on upstairs?", "is the heating running?").

Design:
  - One cheap GET of /api/states, summarised. We group by domain and surface the human-relevant fields
    (on/off, brightness, temperature, open/closed, playing) rather than dumping raw JSON.
  - A free-text filter narrows to what the person asked about (room word, device word, domain) using the
    same token-overlap idea as the resolver, but here it only SELECTS what to report, a wrong guess
    shows extra/fewer rows, it never actuates anything, so the safety bar is far lower than for control.
  - Panel-safety entities are still listed (reading their state is fine) but flagged, so a summary never
    pretends they don't exist while making clear they're protected hardware.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..settings import get_settings

# Domains worth summarising for "what's going on at home". Ordered for a sensible readout.
_READABLE_DOMAINS = ("light", "switch", "climate", "cover", "media_player", "fan", "lock",
                     "binary_sensor", "sensor", "person", "alarm_control_panel")

# The handful of attributes that actually matter in a spoken/typed summary, per domain.
_INTERESTING_ATTRS = {
    "light": ("brightness", "color_temp", "rgb_color"),
    "climate": ("current_temperature", "temperature", "hvac_action"),
    "cover": ("current_position",),
    "media_player": ("media_title", "volume_level"),
    "sensor": ("unit_of_measurement",),
    "lock": (),
}


def _panel_ids() -> frozenset[str]:
    try:
        from .protocol_v3 import PANEL_SAFETY_ENTITIES
        return PANEL_SAFETY_ENTITIES
    except Exception:
        return frozenset()


def _tokens(text: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in str(text)).split() if t}


def _matches(query_tokens: set[str], eid: str, friendly: str, domain: str) -> bool:
    """True if this entity is relevant to the query. Empty query => everything readable."""
    if not query_tokens:
        return True
    hay = _tokens(eid.replace(".", " ")) | _tokens(friendly) | {domain}
    # also let a domain word ("lights", "heating") match its domain
    domain_words = {"light": {"light", "lights", "lamp", "lamps"},
                    "climate": {"heating", "heat", "thermostat", "temperature", "ac", "aircon"},
                    "cover": {"blind", "blinds", "curtain", "curtains", "shutter", "garage"},
                    "lock": {"lock", "locks", "door"},
                    "media_player": {"tv", "music", "speaker", "media"},
                    "fan": {"fan", "fans"},
                    "switch": {"switch", "switches", "plug", "plugs"}}.get(domain, set())
    return bool(query_tokens & hay) or bool(query_tokens & domain_words)


def _summarise_entity(state: dict[str, Any], panels: frozenset[str]) -> dict[str, Any]:
    eid = state.get("entity_id", "")
    domain = eid.split(".", 1)[0]
    attrs = state.get("attributes") or {}
    out: dict[str, Any] = {
        "entity_id": eid,
        "name": attrs.get("friendly_name", eid),
        "state": state.get("state", "unknown"),
        "domain": domain,
    }
    interesting = {}
    for a in _INTERESTING_ATTRS.get(domain, ()):
        if a in attrs and attrs[a] not in (None, ""):
            interesting[a] = attrs[a]
    if interesting:
        out["detail"] = interesting
    if eid in panels:
        out["panel_protected"] = True
    return out


def read_home_state(query: str = "", *, max_entities: int = 60) -> dict[str, Any]:
    """Read-only snapshot of live Home Assistant state, optionally filtered to what the person asked
    about. Never calls a service; only GETs /api/states. Returns a grouped, summarised view plus a few
    quick rollups (how many lights are on, etc). Never raises, returns a friendly not-configured /
    unreachable result instead."""
    settings = get_settings()
    if not settings.ha_url or not settings.ha_token:
        return {"ok": False, "reason": "not_configured",
                "say": "Home Assistant isn't connected, so I can't see the home's state."}
    url = f"{settings.ha_url.rstrip('/')}/api/states"
    headers = {"Authorization": f"Bearer {settings.ha_token}"}
    try:
        with httpx.Client(timeout=settings.ha_timeout_seconds) as client:
            resp = client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"unreachable:{type(exc).__name__}",
                "say": "Couldn't reach Home Assistant to read the home's state."}
    if resp.status_code != 200:
        return {"ok": False, "reason": f"http_{resp.status_code}",
                "say": f"Home Assistant returned {resp.status_code} when I asked for the state."}

    panels = _panel_ids()
    qtokens = _tokens(query)
    by_domain: dict[str, list[dict[str, Any]]] = {}
    lights_on = switches_on = 0
    total_seen = 0
    for state in resp.json():
        eid = state.get("entity_id", "")
        domain = eid.split(".", 1)[0]
        if domain not in _READABLE_DOMAINS:
            continue
        friendly = (state.get("attributes") or {}).get("friendly_name", "")
        if not _matches(qtokens, eid, friendly, domain):
            continue
        total_seen += 1
        summ = _summarise_entity(state, panels)
        by_domain.setdefault(domain, []).append(summ)
        if domain == "light" and summ["state"] == "on":
            lights_on += 1
        if domain == "switch" and summ["state"] == "on":
            switches_on += 1

    # cap the payload so a huge home doesn't flood the model
    capped = {}
    shown = 0
    for dom in _READABLE_DOMAINS:
        if dom not in by_domain:
            continue
        rows = by_domain[dom]
        room = rows[: max(0, max_entities - shown)]
        if room:
            capped[dom] = room
            shown += len(room)

    rollup = {"lights_on": lights_on, "switches_on": switches_on,
              "entities_reported": shown, "entities_matched": total_seen}
    say = _phrase_rollup(query, capped, rollup)
    return {"ok": True, "query": query, "by_domain": capped, "rollup": rollup,
            "truncated": total_seen > shown, "say": say,
            "note": "Read-only snapshot from Home Assistant. Panel hardware is shown but flagged."}


def _phrase_rollup(query: str, by_domain: dict[str, list[dict[str, Any]]],
                   rollup: dict[str, int]) -> str:
    """A short human sentence so voice/text has something to say without re-deriving it from the rows."""
    if rollup["entities_reported"] == 0:
        return "I couldn't find anything matching that at home." if query else "Nothing to report."
    parts = []
    lights = by_domain.get("light", [])
    if lights:
        on = [l for l in lights if l["state"] == "on"]
        if on:
            names = ", ".join(l["name"] for l in on[:6])
            parts.append(f"{len(on)} light{'s' if len(on) != 1 else ''} on ({names}{'…' if len(on) > 6 else ''})")
        else:
            parts.append("all lights off")
    climate = by_domain.get("climate", [])
    for c in climate[:3]:
        t = (c.get("detail") or {}).get("current_temperature")
        if t is not None:
            parts.append(f"{c['name']} at {t}°")
    covers = [c for c in by_domain.get("cover", []) if c["state"] == "open"]
    if covers:
        parts.append(f"{len(covers)} open ({', '.join(c['name'] for c in covers[:4])})")
    media = [m for m in by_domain.get("media_player", []) if m["state"] == "playing"]
    if media:
        parts.append(f"{len(media)} playing")
    if not parts:
        return f"Reporting {rollup['entities_reported']} device(s)."
    return "; ".join(parts) + "."
