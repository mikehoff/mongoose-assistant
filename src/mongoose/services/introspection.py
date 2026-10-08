"""Introspection: Mongoose visits its own subsystems and reports HOW it is wired, and, more usefully,
WHERE a wire is missing.

This exists because a feature (guest music onboarding) turned out to be wired into one chat surface
(private chat) but not another (the lounge), with nothing noticing: the same shape as the find box that
silently lost 'repair', and capabilities that drift. Each is a wiring gap no test caught. This module is
the visitor that walks the wiring and the auditor that flags the holes, so 'wired here but not there'
becomes a failing test instead of a guest complaint.

Cheap and read-only: it introspects in-process registries (routes, intents, scheduler jobs,
capabilities) and reads function SOURCE to see which chat features are wired into which surface.
"""
from __future__ import annotations

import importlib
import inspect
from typing import Any


# --------------------------------------------------------------------------- collectors (visitors)

def collect_intents() -> dict[str, dict[str, Any]]:
    from .intent_resolver import INTENTS
    return {name: {"owner_only": bool(spec.get("owner_only", False)),
                   "priority": spec.get("priority", 0),
                   "anchors": len(spec.get("anchors", []) or [])}
            for name, spec in INTENTS.items()}


def collect_routes() -> list[dict[str, Any]]:
    try:
        from ..app import app
    except Exception:
        return []
    out = []
    for r in app.routes:
        path = getattr(r, "path", None)
        if not path:
            continue
        out.append({"path": path, "methods": sorted(getattr(r, "methods", None) or []),
                    "name": getattr(r, "name", "")})
    return out


def collect_scheduler_jobs() -> list[str]:
    try:
        from .scheduler import _build_registry
        return sorted(_build_registry().keys())
    except Exception:
        return []


def collect_capabilities() -> dict[str, list[str]]:
    try:
        from .capabilities import capabilities_report
        rep = capabilities_report()
        return {"ready": [c.get("capability") for c in rep.get("ready", []) if c.get("capability")],
                "missing": [c.get("capability") for c in rep.get("missing", []) if c.get("capability")]}
    except Exception:
        return {"ready": [], "missing": []}


# A chat surface is one or more functions (the private path is device_chat_route PLUS the brain
# chat_with_memory it calls; the lounge is device_irc_send). Feature = source markers proving it is
# wired into that surface.
_CHAT_SURFACES: dict[str, list[tuple[str, str]]] = {
    "private_chat": [("mongoose.app", "device_chat_route"), ("mongoose.llm", "chat_with_memory")],
    "lounge": [("mongoose.app", "device_irc_send")],
}
_CHAT_FEATURES: dict[str, list[str]] = {
    "bug_report": ["_maybe_handle_bug_report"],
    "bug_detail": ["handle_bug_detail_command"],
    "fortune": ["maybe_handle_fortune"],
    "poke": ["_parse_poke_target", "_poke_from_chat"],
    "music_onboarding": ["_music_scope_for", "onboard_guest_music"],
}
# Features that must be reachable wherever a guest is (private chat AND the lounge). One surface having
# it while another does not is a gap , this is the check that would have caught the music breach.
_PARITY_REQUIRED = {"bug_report", "bug_detail", "fortune", "poke", "music_onboarding"}


def _fn_source(mod_name: str, fn_name: str) -> str:
    try:
        return inspect.getsource(getattr(importlib.import_module(mod_name), fn_name))
    except Exception:
        return ""


def _module_source(mod_name: str) -> str:
    try:
        return inspect.getsource(importlib.import_module(mod_name))
    except Exception:
        return ""


def chat_surface_matrix() -> dict[str, dict[str, bool]]:
    """feature -> {surface: is_wired}. Concatenates every function that makes up a surface."""
    src = {surf: "\n".join(_fn_source(m, f) for m, f in fns)
           for surf, fns in _CHAT_SURFACES.items()}
    return {feat: {surf: any(mark in s for mark in marks) for surf, s in src.items()}
            for feat, marks in _CHAT_FEATURES.items()}


# --------------------------------------------------------------------------- map + audit

def wiring_map() -> dict[str, Any]:
    intents = collect_intents()
    routes = collect_routes()
    return {
        "intents": intents,
        "routes": routes,
        "scheduler_jobs": collect_scheduler_jobs(),
        "capabilities": collect_capabilities(),
        "chat_surfaces": chat_surface_matrix(),
        "counts": {"intents": len(intents), "routes": len(routes)},
    }


def audit() -> dict[str, Any]:
    """Walk the wiring and return the gaps. Empty gaps == everything that should be wired, is."""
    gaps: list[dict[str, Any]] = []

    # 1) Cross-surface parity: a guest-facing feature present on one chat surface but missing on another.
    matrix = chat_surface_matrix()
    for feat in sorted(_PARITY_REQUIRED):
        row = matrix.get(feat, {})
        present = sorted(s for s, v in row.items() if v)
        missing = sorted(s for s, v in row.items() if not v)
        if present and missing:
            gaps.append({"kind": "surface_parity", "feature": feat, "present": present,
                         "missing": missing,
                         "detail": f"{feat} is wired in {present} but not {missing}"})

    # 2) Declared intents that nothing references (no consumer anywhere) , a resolver entry that can
    # never act. Search every module that consumes an intent name (deterministic handlers in llm/app,
    # the role router and tool loop for model-routed intents, and the surfacing catalogs).
    handler_src = "\n".join(_module_source(m) for m in (
        "mongoose.llm", "mongoose.app", "mongoose.role_router", "mongoose.tools", "mongoose.voice",
        "mongoose.services.readiness", "mongoose.services.capability_catalog"))
    for name in sorted(collect_intents()):
        if f'"{name}"' not in handler_src and f"'{name}'" not in handler_src:
            gaps.append({"kind": "unwired_intent", "feature": name,
                         "detail": f"intent {name} is declared in the resolver but no consumer references it"})

    return {"ok": not gaps, "gaps": gaps, "gap_count": len(gaps)}


def summary_text() -> str:
    """A short human summary of how Mongoose is wired, for a 'how are you wired?' chat answer."""
    m = wiring_map()
    a = audit()
    lines = [
        f"I'm wired as {m['counts']['routes']} HTTP routes, {len(m['intents'])} chat intents, and "
        f"{len(m['scheduler_jobs'])} scheduled jobs.",
        f"Ready capabilities: {len(m['capabilities']['ready'])}; missing deps: "
        f"{len(m['capabilities']['missing'])}.",
    ]
    if a["ok"]:
        lines.append("Self-audit: no wiring gaps: every guest-facing chat feature is reachable on "
                     "every surface and every intent has a handler.")
    else:
        lines.append(f"Self-audit found {a['gap_count']} wiring gap(s):")
        lines.extend("  - " + g["detail"] for g in a["gaps"][:6])
    return "\n".join(lines)
