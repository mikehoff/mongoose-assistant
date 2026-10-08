"""Runtime resource guard: OS-level checks for memory pressure, stuck jobs and looping models.

This module is deliberately *observational first*. It uses the cheapest system signals available on the
host (psutil when present; otherwise /proc, ps, vm_stat and resource) and returns small JSON-safe
snapshots for health, Cockpit and chat. Mutating recovery remains explicit and conservative: the
scheduler can skip deferrable jobs under pressure, and the external watchdog can restart the Mongoose
server only when the Python process itself is far beyond a configured RSS ceiling.
"""
from __future__ import annotations

import os
import platform
import re
import resource
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Thresholds:
    python_rss_warn_mb: float = 2048.0
    python_rss_restart_mb: float = 8192.0
    system_available_warn_mb: float = 2048.0
    system_used_warn_percent: float = 92.0
    ollama_rss_warn_mb: float = 19000.0
    loop_lag_warn_ms: float = 1500.0
    scheduler_stale_warn_s: float = 360.0


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def thresholds() -> Thresholds:
    return Thresholds(
        python_rss_warn_mb=_float_env("MONGOOSE_PYTHON_RSS_WARN_MB", 2048.0),
        python_rss_restart_mb=_float_env("MONGOOSE_WATCHDOG_MAX_RSS_MB", 8192.0),
        system_available_warn_mb=_float_env("MONGOOSE_SYSTEM_AVAILABLE_WARN_MB", 2048.0),
        system_used_warn_percent=_float_env("MONGOOSE_SYSTEM_USED_WARN_PERCENT", 92.0),
        ollama_rss_warn_mb=_float_env("MONGOOSE_OLLAMA_RSS_WARN_MB", 19000.0),
        loop_lag_warn_ms=_float_env("MONGOOSE_LOOP_LAG_WARN_MS", 1500.0),
        scheduler_stale_warn_s=_float_env("MONGOOSE_SCHEDULER_STALE_WARN_S", 360.0),
    )


def _run(args: list[str], *, timeout: float = 2.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def rss_mb_for_pid(pid: int) -> float | None:
    """Return resident set size for pid in MB using psutil if present, then ps fallback."""
    try:
        import psutil  # type: ignore
        return round(float(psutil.Process(pid).memory_info().rss) / (1024 * 1024), 1)
    except Exception:
        pass
    proc = _run(["ps", "-o", "rss=", "-p", str(pid)])
    if proc and proc.returncode == 0:
        try:
            # ps rss is KiB on macOS/Linux.
            return round(float((proc.stdout or "").strip().split()[0]) / 1024.0, 1)
        except Exception:
            return None
    return None


def current_process_snapshot() -> dict[str, Any]:
    pid = os.getpid()
    rss = rss_mb_for_pid(pid)
    max_rss_mb = None
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        raw = float(getattr(ru, "ru_maxrss", 0.0))
        # Linux reports KiB; macOS reports bytes.
        if platform.system() == "Darwin":
            max_rss_mb = round(raw / (1024 * 1024), 1)
        else:
            max_rss_mb = round(raw / 1024.0, 1)
    except Exception:
        pass
    return {
        "pid": pid,
        "rss_mb": rss,
        "max_rss_mb": max_rss_mb,
        "thread_count": threading.active_count(),
    }


def _system_memory_proc() -> dict[str, Any] | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    vals: dict[str, float] = {}
    try:
        for line in meminfo.read_text().splitlines():
            if ":" not in line:
                continue
            k, rest = line.split(":", 1)
            m = re.search(r"(\d+)", rest)
            if m:
                vals[k] = float(m.group(1)) / 1024.0  # KiB -> MB
    except Exception:
        return None
    total = vals.get("MemTotal")
    available = vals.get("MemAvailable", vals.get("MemFree"))
    if not total or available is None:
        return None
    used_percent = max(0.0, min(100.0, (1.0 - (available / total)) * 100.0))
    return {"total_mb": round(total, 1), "available_mb": round(available, 1), "used_percent": round(used_percent, 1), "source": "/proc/meminfo"}


def _system_memory_vm_stat() -> dict[str, Any] | None:
    proc = _run(["vm_stat"], timeout=2.0)
    if not proc or proc.returncode != 0:
        return None
    page_size = 4096
    m = re.search(r"page size of (\d+) bytes", proc.stdout or "")
    if m:
        page_size = int(m.group(1))
    vals: dict[str, int] = {}
    for line in (proc.stdout or "").splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        n = re.sub(r"[^0-9]", "", val)
        if n:
            vals[key.strip()] = int(n)
    free_pages = vals.get("Pages free", 0) + vals.get("Pages inactive", 0) + vals.get("Pages speculative", 0)
    used_pages = sum(v for k, v in vals.items() if k.startswith("Pages active") or k.startswith("Pages wired") or k.startswith("Pages occupied by compressor"))
    total_pages = free_pages + used_pages
    if total_pages <= 0:
        return None
    total_mb = total_pages * page_size / (1024 * 1024)
    available_mb = free_pages * page_size / (1024 * 1024)
    used_percent = (1.0 - available_mb / total_mb) * 100.0 if total_mb else 0.0
    return {"total_mb": round(total_mb, 1), "available_mb": round(available_mb, 1), "used_percent": round(used_percent, 1), "source": "vm_stat"}


def system_memory_snapshot() -> dict[str, Any]:
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        return {
            "total_mb": round(float(vm.total) / (1024 * 1024), 1),
            "available_mb": round(float(vm.available) / (1024 * 1024), 1),
            "used_percent": round(float(vm.percent), 1),
            "source": "psutil",
        }
    except Exception:
        pass
    return _system_memory_proc() or _system_memory_vm_stat() or {"available_mb": None, "used_percent": None, "source": "unknown"}


def process_table(names: tuple[str, ...] = ("mongoose", "ollama"), *, limit: int = 20) -> list[dict[str, Any]]:
    """Small process snapshot for owner diagnostics. No command output beyond one-line process command."""
    rows: list[dict[str, Any]] = []
    proc = _run(["ps", "-axo", "pid=,ppid=,rss=,pcpu=,etime=,command="], timeout=2.0)
    if not proc or proc.returncode != 0:
        return rows
    needles = tuple(n.lower() for n in names)
    for line in (proc.stdout or "").splitlines():
        low = line.lower()
        if not any(n in low for n in needles):
            continue
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0]); ppid = int(parts[1]); rss_mb = round(float(parts[2]) / 1024.0, 1); pcpu = float(parts[3])
        except Exception:
            continue
        cmd = parts[5]
        rows.append({"pid": pid, "ppid": ppid, "rss_mb": rss_mb, "cpu_percent": pcpu, "etime": parts[4], "command": cmd[:220]})
    rows.sort(key=lambda r: float(r.get("rss_mb") or 0), reverse=True)
    return rows[:limit]


