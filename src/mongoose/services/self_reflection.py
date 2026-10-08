"""Bounded self-reflection: notice when a feature that SHOULD be wired isn't, and (within tight
limits) wire it, then tell the v3 peers so Claude Chat gets visibility.

Why this exists
---------------
The connector-exposure gap (a probe layer that existed but wasn't reachable from the single brain) was
only caught because a human asked "is this actually wired to chat?". The system had no way to notice its
own missing wiring. This module is that noticer, a small, declarative set of INVARIANTS about how the
single brain is supposed to be wired, each checked against the LIVE system, each with an honest finding
and (where safe) a bounded auto-fix.

Safety model (deliberately narrow)
----------------------------------
- This NEVER writes or generates arbitrary code. It cannot author features.
- It can only perform AUTO-FIXES from a fixed allowlist of reversible, in-memory registration actions
  (e.g. "an executor function exists but isn't in READ_TOOLS" -> add the name to READ_TOOLS at runtime).
  Anything structural (new endpoints, new modules) is REPORTED for a human/Claude to action, never done.
- It is read-mostly: detection touches only in-process registries and module attributes, no network.
- v3 peer notification is M-gated exactly like every other v3 action: nothing is posted to the bus unless
  protocol_v3 is enabled. A detected gap is recorded locally regardless; the bus post is the optional
  "tell Claude Chat" step.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Callable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Finding:
    invariant: str                 # short id, e.g. "tool_in_read_tools"
    subject: str                   # what it's about, e.g. "connector_status"
    ok: bool                       # is the invariant satisfied?
    detail: str = ""               # human-readable
    severity: str = "info"         # info | warn | gap
    auto_fixable: bool = False     # can a bounded auto-fix close it?
    fixed: bool = False            # did we close it this run?
    evidence: str = ""
    checked_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------------------
# Invariants. Each returns a Finding. They describe how the SINGLE BRAIN is supposed to be wired so a
# capability that exists in code is actually reachable + actionable through chat/voice/text.
# --------------------------------------------------------------------------------------------------

def _inv_read_tools_complete() -> list[Finding]:
    """Every read-only tool executor registered in TOOLS should be listed in READ_TOOLS, otherwise the
    single brain can't safely call it on a plain question (it'd be treated as a mutating tool). This is
    AUTO-FIXABLE: adding a name to the READ_TOOLS set at runtime is reversible and harmless.

    We can't infer read-vs-write perfectly, so we only flag tools whose name or description strongly
    signals read-only (status/check/list/get/probe/show), a conservative allowlist, never a guess that
    could mark a mutating tool as read-only."""
    from .. import tools as T
    findings: list[Finding] = []
    # Sensitivity markers: if a description signals these, the tool is NOT safe to auto-mark read-only,
    # even if its name looks innocent. A false "this is read-only" is worse than the gap it closes.
    sensitive_markers = ("sensitive", "apply", "write", "send", "delete", "modif", "mutat", "execute",
                         "install", "approve", "move money", "payment", "irreversible")
    for name, spec in T.TOOLS.items():
        if name in T.READ_TOOLS:
            continue
        desc = str(spec.get("description", "")).lower()
        if any(m in desc for m in sensitive_markers):
            continue  # never auto-touch anything that hints at side effects
        # Only treat as auto-fixable when the description EXPLICITLY declares read-only. A name that
        # merely looks read-only ("check_*") is reported as a softer finding for a human, not auto-fixed.
        explicit_read_only = desc.startswith("read-only") or "read-only" in desc[:80]
        name_looks_read_only = any(name.startswith(p) for p in ("get_", "list_", "show_")) or \
            any(s in name for s in ("status", "probe", "summary"))
        if explicit_read_only:
            findings.append(Finding(
                "tool_in_read_tools", subject=name, ok=False, severity="gap",
                detail=f"Tool '{name}' is declared read-only but isn't in READ_TOOLS, so the brain "
                       f"won't call it on a plain question.",
                auto_fixable=True, evidence="description starts 'read-only'"))
        elif name_looks_read_only:
            findings.append(Finding(
                "tool_in_read_tools", subject=name, ok=False, severity="warn",
                detail=f"Tool '{name}' looks read-only by name but isn't explicitly declared so; a human "
                       f"should confirm before adding it to READ_TOOLS.",
                auto_fixable=False, evidence="name signal only, not auto-fixed"))
    if not findings:
        findings.append(Finding("tool_in_read_tools", subject="*", ok=True,
                                detail="All read-only-looking tools are in READ_TOOLS."))
    return findings


def _inv_tools_in_planner_catalogue() -> list[Finding]:
    """Every registered tool must appear in the planner catalogue the tool model reads, otherwise the
    brain literally cannot select it. NOT auto-fixable (the catalogue is generated from TOOLS, so a
    missing one signals a real registration bug to report)."""
    from .. import tools as T
    findings: list[Finding] = []
    try:
        catalogue = T._tool_catalogue()
    except Exception as exc:  # noqa: BLE001
        return [Finding("tool_in_catalogue", subject="*", ok=False, severity="warn",
                        detail=f"Couldn't build the planner catalogue: {exc}")]
    for name in T.TOOLS:
        if name not in catalogue:
            findings.append(Finding(
                "tool_in_catalogue", subject=name, ok=False, severity="gap",
                detail=f"Tool '{name}' is registered but missing from the planner catalogue; the brain "
                       f"can't select it.",
                auto_fixable=False))
    if not findings:
        findings.append(Finding("tool_in_catalogue", subject="*", ok=True,
                                detail="Every registered tool is in the planner catalogue."))
    return findings


def _inv_status_areas_callable() -> list[Finding]:
    """Every registered system_status area must actually be callable (its builder doesn't crash on a
    bare call), so a typo'd/removed brief function is caught. Read-only; NOT auto-fixable (a crashing
    builder is a real bug to report)."""
    findings: list[Finding] = []
    try:
        from .status_tools import _AREAS
    except Exception as exc:  # noqa: BLE001
        return [Finding("status_area_callable", subject="*", ok=False, severity="warn",
                        detail=f"Couldn't load status areas: {exc}")]
    for area, builder in _AREAS.items():
        if area == "connectors":
            continue  # its builder runs LIVE network probes; not exercised during a cheap self-audit
        try:
            out = builder()
            # a healthy brief returns a dict (often {"ok": ...}); we only flag a hard failure
            if not isinstance(out, dict):
                findings.append(Finding("status_area_callable", subject=area, ok=False, severity="warn",
                                        detail=f"system_status area '{area}' returned non-dict."))
        except Exception as exc:  # noqa: BLE001
            findings.append(Finding("status_area_callable", subject=area, ok=False, severity="gap",
                                    detail=f"system_status area '{area}' crashed: {str(exc)[:120]}",
                                    auto_fixable=False))
    if not findings:
        findings.append(Finding("status_area_callable", subject="*", ok=True,
                                detail="All system_status areas are callable."))
    return findings


def _inv_probe_has_chat_path() -> list[Finding]:
    """The exact gap that started this: if a connector PROBE layer exists, the brain must have a tool to
    reach it. Verifies connector_probes -> connector_status tool -> READ_TOOLS chain. AUTO-FIXABLE only
    for the READ_TOOLS membership; a missing tool itself is reported, not fabricated."""
    findings: list[Finding] = []
    try:
        importlib.import_module("mongoose.connector_probes")
        probes_exist = True
    except Exception:
        probes_exist = False
    if not probes_exist:
        return [Finding("probe_chat_path", subject="connector_probes", ok=True,
                        detail="No connector_probes module; nothing to wire.")]
    from .. import tools as T
    has_tool = "connector_status" in T.TOOLS
    in_read = "connector_status" in T.READ_TOOLS
    if has_tool and in_read:
        findings.append(Finding("probe_chat_path", subject="connector_status", ok=True,
                                detail="Connector probes are reachable from the brain via "
                                       "connector_status (read-only)."))
    elif has_tool and not in_read:
        findings.append(Finding("probe_chat_path", subject="connector_status", ok=False,
                                severity="gap", auto_fixable=True,
                                detail="connector_status tool exists but isn't read-only-registered."))
    else:
        findings.append(Finding("probe_chat_path", subject="connector_status", ok=False,
                                severity="gap", auto_fixable=False,
                                detail="Connector probes exist but no connector_status chat tool wires "
                                       "them to the single brain. Needs wiring (reported, not auto-made)."))
    return findings


def _inv_known_deferred_surfaces() -> list[Finding]:
    """Record capabilities that a review identified as SHOULD-exist but are deliberately deferred (not
    yet wired), so the system keeps surfacing them to a human rather than silently forgetting. These are
    never auto-fixed, they need real modules (settings, providers), which this module won't author."""
    from .. import tools as T
    findings: list[Finding] = []
    # Each: (subject, present_check, why_it_matters)
    deferred = [
        ("connector_sync_action",
         "connector_status" in T.TOOLS and "action" in str(T.TOOLS.get("connector_status", {}).get("args", {})),
         "the brain should be able to ACT on connectors (sync/verify), not only report"),
        ("whatsapp_connector", _module_exists("mongoose.services.whatsapp"),
         "WhatsApp/WAHA session status should be a first-class connector"),
        ("native_surface_connectors", _module_exists("mongoose.services.native_surfaces"),
         "ntfy/Android-companion/PWA/HTTPS-voice should be first-class probed surfaces"),
    ]
    for subject, present, why in deferred:
        if present:
            findings.append(Finding("known_surface", subject=subject, ok=True,
                                    detail=f"{subject} is wired."))
        else:
            findings.append(Finding("known_surface", subject=subject, ok=False, severity="info",
                                    auto_fixable=False,
                                    detail=f"Deferred: {subject} not wired yet, {why}. Needs a real "
                                           f"module; reported, never auto-authored."))
    return findings


