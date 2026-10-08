from __future__ import annotations

import time
from typing import Any

import httpx

from ..settings import get_settings


class HAUnreachable(RuntimeError):
    """Raised when Home Assistant is configured but cannot be reached."""

# Resolves human descriptions ("study desk lamp") to real Home Assistant entity
# ids ("switch.study_cat_neon_socket_1"). The tool model produces room/device
# words, not entity ids; without this, control commands either fail or silently
# do nothing. We fetch the entity list from HA (cached briefly) and fuzzy-match.

_CACHE: dict[str, Any] = {"entities": None, "fetched_at": 0.0}
_CACHE_TTL_SECONDS = 60.0

# Domains we can actually control; also the prefixes we offer for matching.
_CONTROLLABLE_DOMAINS = ("light", "switch", "media_player", "cover", "climate", "scene", "fan")

# Default service per domain for a turn_on / turn_off style action.
_SERVICE_FOR_ACTION = {
    "light": {"on": ("light", "turn_on"), "off": ("light", "turn_off")},
    "switch": {"on": ("switch", "turn_on"), "off": ("switch", "turn_off")},
    "fan": {"on": ("fan", "turn_on"), "off": ("fan", "turn_off")},
    "media_player": {"on": ("media_player", "turn_on"), "off": ("media_player", "turn_off")},
    "cover": {"on": ("cover", "open_cover"), "off": ("cover", "close_cover")},
    "scene": {"on": ("scene", "turn_on"), "off": ("scene", "turn_on")},
}


def _verb_service(domain: str, action: str | None) -> tuple[str, str] | None:
    """Map a natural-language action to a concrete (domain, service), handling
    verbs beyond on/off (close, open, pause, stop, play) so they don't all
    degrade to 'turn_on'. Falls back to on/off for unrecognised verbs."""
    a = (action or "").lower()
    if domain == "cover":
        if any(w in a for w in ("close", "shut", "down", "lower")):
            return ("cover", "close_cover")
        if any(w in a for w in ("open", "up", "raise")):
            return ("cover", "open_cover")
    if domain == "media_player":
        if "pause" in a:
            return ("media_player", "media_pause")
        if "stop" in a:
            return ("media_player", "media_stop")
        if "play" in a or "resume" in a:
            return ("media_player", "media_play")
    # Default on/off mapping.
    act = "off" if a.find("off") >= 0 else "on"
    return _SERVICE_FOR_ACTION.get(domain, {}).get(act)


