"""Mongoose's self-knowledge as evidence.

Every fabrication in the incident was Mongoose inventing about ITSELF: its own scores, its own RAM, whether
it even had web search. It had no grounded self to speak from, so it improvised one. This provider fetches
the real self as evidence rows, subject to the same grounding contract as anything else.

It REUSES the existing self-wiring introspection (services/introspection.py, the visitor/auditor already
built) as the source of truth for what Mongoose can do and how it is wired, and adds only the live-state
probes introspection does not cover (version, pinned models, RAM). What cannot be probed is simply absent,
which the pipeline renders as an honest unknown rather than a guess.
"""
from __future__ import annotations

from typing import Any

from .base import Evidence, Source


def _version() -> "Source | None":
    try:
        from .. import __version__
        return Source(id="self:version", kind="self_config", data={"mongoose_version": str(__version__)})
    except Exception:
        return None


def _models() -> "Source | None":
    try:
        from ..settings import get_settings
        s = get_settings()
        flat = {}
        for role in ("chat", "tool", "code"):
            try:
                flat[f"{role}_model"] = s.model_for_role(role)   # flat scalars so the model can quote them
            except Exception:
                continue
        return Source(id="self:models", kind="self_config", data=flat) if flat else None
    except Exception:
        return None


def _memory() -> "Source | None":
    """Live RAM from a real probe, so 'does the 30b fit' is answered from the machine, not guessed."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return Source(id="self:memory", kind="self_state", data={
            "total_ram_gb": round(vm.total / 1e9, 1),
            "available_ram_gb": round(vm.available / 1e9, 1),
            "used_percent": vm.percent,
        })
    except Exception:
        return None


def _capabilities() -> "Source | None":
    """What Mongoose can actually do, straight from the introspection visitor (not a hand-kept list)."""
    try:
        from ..services.introspection import collect_capabilities
        caps = collect_capabilities() or {}
        ready = [c for c in (caps.get("ready") or []) if c]
        return Source(id="self:capabilities", kind="self_wiring",
                      data={"ready_capabilities": ready}) if ready else None
    except Exception:
        return None


def _tools() -> "Source | None":
    """The tools Mongoose actually has this turn, so 'do you have web search' is a checkable fact."""
    try:
        from ..tools import TOOLS
        return Source(id="self:tools", kind="self_config",
                      data={"tools_available": sorted(TOOLS.keys())})
    except Exception:
        return None


def _wiring() -> "Source | None":
    """Mongoose's awareness of its OWN wiring gaps, from the introspection auditor: honest self-knowledge
    includes what is NOT joined up."""
    try:
        from ..services.introspection import audit
        a = audit() or {}
        return Source(id="self:wiring_audit", kind="self_wiring", data={
            "fully_wired": bool(a.get("ok")),
            "wiring_gap_count": int(a.get("gap_count", 0)),
        })
    except Exception:
        return None


def gather(deps: Any = None) -> Evidence:
    """Best-effort: each probe/collector is independent and never raises. A probe that cannot answer is
    simply left out, so a question it would have covered becomes an honest unknown downstream."""
    rows = (_version(), _models(), _memory(), _capabilities(), _tools(), _wiring())
    return Evidence(sources=[s for s in rows if s is not None])