def _module_exists(dotted: str) -> bool:
    try:
        importlib.import_module(dotted)
        return True
    except Exception:
        return False


_INVARIANTS: dict[str, Callable[[], list[Finding]]] = {
    "read_tools_complete": _inv_read_tools_complete,
    "tools_in_planner_catalogue": _inv_tools_in_planner_catalogue,
    "status_areas_callable": _inv_status_areas_callable,
    "probe_has_chat_path": _inv_probe_has_chat_path,
    "known_deferred_surfaces": _inv_known_deferred_surfaces,
}


# --------------------------------------------------------------------------------------------------
# Bounded auto-fixes. The ONLY mutations this module can perform. Each is reversible + in-memory.
# --------------------------------------------------------------------------------------------------

def _fix_add_to_read_tools(subject: str) -> tuple[bool, str]:
    """Add a tool name to READ_TOOLS at runtime. Safe + reversible: it only relaxes a tool from
    'mutating' to 'read-only-callable', and only for tools we positively identified as read-only."""
    from .. import tools as T
    if subject not in T.TOOLS:
        return False, f"no such tool '{subject}'"
    if subject in T.READ_TOOLS:
        return True, "already present"
    try:
        T.READ_TOOLS.add(subject)
        return True, f"added '{subject}' to READ_TOOLS"
    except Exception as exc:  # noqa: BLE001
        return False, f"could not add: {exc}"