def loop_and_scheduler_snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        import json as _json
        from ..db import get_app_state
        raw = get_app_state("runtime_watchdog")
        if raw:
            out["runtime_watchdog"] = _json.loads(raw)
        hb = get_app_state("scheduler_heartbeat")
        if hb:
            # heartbeat may be an epoch from older versions or ISO from current scheduler.
            try:
                age = max(0.0, time.time() - float(hb))
            except Exception:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age = max(0.0, time.time() - dt.timestamp())
            out["scheduler_heartbeat_age_s"] = round(age, 1)
    except Exception:
        pass
    return out


def assess_runtime_pressure(*, include_process_table: bool = False) -> dict[str, Any]:
    th = thresholds()
    proc = current_process_snapshot()
    mem = system_memory_snapshot()
    loop = loop_and_scheduler_snapshot()
    procs = process_table(("ollama", "mongoose"), limit=12) if include_process_table else []

    warnings: list[str] = []
    level = "green"

    rss = proc.get("rss_mb")
    if isinstance(rss, (int, float)) and rss >= th.python_rss_warn_mb:
        warnings.append(f"mongoose Python RSS is high ({rss} MB >= {th.python_rss_warn_mb:g} MB)")
        level = "amber"
    avail = mem.get("available_mb")
    used = mem.get("used_percent")
    if isinstance(avail, (int, float)) and avail <= th.system_available_warn_mb:
        warnings.append(f"system available memory is low ({avail} MB <= {th.system_available_warn_mb:g} MB)")
        level = "red"
    if isinstance(used, (int, float)) and used >= th.system_used_warn_percent:
        warnings.append(f"system memory use is high ({used}% >= {th.system_used_warn_percent:g}%)")
        level = "red"
    for p in procs:
        cmd = str(p.get("command") or "").lower()
        if "ollama" in cmd and isinstance(p.get("rss_mb"), (int, float)) and p["rss_mb"] >= th.ollama_rss_warn_mb:
            warnings.append(f"Ollama process RSS is very high ({p['rss_mb']} MB >= {th.ollama_rss_warn_mb:g} MB)")
            if level == "green":
                level = "amber"
    rw = loop.get("runtime_watchdog") or {}
    lag = rw.get("loop_lag_ms") if isinstance(rw, dict) else None
    if isinstance(lag, (int, float)) and lag >= th.loop_lag_warn_ms:
        warnings.append(f"event-loop lag is high ({lag} ms >= {th.loop_lag_warn_ms:g} ms)")
        level = "red"
    sage = loop.get("scheduler_heartbeat_age_s")
    if isinstance(sage, (int, float)) and sage >= th.scheduler_stale_warn_s:
        warnings.append(f"scheduler heartbeat is stale ({sage}s >= {th.scheduler_stale_warn_s:g}s)")
        if level == "green":
            level = "amber"

    actions: list[str] = []
    if warnings:
        actions.append("pause deferrable scheduler/model jobs until pressure drops")
    if level == "red":
        actions.append("avoid starting benchmark/build/model-heavy work")
    if isinstance(rss, (int, float)) and rss >= th.python_rss_restart_mb:
        actions.append("external watchdog may restart the Mongoose server if configured threshold is exceeded")

    return {
        "ok": level == "green",
        "level": level,
        "warnings": warnings,
        "recommended_actions": actions,
        "thresholds": th.__dict__,
        "current_process": proc,
        "system_memory": mem,
        "loop": loop,
        "processes": procs,
    }


def should_skip_deferrable_work(snapshot: dict[str, Any] | None = None) -> bool:
    snap = snapshot or assess_runtime_pressure(include_process_table=False)
    return str(snap.get("level")) in {"amber", "red"} and bool(snap.get("warnings"))


def server_rss_exceeds_restart_threshold(pid: int) -> dict[str, Any]:
    th = thresholds()
    rss = rss_mb_for_pid(pid)
    exceeded = isinstance(rss, (int, float)) and th.python_rss_restart_mb > 0 and rss >= th.python_rss_restart_mb
    return {"pid": pid, "rss_mb": rss, "threshold_mb": th.python_rss_restart_mb, "exceeded": bool(exceeded)}
