"""Memory guard, keep the main model hot, shed the OTHER fat under pressure.

the owner's explicit strategy (see memory m-keep-main-model-pinned): the primary LLM stays pinned
(OLLAMA_KEEP_ALIVE=-1). When RAM gets tight we do NOT evict it; we reclaim from everything else.
This module is the piece that makes that hold:

  1. PROTECT the pinned chat model, never evicted here.
  2. EVICT any SECONDARY Ollama model that is resident (code/research/tool if it differs from chat).
     Models are mutually exclusive in memory, so a stray second model is pure waste.
  3. SURFACE the external hogs the server can see but must not silently kill, browsers, Spotlight,
     Docker, with the exact reclaim action, so M (or a one-tap cockpit control) can act.

The scheduler already pauses heavy/proactive jobs under pressure (see scheduler _HEAVY_DEFERRABLE),
so this module deliberately does NOT duplicate that. It complements it.

Everything here is best-effort and macOS-aware; it never raises into a caller.
"""
from __future__ import annotations

import subprocess
from typing import Any

from ..settings import get_settings

_OLLAMA_TIMEOUT = 6.0


def _resident_models() -> list[dict[str, Any]]:
    import httpx
    s = get_settings()
    try:
        r = httpx.get(f"{s.ollama_base_url.rstrip('/')}/api/ps", timeout=_OLLAMA_TIMEOUT)
        if r.status_code >= 400:
            return []
        return [m for m in (r.json().get("models") or []) if isinstance(m, dict)]
    except Exception:
        return []


def _evict_model(model: str) -> bool:
    """Unload one model (keep_alive:0). Returns True on a clean request."""
    import httpx
    s = get_settings()
    try:
        r = httpx.post(f"{s.ollama_base_url.rstrip('/')}/api/chat",
                       json={"model": model, "messages": [{"role": "user", "content": "x"}],
                             "stream": False, "keep_alive": 0, "think": False},
                       timeout=30)
        return r.status_code < 400
    except Exception:
        return False


def _ps_rss_mb(name_contains: str) -> float:
    """Sum RSS (MB) of processes whose command contains name_contains. Best-effort, macOS `ps`."""
    try:
        out = subprocess.run(["ps", "-Ao", "rss,comm"], capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return 0.0
    total_kb = 0
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        rss, comm = parts
        if name_contains.lower() in comm.lower():
            try:
                total_kb += int(rss)
            except ValueError:
                pass
    return round(total_kb / 1024, 0)


def _external_hogs() -> list[dict[str, Any]]:
    """Reclaimable memory the SERVER can see but must not silently kill. Each carries the exact action."""
    hogs: list[dict[str, Any]] = []
    chrome = _ps_rss_mb("Google Chrome")
    if chrome >= 300:
        hogs.append({"name": "Google Chrome", "mb": chrome, "safe_server_action": False,
                     "reclaim": "quit Chrome (tabs restore on relaunch)"})
    safari = _ps_rss_mb("Safari")
    if safari >= 400:
        hogs.append({"name": "Safari", "mb": safari, "safe_server_action": False,
                     "reclaim": "close Safari windows/tabs"})
    mds = _ps_rss_mb("/Support/mds") + _ps_rss_mb("mdworker")
    if mds >= 500:
        hogs.append({"name": "Spotlight indexing", "mb": mds, "safe_server_action": False,
                     "reclaim": "sudo mdutil -a -i off (re-enable later with -i on)"})
    docker = _ps_rss_mb("com.docker") + _ps_rss_mb("Virtualization.VirtualMachine")
    if docker >= 500:
        hogs.append({"name": "Docker VM (WAHA/SearXNG/ntfy)", "mb": docker, "safe_server_action": False,
                     "reclaim": "stop unused containers, or quit Docker Desktop if not needed"})
    return sorted(hogs, key=lambda h: -h["mb"])


def snapshot() -> dict[str, Any]:
    """Current memory picture through the guard's lens: pressure level, the protected pinned model,
    any wasteful secondary model resident, and the external hogs with their reclaim actions."""
    s = get_settings()
    protected = getattr(s, "ollama_chat_model", "") or ""
    resident = _resident_models()
    resident_names = [m.get("name") for m in resident]
    secondary = [n for n in resident_names if n and n != protected]
    try:
        from .resource_guard import assess_runtime_pressure
        pressure = assess_runtime_pressure()
        level = pressure.get("level")
    except Exception:
        level = None
    return {
        "level": level,
        "protected_model": protected,
        "protected_resident": protected in resident_names,
        "secondary_models_resident": secondary,   # pure waste, safe to evict
        "external_hogs": _external_hogs(),
    }


def apply_safe_relief() -> dict[str, Any]:
    """Take ONLY the server-safe relief actions: evict secondary Ollama models, keeping the pinned
    chat model resident. External hogs are returned as recommendations, never killed here."""
    snap = snapshot()
    evicted: list[str] = []
    for model in snap.get("secondary_models_resident", []):
        if _evict_model(model):
            evicted.append(model)
    return {
        "ok": True,
        "level": snap.get("level"),
        "protected_model": snap.get("protected_model"),
        "evicted_secondary_models": evicted,       # freed without touching the primary
        "recommendations": snap.get("external_hogs"),  # M/cockpit must action these
    }