_AUTO_FIXES: dict[str, Callable[[str], tuple[bool, str]]] = {
    "tool_in_read_tools": _fix_add_to_read_tools,
    "probe_chat_path": _fix_add_to_read_tools,
}


# --------------------------------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------------------------------

def reflect(*, apply_fixes: bool = False) -> dict[str, Any]:
    """Run every invariant against the LIVE system. With apply_fixes=False (default) this is a pure,
    read-only self-audit: it reports gaps but changes nothing. With apply_fixes=True it additionally
    applies the bounded, allowlisted auto-fixes (only READ_TOOLS registration today) and records what
    it changed. Never writes code; structural gaps are always reported for a human/Claude to action."""
    all_findings: list[Finding] = []
    for inv in _INVARIANTS.values():
        try:
            all_findings.extend(inv())
        except Exception as exc:  # noqa: BLE001
            all_findings.append(Finding("invariant_error", subject=getattr(inv, "__name__", "?"),
                                        ok=False, severity="warn", detail=f"invariant crashed: {exc}"))

    gaps = [f for f in all_findings if not f.ok]
    if apply_fixes:
        for f in gaps:
            if f.auto_fixable and f.invariant in _AUTO_FIXES:
                ok, msg = _AUTO_FIXES[f.invariant](f.subject)
                f.fixed = ok
                f.evidence = (f.evidence + f"; fix: {msg}").strip("; ")

    fixed = [f for f in gaps if f.fixed]
    remaining = [f for f in gaps if not f.fixed]
    return {
        "checked_at": _now_iso(),
        "findings": [f.to_dict() for f in all_findings],
        "summary": {
            "total": len(all_findings),
            "ok": len([f for f in all_findings if f.ok]),
            "gaps": len(gaps),
            "auto_fixed": len(fixed),
            "remaining_gaps": len(remaining),
            "needs_human": [f.subject for f in remaining if not f.auto_fixable],
        },
        "applied_fixes": apply_fixes,
    }


def reflect_and_notify(*, apply_fixes: bool = True) -> dict[str, Any]:
    """Reflect, optionally apply bounded fixes, and, if v3 is enabled, tell the peers (Claude Chat +
    Code) what was detected and closed, so Chat gets visibility into the self-applied patch. The bus
    post is M-gated by protocol_v3 exactly like every other v3 action; if v3 is off, this still returns
    the reflection result, just without posting."""
    result = reflect(apply_fixes=apply_fixes)
    sm = result["summary"]
    posted = False
    note = "v3 disabled; not notifying peers."
    if sm["auto_fixed"] or sm["remaining_gaps"]:
        try:
            from .protocol_v3 import _append_to_bus, build_envelope
            from ..settings import get_settings
            if getattr(get_settings(), "protocol_v3_enabled", False):
                fixed_subjects = [f["subject"] for f in result["findings"] if f.get("fixed")]
                gaps_remaining = sm.get("needs_human", [])
                body = (
                    "mongoose self-reflection ran. "
                    + (f"Auto-wired (bounded, reversible): {', '.join(fixed_subjects)}. " if fixed_subjects else "")
                    + (f"Reported for action (structural, not auto-made): {', '.join(gaps_remaining)}. " if gaps_remaining else "")
                    + "Auto-fixes are limited to runtime read-only-tool registration; no code was authored."
                )
                env = build_envelope(topic="self-reflection-patch", body=body, to="all", needs_ack=False)
                r = _append_to_bus(env)
                posted = bool(r.get("ok"))
                note = ("Posted self-reflection summary to chat + code."
                        if posted else f"v3 enabled but bus post failed: {r.get('detail')}")
        except Exception as exc:  # noqa: BLE001
            note = f"notify error: {exc}"
    else:
        note = "Nothing to notify (no gaps, no fixes)."
    result["peer_notification"] = {"posted": posted, "note": note}
    return result