async def _fetch_entities() -> list[dict[str, Any]]:
    """Return [{entity_id, friendly_name}] for controllable entities, cached.

    Raises HAUnreachable if HA is configured but the call fails (timeout, DNS,
    non-200), so callers can distinguish "can't reach HA" from "no match". A
    cached list is returned if still fresh even on a later failure.
    """
    now = time.time()
    if _CACHE["entities"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL_SECONDS:
        return _CACHE["entities"]
    settings = get_settings()
    if not settings.ha_url or not settings.ha_token:
        return []
    url = f"{settings.ha_url.rstrip('/')}/api/states"
    headers = {"Authorization": f"Bearer {settings.ha_token}"}
    try:
        async with httpx.AsyncClient(timeout=settings.ha_timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        if _CACHE["entities"] is not None:
            return _CACHE["entities"]
        raise HAUnreachable(f"could not reach Home Assistant at {url}: {exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        if _CACHE["entities"] is not None:
            return _CACHE["entities"]
        raise HAUnreachable(f"Home Assistant returned {resp.status_code} for {url}")
    data = resp.json()
    entities = []
    for state in data:
        eid = state.get("entity_id", "")
        if eid.split(".", 1)[0] in _CONTROLLABLE_DOMAINS:
            entities.append({
                "entity_id": eid,
                "friendly_name": (state.get("attributes") or {}).get("friendly_name", ""),
            })
    _CACHE["entities"] = entities
    _CACHE["fetched_at"] = now
    return entities


def _tokens(text: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t}


def _score(query_tokens: set[str], entity: dict[str, Any]) -> float:
    """Token-overlap score between the query and an entity's id + friendly name."""
    hay = _tokens(entity["entity_id"].replace(".", " ")) | _tokens(entity["friendly_name"])
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens & hay)
    # Fraction of the query's words that are present, with a small bonus for
    # matching more words overall (favours the most specific entity).
    return overlap / len(query_tokens) + 0.01 * overlap


def _prefilter(query_tokens: set[str], entities: list[dict[str, Any]], top: int = 8) -> list[dict[str, Any]]:
    """Cheap token-overlap prefilter: narrow potentially hundreds of entities to
    a handful of plausible candidates for the model to choose between. Partial
    matches are kept here (unlike the strict resolver) because the model makes
    the final call."""
    scored = sorted(entities, key=lambda e: _score(query_tokens, e), reverse=True)
    # Keep anything sharing at least one token, capped at `top`.
    return [e for e in scored if _score(query_tokens, e) > 0][:top]


async def resolve_entity_with_model(room: str | None, device_id: str | None,
                                    action: str | None) -> dict[str, Any] | None:
    """Resolve using the resident model to pick from prefiltered candidates.

    More robust than pure token matching: the model handles synonyms ("lounge"
    vs "living room"), minor spelling differences, and uses the action verb to
    prefer the right domain (e.g. "play" -> media_player). Falls back to the
    deterministic resolver if the model is unavailable or unsure.
    """
    entities = await _fetch_entities()
    if not entities:
        return None
    query = _tokens(f"{room or ''} {device_id or ''}")
    if not query:
        return None
    candidates = _prefilter(query, entities)
    if not candidates:
        return {"entity_id": None, "score": 0.0, "candidates": []}
    # Single obvious candidate: skip the model call.
    if len(candidates) == 1:
        return _finalise(candidates[0], action, 1.0)

    import json as _json
    from ..llm import ask_ollama, LocalModelError
    listing = "\n".join(f'{i}: {c["entity_id"]} ("{c["friendly_name"]}")'
                        for i, c in enumerate(candidates))
    system = (
        "You map a spoken home-control request to ONE Home Assistant entity id "
        "from a candidate list. Consider room, device, synonyms, and the action "
        "verb (e.g. play/pause -> a media_player; on/off -> a light or switch). "
        'Reply with ONLY JSON: {"index": <n>} for the best match, or '
        '{"index": null} if none fit. No prose.'
    )
    user = (f'Request: room="{room or ""}" device="{device_id or ""}" action="{action or ""}"\n'
            f"Candidates:\n{listing}")
    try:
        raw = await ask_ollama(user, system=system, role="chat")
        pick = _json.loads(raw.strip().strip("`"))
        idx = pick.get("index")
    except (LocalModelError, ValueError, AttributeError, KeyError):
        # Model unavailable or bad output: fall back to deterministic matching.
        return await resolve_entity(room, device_id, action)
    if idx is None or not isinstance(idx, int) or not (0 <= idx < len(candidates)):
        # Model declined or gave a bad index: fall back to strict matching, which
        # may still find an all-words match or return candidates.
        return await resolve_entity(room, device_id, action)
    return _finalise(candidates[idx], action, 1.0)


def _finalise(entity: dict[str, Any], action: str | None, score: float) -> dict[str, Any]:
    domain = entity["entity_id"].split(".", 1)[0]
    dom_service = _verb_service(domain, action)
    if not dom_service:
        return {"entity_id": None, "score": score, "candidates": [entity["entity_id"]]}
    return {"entity_id": entity["entity_id"], "domain": dom_service[0],
            "service": dom_service[1], "score": score,
            "candidates": [entity["entity_id"]]}


async def resolve_entity(room: str | None, device_id: str | None,
                         action: str | None) -> dict[str, Any] | None:
    """Best-effort map (room, device, action) to a concrete entity + service.

    Returns {"entity_id", "domain", "service", "score", "candidates"} or None
    if nothing plausibly matches. Caller should treat a low score as a miss.
    """
    entities = await _fetch_entities()
    if not entities:
        return None
    query = _tokens(f"{room or ''} {device_id or ''}")
    if not query:
        return None
    ranked = sorted(entities, key=lambda e: _score(query, e), reverse=True)
    best = ranked[0]
    best_score = _score(query, best)
    # Require every query word to be matched (score >= 1.0) to avoid firing the
    # wrong device. "study desk lamp" must hit an entity containing study+cat+neon.
    if best_score < 1.0:
        return {"entity_id": None, "score": best_score,
                "candidates": [e["entity_id"] for e in ranked[:5]]}
    domain = best["entity_id"].split(".", 1)[0]
    dom_service = _verb_service(domain, action)
    if not dom_service:
        return {"entity_id": None, "score": best_score,
                "candidates": [e["entity_id"] for e in ranked[:5]]}
    return {
        "entity_id": best["entity_id"],
        "domain": dom_service[0],
        "service": dom_service[1],
        "score": best_score,
        "candidates": [e["entity_id"] for e in ranked[:5]],
    }
