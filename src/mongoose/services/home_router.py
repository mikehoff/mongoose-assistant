from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..db import audit, connect, utc_now_iso
from ..settings import get_settings


# Non-destructive control services Mongoose is permitted to call. The tool model
# may propose any domain/service; only these are actually executed.
ALLOWED_HA_SERVICES = {
    ("light", "turn_on"), ("light", "turn_off"), ("light", "toggle"),
    ("switch", "turn_on"), ("switch", "turn_off"), ("switch", "toggle"),
    ("media_player", "media_play"), ("media_player", "media_pause"),
    ("media_player", "media_stop"), ("media_player", "volume_set"),
    ("media_player", "turn_on"), ("media_player", "turn_off"),
    ("scene", "turn_on"),
    ("cover", "open_cover"), ("cover", "close_cover"),
    ("climate", "set_temperature"),
    ("fan", "turn_on"), ("fan", "turn_off"),
}


class HomeCommand(BaseModel):
    room: str
    device_id: str
    action: str
    entity_id: str | None = None
    domain: str = "homeassistant"
    service: str = "turn_on"
    privacy: str = "medium"
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass
class RouteAttempt:
    route: str
    success: bool
    reason: str | None = None
    verified_state: str | None = None
    response: dict[str, Any] | None = None


def route_key(command: HomeCommand) -> str:
    return f"{command.room}:{command.device_id}:{command.action}"


def list_route_preferences(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            """
            SELECT route_key, device_id, room, action, preferred_route, confidence, evidence_json, updated_at
            FROM routing_preferences
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def _get_preference(key: str) -> str | None:
    with connect() as con:
        row = con.execute("SELECT * FROM routing_preferences WHERE route_key = ?", (key,)).fetchone()
        # Must match the write threshold in _update_preference (0.75); otherwise
        # preferences written in a 0.75-0.79 band would be stored but never used.
        if row and row["confidence"] >= 0.75:
            return row["preferred_route"]
    return None


def _log_attempt(command: HomeCommand, attempt: RouteAttempt) -> None:
    key = route_key(command)
    with connect() as con:
        con.execute(
            """
            INSERT INTO routing_outcomes(route_key, device_id, room, action, attempted_route, success, failure_reason, verified_state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                command.device_id,
                command.room,
                command.action,
                attempt.route,
                1 if attempt.success else 0,
                attempt.reason,
                attempt.verified_state,
                utc_now_iso(),
            ),
        )
    audit("home", "route_attempt", attempt.success, route=attempt.route, privacy=command.privacy, reason=attempt.reason, payload=command.model_dump())


