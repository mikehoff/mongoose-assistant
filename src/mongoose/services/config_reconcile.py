"""Config-vs-reality reconciliation.

The installer/updater audit (2026-07-17) found the systemic fault behind a string of broken settings:
nothing ever checks that CONFIGURED things actually match the MACHINE. A role can point at a model
that was never pulled (code -> qwen2.5-coder), a feature can be enabled without its dependency
(voicemail transcription with no whisper), a transport can be wired but its feature left off. The
doctor reports health but nothing ranks the ACTIONABLE config drift or says how to fix it, and the
updater never runs any of it, so bad config survives every update silently.

This module is that missing step: reconcile() returns the config drift, ranked, each with a concrete
fix. It is surfaced at startup (a log line), via a cockpit endpoint, and is meant to be run by the
updater so an update can never quietly leave a role pointing at a model you don't have.
"""
from __future__ import annotations

from typing import Any


def _installed_models() -> set[str]:
    try:
        import asyncio
        from ..benchmark import installed_models
        return set(asyncio.run(installed_models()))
    except Exception:
        return set()


def reconcile() -> dict[str, Any]:
    """Return ranked config-vs-reality issues, each with a concrete fix. severity: high|medium|low."""
    from ..settings import get_settings
    s = get_settings()
    issues: list[dict[str, Any]] = []

    # 1. Model roles pointing at a model that is not installed (the code=qwen2.5-coder class of bug).
    have = _installed_models()
    if have:  # only judge when we could actually list models
        chat = getattr(s, "ollama_chat_model", "") or ""
        for role, model in (("chat", chat),
                            ("tool", getattr(s, "ollama_tool_model", "") or ""),
                            ("code", getattr(s, "ollama_code_model", "") or ""),
                            ("research", getattr(s, "ollama_research_model", "") or "")):
            if model and model not in have:
                fix = (f"set OLLAMA_{role.upper()}_MODEL to an installed model "
                       f"(e.g. {chat}) or `ollama pull {model}`")
                issues.append({"key": f"model:{role}", "severity": "high",
                               "detail": f"{role} role points at {model}, which is NOT installed",
                               "fix": fix})

    # 2. Voicemail transcriber configured-but-missing (a shipped feature with no dependency).
    try:
        from .model_install import transcriber_status
        ts = transcriber_status()
        if not ts.get("ready"):
            issues.append({"key": "transcriber", "severity": "medium",
                           "detail": ts.get("detail") or "voicemail transcriber not installed",
                           "fix": "POST /cockpit/voice/transcriber/install (installs whisper.cpp + model)"})
    except Exception:
        pass

    # 3. Protocol v3 enabled but the Pi transport is unreachable (or vice-versa: transport wired, off).
    try:
        enabled = bool(getattr(s, "protocol_v3_enabled", False))
        host = (getattr(s, "pi_ssh_host", "") or "").strip()
        if enabled:
            from .protocol_v3_runtime import transport_status
            if not transport_status().get("reachable"):
                issues.append({"key": "v3:transport", "severity": "medium",
                               "detail": "protocol v3 is enabled but the Pi transport is unreachable",
                               "fix": "check PI_SSH_HOST/PI_SSH_PASSWORD, or disable PROTOCOL_V3_ENABLED"})
        elif host:
            issues.append({"key": "v3:off", "severity": "low",
                           "detail": "Pi transport is configured but protocol v3 is disabled (peer bus silent)",
                           "fix": "set PROTOCOL_V3_ENABLED=true to join the peer bus"})
    except Exception:
        pass

    # 4. keep_alive=-1 (pin forever) vs total RAM: sane if the chat model fits with headroom, risky if tight.
    try:
        import os as _os
        ka = (getattr(s, "ollama_keep_alive", "") or "").strip()
        if ka in ("-1",):
            total_gb = 0
            try:
                total_gb = int(_os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES") / (1024 ** 3))
            except Exception:
                pass
            if total_gb and total_gb <= 24:
                issues.append({"key": "keep_alive", "severity": "low",
                               "detail": f"OLLAMA_KEEP_ALIVE=-1 pins the model on a {total_gb}GB machine "
                                         f"(intended: keep the main model hot)",
                               "fix": "keep as-is if you shed other memory (browsers/Spotlight/Docker); "
                                      "the memory guard helps. Or bound it (e.g. 10m) to auto-unload."})
    except Exception:
        pass

    # 5. Web search backend (SearXNG) configured but UNREACHABLE. This is the class M hit: web search
    # can be wired yet the local SearXNG container is down (common here, since Docker is shed to save RAM),
    # so chat silently loses web lookup. Probe the real /search json endpoint (also catches the documented
    # 403-json-not-enabled failure). A refused connection returns fast; the timeout only bites if the host
    # hangs.
    try:
        from ..web_search import configured_url, _fallback_enabled
        url = configured_url()
        fallback = _fallback_enabled()
        searxng_ok = False
        if url:
            try:
                import httpx
                r = httpx.get(url.rstrip("/") + "/search",
                              params={"q": "ping", "format": "json"}, timeout=3.0)
                searxng_ok = (r.status_code == 200)
            except Exception:
                searxng_ok = False
        if not searxng_ok and not fallback:
            # Web search is genuinely dead: no reachable SearXNG and the native fallback is switched off.
            issues.append({"key": "web_search", "severity": "medium",
                           "detail": ("web search cannot answer: " + (f"SearXNG ({url}) is unreachable"
                                      if url else "no SearXNG configured") + " and the native fallback is off"),
                           "fix": "start Docker + `mongoose start-containers` (SearXNG), or set "
                                  "MONGOOSE_WEB_SEARCH_FALLBACK=true to allow the DuckDuckGo fallback"})
        elif not searxng_ok and fallback:
            # Still works, but via DuckDuckGo (queries leave the machine) instead of private local search.
            issues.append({"key": "web_search:fallback", "severity": "low",
                           "detail": ("web search is using the DuckDuckGo fallback (queries leave your "
                                      "machine); " + (f"SearXNG ({url}) is not answering"
                                      if url else "no local SearXNG configured")),
                           "fix": "start Docker + `mongoose start-containers` to restore private local "
                                  "SearXNG search, or ignore if the fallback is acceptable"})
    except Exception:
        pass

    # 6. LibreOffice (soffice) missing: the RTM demand + receipt PDF engine cannot render (high-value RTM
    # feature that would fail only at generation time, i.e. another silent DOA).
    try:
        import shutil
        from pathlib import Path as _P
        soffice = shutil.which("soffice") or next(
            (c for c in ("/opt/homebrew/bin/soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice")
             if _P(c).exists()), "")
        if not soffice:
            issues.append({"key": "soffice", "severity": "medium",
                           "detail": "LibreOffice (soffice) not found: RTM demand and receipt PDFs cannot be rendered",
                           "fix": "brew install --cask libreoffice"})
    except Exception:
        pass

    # 7. Swift missing: fortune cookie cards and the Swift OCR/vision path cannot render colour-emoji PDFs.
    try:
        from .fortune_cookie import _swift
        if not _swift():
            issues.append({"key": "swift", "severity": "low",
                           "detail": "Swift not found: fortune cookie cards and Swift OCR cannot render",
                           "fix": "xcode-select --install (Xcode Command Line Tools)"})
    except Exception:
        pass

    high = [i for i in issues if i["severity"] == "high"]
    return {"ok": not high, "issues": issues, "count": len(issues),
            "high": len(high), "summary": _summarize(issues)}


def _summarize(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "config matches the machine"
    parts = [f"{i['key']}({i['severity']})" for i in issues]
    return "config drift: " + ", ".join(parts)
