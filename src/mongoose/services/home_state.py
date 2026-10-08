from __future__ import annotations

from typing import Any

import httpx

from ..settings import get_settings

# Read-only home-state for the daily brief. This reads a SELECTED, configured set
# of entities (MONGOOSE_BRIEF_HOME_ENTITIES), never the whole house, and only ever
# GETs state, it cannot control anything. It exists so the brief can surface the
# few home facts that matter ("the back door is unlocked", "heating is off and it's
# cold out") without becoming a dashboard. Every call is best-effort and bounded:
# a slow or unreachable HA must not hang or sink the brief.


def _friendly(entity_id: str, attrs: dict[str, Any]) -> str:
    name = attrs.get("friendly_name")
    if name:
        return str(name)
    # fall back to the id minus domain, humanised
    tail = entity_id.split(".", 1)[-1].replace("_", " ")
    return tail


async def read_selected_home_state() -> list[dict[str, Any]]:
    """GET the state of each configured brief entity. Returns a list of
    {entity_id, state, name, domain, device_class} dicts. Empty if HA isn't
    configured, no entities are selected, or anything fails, never raises.

    Uses ONE call to /api/states and filters to the configured entities, so a slow
    or dead HA costs a single timeout, not one per entity."""
    settings = get_settings()
    entities = settings.brief_home_entities
    if not entities or not settings.ha_url or not settings.ha_token:
        return []

    base = settings.ha_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.ha_token}"}
    timeout = min(getattr(settings, "ha_timeout_seconds", 4), 4)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base}/api/states", headers=headers)
            if r.status_code >= 400:
                return []
            by_id = {str(item.get("entity_id") or ""): item for item in (r.json() or [])}
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for entity_id in entities:
        if "/" in entity_id or ".." in entity_id:
            continue  # never let a configured id traverse
        data = by_id.get(entity_id)
        if not data:
            continue
        state = data.get("state")
        if state in (None, "unknown", "unavailable"):
            # still surface unavailability, it's notable, but mark it as such
            out.append({"entity_id": entity_id, "state": "unavailable",
                        "name": _friendly(entity_id, {}), "domain": entity_id.split(".", 1)[0],
                        "device_class": ""})
            continue
        attrs = data.get("attributes", {}) or {}
        out.append({
            "entity_id": entity_id,
            "state": state,
            "name": _friendly(entity_id, attrs),
            "domain": entity_id.split(".", 1)[0],
            "device_class": str(attrs.get("device_class", "")).lower(),
        })
    return out