def _update_preference(command: HomeCommand) -> None:
    key = route_key(command)
    with connect() as con:
        rows = con.execute(
            """
            SELECT attempted_route, success, COUNT(*) AS n
            FROM routing_outcomes
            WHERE route_key = ?
            GROUP BY attempted_route, success
            """,
            (key,),
        ).fetchall()
        stats: dict[str, dict[str, int]] = {}
        for row in rows:
            route = row["attempted_route"]
            stats.setdefault(route, {"success": 0, "failure": 0})
            if row["success"]:
                stats[route]["success"] += row["n"]
            else:
                stats[route]["failure"] += row["n"]

        best_route = None
        best_rate = 0.0
        for route, data in stats.items():
            total = data["success"] + data["failure"]
            if total < 3:
                continue
            rate = data["success"] / total
            if rate > best_rate:
                best_rate = rate
                best_route = route

        if best_route and best_rate >= 0.75:
            con.execute(
                """
                INSERT OR REPLACE INTO routing_preferences(route_key, device_id, room, action, preferred_route, confidence, evidence_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, command.device_id, command.room, command.action, best_route, best_rate, json.dumps(stats), utc_now_iso()),
            )


# Expected entity state after a simple service, for outcome validation (P1.3). Only services
# with an unambiguous resulting state are listed; toggle/dimming/etc. are intentionally
# absent (we report accepted_unverified for those rather than guessing).
_EXPECTED_STATE = {
    "turn_on": "on", "turn_off": "off",
    "open_cover": "open", "close_cover": "closed",
    "lock": "locked", "unlock": "unlocked",
}


async def _call_home_assistant(command: HomeCommand) -> RouteAttempt:
    settings = get_settings()
    if not settings.ha_url or not settings.ha_token:
        return RouteAttempt("home_assistant", False, "not_configured")
    if not command.entity_id:
        return RouteAttempt("home_assistant", False, "entity_id_required")
    # Safety allowlist: the tool model can propose arbitrary domain/service, but
    # we only actually call a small set of non-destructive control services.
    # Anything else is rejected and audited, so a compromised planner (or a
    # prompt-injected tool result) can't invoke arbitrary HA services.
    if (command.domain, command.service) not in ALLOWED_HA_SERVICES:
        return RouteAttempt("home_assistant", False,
                            f"service_not_allowed:{command.domain}.{command.service}")

    # The RESOLVED target wins. A payload that carries its own entity_id must NOT be able
    # to redirect the call to a different entity than the one Mongoose resolved, audited, and
    # safety-checked , otherwise it could log/authorise light.safe while actually controlling
    # light.other. If the payload names a DIFFERENT entity, reject it outright (a mismatch is
    # either a bug or an injection attempt, never something to silently honour). (QA-audit P0.1)
    service_payload = dict(command.payload)
    payload_entity = service_payload.get("entity_id")
    if payload_entity is not None and payload_entity != command.entity_id:
        return RouteAttempt("home_assistant", False,
                            f"entity_id_mismatch:resolved={command.entity_id},payload={payload_entity}")
    service_payload["entity_id"] = command.entity_id
    from .egress_guard import egress_allowed
    if not egress_allowed("ha_actuate")[0]:
        return RouteAttempt("home_assistant", False, "egress_blocked:non_live_context")
    url = f"{settings.ha_url.rstrip('/')}/api/services/{command.domain}/{command.service}"
    headers = {"Authorization": f"Bearer {settings.ha_token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=settings.ha_timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=service_payload)
            if response.status_code >= 400:
                return RouteAttempt("home_assistant", False, f"http_{response.status_code}")
            state_url = f"{settings.ha_url.rstrip('/')}/api/states/{command.entity_id}"
            state_response = await client.get(state_url, headers=headers)
            verified_state = None
            if state_response.status_code < 400:
                verified_state = state_response.json().get("state")
            # OUTCOME VALIDATION (QA-audit P1.3): a 200 from the service call only means HA
            # ACCEPTED the request, not that the device reached the intended state. For simple
            # services with a known expected state, compare , a turn_off that leaves the light
            # 'on' is a state_mismatch, not a success. Where we can't know the expected state
            # (toggle, dimming, unknown service), we report accepted_unverified rather than
            # over-claiming success.
            expected = _EXPECTED_STATE.get(command.service)
            if expected is not None and verified_state is not None:
                if verified_state != expected:
                    return RouteAttempt("home_assistant", False,
                                        f"state_mismatch:expected={expected},actual={verified_state}",
                                        verified_state=verified_state,
                                        response={"status_code": response.status_code})
                outcome = "verified"
            else:
                outcome = "accepted_unverified"
            return RouteAttempt("home_assistant", True, verified_state=verified_state,
                                response={"status_code": response.status_code, "outcome": outcome})
    except Exception as exc:
        return RouteAttempt("home_assistant", False, str(exc))


# Domains whose control actions are inherently low-risk: actuating them carries no private
# CONTENT (unlike reading an email aloud). For these, Alexa fallback is eligible regardless
# of the command's `privacy` field , which is really about speech/content, not actuation.
# This is what keeps the fallback path alive for the normal case (the planner often omits
# privacy, so it defaults to "medium" and would otherwise block every rescue). (QA-audit P1.2)
_LOW_RISK_CONTROL_DOMAINS = {"light", "switch", "fan", "cover", "media_player", "climate"}


def _alexa_fallback_allowed(command: HomeCommand) -> bool:
    """Alexa fallback is allowed when the command is explicitly low-privacy OR it is a pure
    low-risk CONTROL action (a safe domain + a safe control service). A lock/unlock or an
    unknown domain still requires explicit low privacy."""
    if command.privacy == "low":
        return True
    return (command.domain in _LOW_RISK_CONTROL_DOMAINS
            and (command.domain, command.service) in ALLOWED_HA_SERVICES)


async def _call_alexa_bridge(command: HomeCommand) -> RouteAttempt:
    settings = get_settings()
    if not _alexa_fallback_allowed(command):
        return RouteAttempt("alexa", False, "privacy_blocked")
    if not settings.alexa_fallback_url:
        return RouteAttempt("alexa", False, "not_configured")
    from .egress_guard import egress_allowed
    if not egress_allowed("alexa_bridge")[0]:
        return RouteAttempt("alexa", False, "egress_blocked:non_live_context")

    headers = {"Content-Type": "application/json"}
    if settings.alexa_fallback_token:
        headers["Authorization"] = f"Bearer {settings.alexa_fallback_token}"
    try:
        async with httpx.AsyncClient(timeout=settings.alexa_timeout_seconds) as client:
            response = await client.post(settings.alexa_fallback_url, headers=headers, json=command.model_dump())
            if response.status_code >= 400:
                return RouteAttempt("alexa", False, f"http_{response.status_code}")
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            return RouteAttempt("alexa", True, response=data)
    except Exception as exc:
        return RouteAttempt("alexa", False, str(exc))


async def route_home_command(command: HomeCommand) -> dict[str, Any]:
    key = route_key(command)
    # HA-FIRST is invariant: Home Assistant is always attempted first, so it is always
    # re-probed (even after a prior failure that made Alexa the learned preference) and a
    # learned Alexa preference can never silently bypass HA. The preference only orders the
    # FALLBACK tier (which alternative to try after HA), never displaces HA. (QA-audit P1.1)
    preferred = _get_preference(key)
    routes = ["home_assistant"]
    fallback_order = ["alexa"]
    if preferred and preferred != "home_assistant" and preferred in fallback_order:
        fallback_order = [preferred] + [r for r in fallback_order if r != preferred]
    routes.extend(fallback_order)

    attempts: list[RouteAttempt] = []
    for route in routes:
        if route == "home_assistant":
            attempt = await _call_home_assistant(command)
        elif route == "alexa":
            attempt = await _call_alexa_bridge(command)
        else:
            continue
        _log_attempt(command, attempt)
        attempts.append(attempt)
        if attempt.success:
            _update_preference(command)
            return {
                "ok": True,
                "route": attempt.route,
                "say": "Sorted." if attempt.route == "home_assistant" else "Home Assistant sulked, Alexa did it. I’ll remember that.",
                "attempts": [a.__dict__ for a in attempts],
            }

    _update_preference(command)
    return {
        "ok": False,
        "route": None,
        "say": "Nope. Home Assistant failed and Alexa didn’t rescue it.",
        "attempts": [a.__dict__ for a in attempts],
    }


# --- Time Alerts: speaker announce -------------------------------------------
# Announce text on a speaker via Home Assistant. This is DELIBERATELY separate from the control
# allowlist above: announcing is an opt-in capability for Time Alerts, not part of general home
# control, so enabling it can't widen what the tool model can do to lights/locks/etc. It uses a
# configurable service (default tts.google_translate_say) and the same verified HA call path.

async def announce_via_home_assistant(target: str, text: str) -> bool:
    """Speak `text` on the HA media_player/notify target. Returns whether HA accepted the call.
    Honest: a 2xx from HA means the service was invoked, not that audio definitely played, but
    it's the best signal available without a microphone. Returns False (not an exception) when
    HA isn't configured, so the caller's phone-push floor still stands."""
    settings = get_settings()
    if not settings.ha_url or not settings.ha_token:
        return False
    # the announce service + how the target is addressed are configurable, because tts/notify
    # setups vary (tts.google_translate_say with entity_id, notify.alexa_media with target, etc.)
    service = getattr(settings, "alert_announce_service", "tts.google_translate_say")
    try:
        domain, svc = service.split(".", 1)
    except ValueError:
        return False
    # build the payload for the configured service style
    if domain == "tts":
        payload = {"entity_id": target, "message": text}
    elif domain == "notify":
        payload = {"target": target, "message": text}
    else:
        payload = {"entity_id": target, "message": text}

    from .egress_guard import egress_allowed
    if not egress_allowed("ha_announce")[0]:
        return False
    url = f"{settings.ha_url.rstrip('/')}/api/services/{domain}/{svc}"
    headers = {"Authorization": f"Bearer {settings.ha_token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=settings.ha_timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
        ok = 200 <= resp.status_code < 300
        _audit_announce(target, service, ok)
        return ok
    except Exception:
        _audit_announce(target, service, False)
        return False


def _audit_announce(target: str, service: str, ok: bool) -> None:
    try:
        from ..db import audit
        audit("time_alerts", "announce", ok, payload={"target": target, "service": service})
    except Exception:
        pass


# --- Bulk domain control ("all the lights") --------------------------------------------------------
# HA already knows every light/switch/etc and has its own grouping. Rather than reinvent that, Mongoose
# asks HA for the live entity list and acts on the whole domain , EXCEPT the wall-panel hardware, which
# must never be swept (panel safety). This is what makes "turn all the lights on" actually turn them all
# on (minus the panels), using HA as the source of truth instead of guessing a single best-match entity.

# Panel-safety entities are NEVER included in a bulk sweep and never powered off. Imported from the one
# canonical definition so there's a single source of truth.
def _panel_safety_ids() -> frozenset[str]:
    try:
        from .protocol_v3 import PANEL_SAFETY_ENTITIES
        return PANEL_SAFETY_ENTITIES
    except Exception:
        # Defensive fallback: hard-code the known wall-panel entities so a missing import can never
        # cause a bulk op to sweep them.
        return frozenset({
            "light.control_hub", "light.control_hub_2_kitchen", "light.control_hub_ensuite",
            "switch.control_hub_switch_1", "switch.control_hub_switch_2", "switch.control_hub_switch_3",
            "switch.control_hub_2_switch_1", "switch.control_hub_2_switch_2", "switch.control_hub_2_switch_3",
            "switch.control_hub_ensuite_switch_1", "switch.control_hub_ensuite_switch_2",
            "switch.control_hub_ensuite_switch_3",
        })


# Domains that support a clean "all of them on/off". scene is excluded (no "all scenes"); climate/cover
# are excluded from the bulk verb because "all" is rarely what's meant and the verbs differ.
_BULK_DOMAINS = {"light": ("turn_on", "turn_off"),
                 "switch": ("turn_on", "turn_off"),
                 "fan": ("turn_on", "turn_off")}


async def control_all_in_domain(domain: str, action: str) -> dict[str, Any]:
    """Turn ON/OFF every entity in a domain (e.g. all lights), using HA's live entity list as the source
    of truth, with the wall-panel entities ALWAYS excluded (panel safety) and never powered off. Calls
    the domain service once with the explicit safe entity_id list, one HA call, not N, so it's atomic
    from HA's side and HA's own logbook/Assist see a single coherent action. Read-then-act; never raises.
    """
    settings = get_settings()
    if not settings.ha_url or not settings.ha_token:
        return {"ok": False, "say": "Home Assistant isn't connected, so I can't reach the lights.",
                "reason": "not_configured"}
    from .egress_guard import egress_allowed
    if not egress_allowed("ha_bulk")[0]:
        return {"ok": False, "say": "", "reason": "egress_blocked_non_live"}
    domain = (domain or "light").strip().lower()
    act = (action or "on").strip().lower()
    if domain not in _BULK_DOMAINS:
        return {"ok": False, "say": f"I can only do 'all' for {', '.join(sorted(_BULK_DOMAINS))}.",
                "reason": f"domain_not_bulkable:{domain}"}
    service = _BULK_DOMAINS[domain][0] if act in ("on", "turn_on", "") else _BULK_DOMAINS[domain][1]

    # Pull the live entity list straight from HA (its truth), filter to this domain, drop panel-safety.
    url = f"{settings.ha_url.rstrip('/')}/api/states"
    headers = {"Authorization": f"Bearer {settings.ha_token}"}
    try:
        async with httpx.AsyncClient(timeout=settings.ha_timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        return {"ok": False, "say": "Couldn't reach Home Assistant to list the lights.",
                "reason": f"unreachable:{type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "say": f"Home Assistant returned {resp.status_code} when I asked for the lights.",
                "reason": f"http_{resp.status_code}"}

    panels = _panel_safety_ids()
    targets = []
    for st in resp.json():
        eid = st.get("entity_id", "")
        if eid.split(".", 1)[0] != domain:
            continue
        if eid in panels:
            continue  # NEVER sweep a wall-panel entity
        targets.append(eid)
    if not targets:
        return {"ok": True, "say": f"No {domain}s to switch (after excluding the wall panels).",
                "count": 0, "excluded_panels": [p for p in panels if p.split('.',1)[0] == domain]}

    # One service call with the explicit safe list. HA applies it as a single coherent action, so HA's
    # logbook and its own Assist see exactly one event, not a storm of N calls.
    call_url = f"{settings.ha_url.rstrip('/')}/api/services/{domain}/{service}"
    payload = {"entity_id": targets}
    try:
        async with httpx.AsyncClient(timeout=settings.ha_timeout_seconds) as client:
            r = await client.post(call_url, headers={**headers, "Content-Type": "application/json"},
                                  json=payload)
    except Exception as exc:
        return {"ok": False, "say": "Home Assistant didn't take the bulk command.",
                "reason": f"call_failed:{type(exc).__name__}"}
    ok = 200 <= r.status_code < 300
    try:
        audit("home.bulk", json.dumps({"domain": domain, "service": service,
                                       "count": len(targets), "excluded": len(panels)}))
    except Exception:
        pass
    verb = "on" if service.endswith("on") else "off"
    return {
        "ok": ok,
        "say": (f"Turned {verb} {len(targets)} {domain}{'s' if len(targets) != 1 else ''}."
                if ok else f"Home Assistant rejected the bulk command (HTTP {r.status_code})."),
        "count": len(targets),
        "service": f"{domain}.{service}",
        "excluded_panels": [p for p in panels if p.split(".", 1)[0] == domain],
        "note": "Wall-panel entities are always excluded from 'all' for hardware safety.",
    }


# --- Intent via HA's own Assist (natural-language understanding) ------------------------------------
# Instead of Mongoose guessing an entity_id from a room/device word, hand the user's RAW prompt to Home
# Assistant's conversation engine (/api/conversation/process). HA knows its own areas, device aliases,
# floors and groups, so it reconstructs intent from ambiguous phrasing ("turn on the thing by the sofa",
# "make the lounge cosy") far better than token overlap. HA actuates and returns a spoken response.
#
# Safety: HA Assist can in principle touch any exposed entity, including the wall panels. We can't
# inspect HA's chosen targets before it acts (the API actuates as it parses), so the protection here is:
#   - we only forward prompts that passed Mongoose's own home-control gate (an actual command), and
#   - we PRE-SCAN the prompt: if it names a panel entity or a blanket "everything/all" that could sweep a
#     panel, we refuse and fall back to the explicit, panel-safe paths (single-entity resolver / bulk).
# This keeps the hardware rule intact while still using HA's NLU for the normal case.

def _prompt_risks_panel(prompt: str) -> bool:
    """True if the raw prompt could cause HA Assist to actuate a wall-panel entity (by naming it, or by
    a blanket 'everything off' that HA might apply to a panel). Such prompts must NOT be forwarded to HA
    Assist; they go through Mongoose's explicit panel-safe paths instead."""
    p = (prompt or "").lower()
    panel_words = ("control hub", "control_hub", "wall panel", "wall-panel", "panel")
    if any(w in p for w in panel_words):
        return True
    # a blanket sweep that HA could apply to everything incl. panels
    if ("everything" in p or "all the" in p or "all lights" in p or "every light" in p) and \
       any(v in p for v in ("off", "on", "kill")):
        return True
    return False


async def control_via_ha_assist(prompt: str, *, language: str = "en") -> dict[str, Any]:
    """Forward the user's natural-language prompt to HA's conversation engine so HA reconstructs the
    intent and actuates. Returns HA's spoken reply and whether it matched anything. Pre-scans for
    panel-safety risk and refuses those (caller falls back to the explicit safe paths). Never raises.
    """
    settings = get_settings()
    if not settings.ha_url or not settings.ha_token:
        return {"ok": False, "reason": "not_configured", "say": "Home Assistant isn't connected."}
    from .egress_guard import egress_allowed
    if not egress_allowed("ha_assist")[0]:
        return {"ok": False, "reason": "egress_blocked_non_live", "say": ""}
    if _prompt_risks_panel(prompt):
        return {"ok": False, "reason": "panel_safety",
                "say": "That could touch a wall panel; using the safe path instead.",
                "fall_back": True}
    url = f"{settings.ha_url.rstrip('/')}/api/conversation/process"
    headers = {"Authorization": f"Bearer {settings.ha_token}", "Content-Type": "application/json"}
    payload = {"text": prompt, "language": language}
    try:
        async with httpx.AsyncClient(timeout=settings.ha_timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"unreachable:{type(exc).__name__}",
                "say": "Couldn't reach Home Assistant.", "fall_back": True}
    if resp.status_code != 200:
        return {"ok": False, "reason": f"http_{resp.status_code}",
                "say": f"Home Assistant Assist returned {resp.status_code}.", "fall_back": True}
    data = resp.json()
    # HA conversation response: {response: {speech: {plain: {speech}}, response_type, data:{targets,...}}}
    response = (data or {}).get("response") or {}
    speech = (((response.get("speech") or {}).get("plain") or {}).get("speech") or "").strip()
    rtype = response.get("response_type", "")
    # response_type 'error' or 'action_done' tells us if HA understood + did something
    matched = rtype in ("action_done", "query_answer")
    success_targets = (((response.get("data") or {}).get("success")) or [])
    failed_targets = (((response.get("data") or {}).get("failed")) or [])
    try:
        audit("home.assist", json.dumps({"prompt": prompt[:160], "response_type": rtype,
                                         "matched": matched}))
    except Exception:
        pass
    if rtype == "error" or (not matched and not speech):
        return {"ok": False, "reason": f"assist_no_match:{rtype}",
                "say": speech or "Home Assistant didn't recognise that.", "fall_back": True}
    return {
        "ok": matched,
        "route": "ha_assist",
        "say": speech or ("Done." if matched else "Home Assistant heard, but didn't act."),
        "response_type": rtype,
        "targets_succeeded": len(success_targets),
        "targets_failed": len(failed_targets),
        # if HA actioned nothing AND didn't answer a question, let the caller fall back
        "fall_back": (not matched and rtype != "query_answer"),
    }
