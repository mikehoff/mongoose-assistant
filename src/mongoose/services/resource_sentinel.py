"""Resource sentinel, proactive OS-level pressure detection and safe, proportional relief.

WHY THIS EXISTS (the gap it closes):
The three existing watchdogs each catch a different failure, but together they leave a real hole:

  - server_watchdog.py (own launchd process) restarts the server only when /health/live fails N times,
    i.e. a TOTAL freeze. A model run that balloons to many GB of RSS while the server still answers
    /health slips straight past it.
  - runtime_watchdog.py restarts a DEAD scheduler task and records loop_lag_ms, but does not ACT on
    sustained lag, and never looks at memory at all.
  - self_heal.py recovers STALE subsystems on a time basis, not resource pressure.

So nothing here measured actual memory pressure or a single operation looping too long (short of a full
freeze) and did something about it. On a 24 GB Mac mini running a local model plus background jobs, that
is exactly the failure that degrades the whole machine. This sentinel fills it.

WHAT IT DOES, AND WHAT IT DELIBERATELY DOES NOT:
Each watchdog tick it reads OS-level signals (this process's RSS, system memory available, and the
event-loop lag the watchdog already computes) and responds in PROPORTION:

  1. observe: always record the live reading so status/diagnosis is honest.
  2. relieve: under genuine, SUSTAINED pressure, trigger the SAME safe, audited memory-relief
                      the owner can ask for by hand (scheduler_control "free_memory"), which pauses only
                      deferrable/background-heavy jobs and leaves alerts, DDNS, backups, v3 reconcile and
                      RTM operational refresh running. It NEVER bulk-kills and NEVER invents its own kill
                      logic; a true freeze remains the process-watchdog's job.
  3. flag: record a self-heal/hardening incident so the pressure is visible after the fact.

Safety properties that make this safe to run unattended:
  - HYSTERESIS + SUSTAIN: relief only fires after pressure persists across several consecutive ticks,
    so a brief spike (a single large model response) does not trip it.
  - RATE LIMIT: relief fires at most once per cooldown window, so it can never thrash the scheduler.
  - GRACEFUL ABSENCE: if psutil is unavailable, the sentinel degrades to a pure no-op (it never raises
    into the watchdog loop). Memory readings simply become None and no relief is attempted on memory.
  - READ-ONLY ESCALATION: the strongest thing it does on its own is pause deferrable jobs and record an
    incident. It does not restart or kill the server; that decision stays with the process-watchdog and
    its circuit breaker.
"""
from __future__ import annotations

import os
import time
from typing import Any

from ..db import set_app_state


# ── thresholds (deliberately conservative; relief is cheap and reversible, false freezes are not) ──
# System memory available, as a fraction of total, below which the MACHINE is under mild pressure.
# NOTE for macOS: the OS keeps "available" chronically low (aggressive cache + memory compression), so
# a 12% floor fired constantly on healthy Macs and paused the assistant needlessly. 6% is real pressure.
_SYS_AVAIL_FRACTION_FLOOR = float(os.environ.get("MONGOOSE_SENTINEL_SYS_AVAIL_FLOOR", "0.06"))
# Below THIS, pressure is severe: only here may relief touch the PROACTIVE layer (sync/surprisers), and
# only after a longer sustain. Above it, relief is HEAVY-jobs-only so the assistant keeps feeling alive.
_SYS_AVAIL_SEVERE_FLOOR = float(os.environ.get("MONGOOSE_SENTINEL_SYS_AVAIL_SEVERE", "0.03"))
# This process's RSS, as a fraction of total system memory, above which WE are the likely culprit.
_PROC_RSS_FRACTION_CEIL = float(os.environ.get("MONGOOSE_SENTINEL_PROC_RSS_CEIL", "0.55"))
# Event-loop lag (seconds) above which the loop is considered to be under sustained pressure.
_LOOP_LAG_SECONDS_CEIL = float(os.environ.get("MONGOOSE_SENTINEL_LOOP_LAG_CEIL", "5.0"))
# Pressure must persist this many consecutive ticks before mild (heavy-only) relief fires.
_SUSTAIN_TICKS = int(os.environ.get("MONGOOSE_SENTINEL_SUSTAIN_TICKS", "3"))
# Severe relief (which may pause proactive jobs) must persist much longer, it is a bigger hammer.
_SEVERE_SUSTAIN_TICKS = int(os.environ.get("MONGOOSE_SENTINEL_SEVERE_SUSTAIN_TICKS", "8"))
# Minimum seconds between two relief actions (rate limit, so it can never thrash the scheduler).
_RELIEF_COOLDOWN_SECONDS = float(os.environ.get("MONGOOSE_SENTINEL_RELIEF_COOLDOWN", "900"))

SENTINEL_KEY = "resource_sentinel_status"


def _psutil():
    try:
        import psutil  # noqa
        return psutil
    except Exception:
        return None


def read_signals(loop_lag_ms: float | None = None) -> dict[str, Any]:
    """Read live OS-level resource signals. Never raises. Fields are None when unavailable so callers
    can tell 'no pressure' apart from 'could not measure'."""
    out: dict[str, Any] = {
        "sys_available_fraction": None,   # free system memory / total
        "proc_rss_fraction": None,        # this process RSS / total system memory
        "proc_rss_mb": None,
        "loop_lag_seconds": (round(loop_lag_ms / 1000.0, 3) if loop_lag_ms is not None else None),
        "measured": False,
    }
    ps = _psutil()
    if ps is None:
        return out
    try:
        vm = ps.virtual_memory()
        total = float(getattr(vm, "total", 0) or 0)
        avail = float(getattr(vm, "available", 0) or 0)
        if total > 0:
            out["sys_available_fraction"] = round(avail / total, 4)
        proc = ps.Process(os.getpid())
        rss = float(proc.memory_info().rss)
        out["proc_rss_mb"] = round(rss / (1024 * 1024), 1)
        if total > 0:
            out["proc_rss_fraction"] = round(rss / total, 4)
        out["measured"] = True
    except Exception:
        # leave the partial reading; never propagate into the watchdog loop
        pass
    return out


def _under_pressure(sig: dict[str, Any]) -> tuple[bool, list[str]]:
    """Decide whether the current reading indicates genuine pressure, and why. Memory checks only count
    when actually measured (psutil present); loop lag counts whenever the watchdog supplied it."""
    reasons: list[str] = []
    sysf = sig.get("sys_available_fraction")
    if sysf is not None and sysf < _SYS_AVAIL_FRACTION_FLOOR:
        reasons.append(f"system memory low (avail {sysf:.0%} < {_SYS_AVAIL_FRACTION_FLOOR:.0%})")
    rssf = sig.get("proc_rss_fraction")
    if rssf is not None and rssf > _PROC_RSS_FRACTION_CEIL:
        reasons.append(f"process RSS high ({rssf:.0%} of system memory > {_PROC_RSS_FRACTION_CEIL:.0%})")
    lag = sig.get("loop_lag_seconds")
    if lag is not None and lag > _LOOP_LAG_SECONDS_CEIL:
        reasons.append(f"event-loop lag sustained ({lag:.1f}s > {_LOOP_LAG_SECONDS_CEIL:.1f}s)")
    return (bool(reasons), reasons)


class ResourceSentinel:
    """Holds the small amount of state needed for sustain + rate-limit decisions across ticks. One
    instance lives inside the runtime watchdog and is ticked once per watchdog cycle."""

    def __init__(self) -> None:
        self._consecutive = 0
        self._last_relief_at = 0.0
        self._relief_count = 0

    def tick(self, loop_lag_ms: float | None = None) -> dict[str, Any]:
        """Read signals, update sustain state, and take a proportional action if warranted. Returns the
        status dict (also persisted) so the watchdog/UI can show it. Never raises."""
        sig = read_signals(loop_lag_ms)
        pressure, reasons = _under_pressure(sig)
        if pressure:
            self._consecutive += 1
        else:
            self._consecutive = 0

        action = "observe"
        detail = ""
        now = time.time()
        sustained = self._consecutive >= _SUSTAIN_TICKS
        cooled = (now - self._last_relief_at) >= _RELIEF_COOLDOWN_SECONDS

        # Is this SEVERE (may justify pausing the proactive layer) or mild (heavy jobs only)?
        avail = sig.get("sys_available_fraction")
        severe = (avail is not None and avail < _SYS_AVAIL_SEVERE_FLOOR
                  and self._consecutive >= _SEVERE_SUSTAIN_TICKS)

        if sustained and cooled:
            # Proportional response. Mild pressure pauses HEAVY background jobs only, keeping the
            # proactive layer (sync/surprisers/delight) alive. Only genuinely severe, long-sustained
            # pressure escalates to pausing proactive jobs too. Never kills.
            relieved = self._relieve(reasons, severe=severe)
            action = ("severe_relief" if severe else "relief") if relieved else "relief_attempted"
            detail = "; ".join(reasons)
            self._last_relief_at = now
            self._relief_count += 1
            self._consecutive = 0  # reset so we re-accumulate before any further action
        elif sustained:
            action = "pressure_cooldown"  # pressure persists but we relieved recently; do not thrash
            detail = "; ".join(reasons)
        elif pressure:
            action = "pressure_observed"  # pressure this tick but not yet sustained
            detail = "; ".join(reasons)

        status = {
            "at": _now_iso(),
            "signals": sig,
            "consecutive_pressure_ticks": self._consecutive,
            "action": action,
            "detail": detail,
            "relief_count": self._relief_count,
            "psutil": sig.get("measured", False) or (_psutil() is not None),
        }
        try:
            set_app_state(SENTINEL_KEY, _json_dumps(status))
        except Exception:
            pass
        return status

    def _relieve(self, reasons: list[str], *, severe: bool = False) -> bool:
        """Trigger the existing safe memory-relief and record an incident. Returns True if relief ran.
        Mild pressure pauses HEAVY background jobs only; severe pressure escalates to also pausing the
        proactive layer. Either way this pauses (never kills) and auto-resumes after the relief window."""
        ok = False
        try:
            from .scheduler import scheduler_control
            act = "free_memory_all" if severe else "free_memory"
            res = scheduler_control(act, actor="resource_sentinel",
                                    reason="automatic relief: " + "; ".join(reasons))
            ok = bool(res.get("ok", False))
        except Exception:
            ok = False
        # Flag it so the pressure is visible after the fact (best-effort; never blocks relief).
        try:
            from .self_heal import _record_incident, _ensure_incident_table
            _ensure_incident_table()
            _record_incident("resource_sentinel", None, "auto_free_memory",
                             "ok" if ok else "failed", "; ".join(reasons))
        except Exception:
            pass
        try:
            from ..db import audit
            audit("sentinel", "auto_relief", ok, payload={"reasons": reasons})
        except Exception:
            pass
        return ok


def current_status() -> dict[str, Any] | None:
    from ..db import get_app_state
    raw = get_app_state(SENTINEL_KEY, "")
    if not raw:
        return None
    try:
        import json
        return json.loads(raw)
    except Exception:
        return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj)
