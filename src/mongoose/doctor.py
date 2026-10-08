from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from .settings import get_settings


def _status(ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": ok, "detail": detail}
    payload.update(extra)
    return payload


def run_doctor(timeout_seconds: float = 2.0) -> dict[str, Any]:
    """Non-destructive readiness report for a local Mongoose install.

    This does not send emails, control Home Assistant, read private content, or
    call paid APIs. It only checks local configuration and service reachability.
    """
    settings = get_settings()
    checks: dict[str, dict[str, Any]] = {}

    db_parent = settings.db_path.parent
    checks["database_path"] = _status(
        db_parent.exists() or db_parent.parent.exists(),
        str(settings.db_path),
        path=str(settings.db_path),
    )

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            r = client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            data = r.json()
        names = {m.get("name", "") for m in data.get("models", [])}

        def _model_present(want: str) -> bool:
            """Ollama resolves model names flexibly (a bare 'gemma3:27b' can be served by a pulled
            'gemma3:27b-it-q4_K_M', and ':latest' is implicit). An exact set-membership test reports
            a perfectly working model as 'missing' whenever tags differ, which falsely drove
            'core not ready' while chat worked. Match the way Ollama does: exact, or by base repo,
            or by prefix either direction, ignoring an implicit ':latest'."""
            if not want:
                return False
            if want in names:
                return True
            w = want[:-7] if want.endswith(":latest") else want
            w_base = w.split(":", 1)[0]
            for n in names:
                nn = n[:-7] if n.endswith(":latest") else n
                if nn == w:
                    return True
                # base repo matches (e.g. want 'gemma3' served by 'gemma3:27b-it-q4_K_M')
                if nn.split(":", 1)[0] == w_base and (":" not in w or nn.startswith(w) or w.startswith(nn)):
                    return True
            return False

        configured = {
            "chat": settings.ollama_chat_model,
            "tool": settings.ollama_tool_model,
            "code": settings.ollama_code_model,
        }
        present = {role: _model_present(model) for role, model in configured.items()}
        required_present = present["chat"] and present["tool"]
        checks["ollama"] = _status(
            required_present,
            "Ollama reachable; chat/tool models present" if required_present else "Ollama reachable; pull missing role models",
            base_url=settings.ollama_base_url,
            configured=configured,
            present=present,
            installed_count=len(names),
        )
    except Exception as exc:
        checks["ollama"] = _status(False, "Ollama not reachable", base_url=settings.ollama_base_url, error=str(exc))

    checks["weather"] = _status(
        bool(settings.home_lat and settings.home_lon),
        "home coordinates configured; Open-Meteo fallback needs no API key",
        provider_order=settings.provider_names,
        has_tomorrow_key=bool(settings.tomorrow_api_key),
        has_metoffice_key=bool(settings.metoffice_api_key),
        has_weatherkit=bool(settings.weatherkit_team_id and settings.weatherkit_key_id),
    )

    checks["home_assistant"] = _status(
        bool(settings.ha_url and settings.ha_token),
        "configured" if settings.ha_url and settings.ha_token else "not configured; home control will not reach HA",
        url_configured=bool(settings.ha_url),
        token_configured=bool(settings.ha_token),
    )

    checks["web_search"] = _status(
        bool(getattr(settings, "searxng_url", "")),
        ("configured (SearXNG; research worker uses it when local is thin)"
         if getattr(settings, "searxng_url", "") else
         "not configured; research worker is local-only (set MONGOOSE_SEARXNG_URL to enable web)"),
        url_configured=bool(getattr(settings, "searxng_url", "")),
    )

    # FIX (three screens disagreeing about the SAME fact): this used to independently
    # re-derive "is https ready" locally. setup_state.py is now the ONE canonical
    # implementation every surface reads (Overview, cockpit's TLS panel, this doctor check),
    # so they can never drift into disagreeing answers again the way Overview's old
    # "not configured, run the manual cert script" text once did next to a working live certificate.
    try:
        from .setup_state import setup_state as _setup_state
        _https_surf = _setup_state(only=["https"])["surfaces"]["https"]
        _https_detail = _https_surf["detail"]
        if not _https_surf["ok"] and _https_surf.get("next_action"):
            _https_detail += f". One click, no terminal: {_https_surf['next_action']}."
        checks["https"] = _status(_https_surf["ok"], _https_detail,
                                  **_https_surf.get("facts", {}))
    except Exception as exc:
        checks["https"] = _status(False, f"https check error: {exc}")

    checks["alexa_bridge"] = _status(
        bool(settings.alexa_fallback_url and settings.alexa_fallback_token),
        "configured" if settings.alexa_fallback_url and settings.alexa_fallback_token else "not configured; Alexa fallback intentionally disabled",
        url_configured=bool(settings.alexa_fallback_url),
        token_configured=bool(settings.alexa_fallback_token),
    )

    checks["cloud_escalation"] = _status(
        bool(settings.cloud_escalation_enabled),
        "enabled" if settings.cloud_escalation_enabled else "disabled by default",
        provider_order=settings.cloud_provider_names,
        google=bool(settings.google_api_key),
        anthropic=bool(settings.anthropic_api_key),
        openai=bool(settings.openai_api_key),
        models={
            "google": settings.google_instant_model,
            "anthropic": settings.anthropic_instant_model,
            "openai": settings.openai_instant_model,
        },
        budget_pennies=settings.cloud_turn_budget_pennies,
    )

    checks["connectors"] = _status(
        True,
        "read-first connectors are optional; missing credentials are expected in beta",
        imap=bool(settings.imap_host and settings.imap_username and settings.imap_password),
        google_files_exist=Path(settings.google_client_secrets_path).exists(),
        microsoft=bool(settings.ms_client_id),
        ntfy=bool(settings.ntfy_url and settings.ntfy_topic),
    )

    # Bus: can we reach the bus index? (the coordination + brief substrate)
    try:
        from .services import bus as _bus
        _bus.list_messages(limit=1)
        checks["bus"] = _status(True, "message bus reachable",
                                 owner_id=settings.owner_id)
    except Exception as exc:
        checks["bus"] = _status(False, "bus not reachable", error=str(exc))

    # Builder: is the overnight build-runner configured? (claude CLI present?)
    import shutil as _shutil
    code_path = _shutil.which(settings.claude_code_cmd) if settings.claude_code_cmd else None
    # Billing trap: with ANTHROPIC_API_KEY set, Claude Code bills at API rates instead of using the
    # subscription. Surface it here so it's caught before it shows up on an invoice.
    import os as _os_dr
    _api_key_set = bool(_os_dr.getenv("ANTHROPIC_API_KEY", "").strip())
    _builder_detail = (
        "claude code CLI found" if code_path else
        "claude code CLI not found; overnight builds will run local-draft-only. To enable real builds: "
        "install it with  npm install -g @anthropic-ai/claude-code  (needs Node.js), or reuse the "
        "Claude Code already on the HA Pi, point builds at the Pi over Protocol V3 (Setup → Protocol V3 "
        "→ Pi host) so overnight work runs there instead of local-draft-only.")
    if code_path and _api_key_set:
        _builder_detail += (": WARNING: ANTHROPIC_API_KEY is set, so Claude Code bills at API rates "
                            "instead of your subscription. Unset it to use your plan.")
    checks["builder"] = _status(
        bool(code_path),
        _builder_detail,
        claude_code_cmd=settings.claude_code_cmd,
        resolved_path=code_path,
        anthropic_api_key_in_env=_api_key_set,
        code_call_budget=settings.build_code_call_budget,
    )

    # Voice: is the spoken-input toolchain present? (rec + whisper + ffmpeg)
    import shutil as _sh
    rec = _sh.which("rec")
    whisper = (_sh.which("whisper") or _sh.which("whisper-cli") or _sh.which("whisper-cpp"))
    ffmpeg = _sh.which("ffmpeg")
    voice_ok = bool(rec and whisper and ffmpeg)
    voice_missing = [name for name, present in
                     (("sox/rec", rec), ("whisper", whisper), ("ffmpeg", ffmpeg)) if not present]
    checks["voice"] = _status(
        voice_ok,
        "voice input ready (rec + whisper + ffmpeg)" if voice_ok else
        "voice OUTPUT works (say); mic input needs: " + ", ".join(voice_missing),
        rec=bool(rec), whisper=bool(whisper), ffmpeg=bool(ffmpeg),
        tts=bool(_sh.which(settings.tts_cmd)),
    )

    warnings: list[str] = []
    if settings.ollama_keep_alive.strip() in {"-1"}:
        warnings.append(
            "OLLAMA_KEEP_ALIVE=-1 keeps the model resident permanently (no cold "
            "starts) but pins ~17GB RAM on a 24GB machine. Set 10m or 0 to free it."
        )
    elif settings.ollama_keep_alive not in {"0", "0s"}:
        warnings.append(
            f"OLLAMA_KEEP_ALIVE={settings.ollama_keep_alive} keeps a model warm for speed; "
            "if you hit memory thrash on 24GB during chat/tool switches, set it to 0."
        )
    if settings.ollama_tool_model.startswith("qwen3.6:27b"):
        warnings.append("Dense Qwen 27B tool model is configured; expect slow tool turns on base M4 24GB.")
    if not settings.ha_url:
        warnings.append("Home Assistant is not configured yet; home-control commands will be failure-path only.")

    # Readiness breakdown: "core OK" is not the same as "ready for unattended
    # builds / HA / cloud". Surface each capability's readiness separately so the
    # user knows what they can actually trust right now.
    # File and attachment indexes: counts + devices present, so "is my stuff
    # indexed?" is answerable, and the attachment content-extraction follow-up's
    # progress is visible.
    try:
        from .services.attachments import attachment_stats
        from .db import connect as _connect
        astats = attachment_stats()
        with _connect() as _con:
            file_total = _con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            devices = [r[0] for r in _con.execute("SELECT DISTINCT device FROM files").fetchall()]
        checks["search_index"] = _status(
            True,
            f"{file_total} file(s) across {len(devices)} device(s); "
            f"{astats['total']} attachment(s) ({astats['content_indexed']} with content)",
            files=file_total, devices=devices,
            attachments=astats["total"], attachments_with_content=astats["content_indexed"],
        )
        # Is content extraction (search INSIDE pdf/docx) available?
        import importlib.util as _ilu
        pdf_ok = _ilu.find_spec("pypdfium2") is not None
        docx_ok = _ilu.find_spec("docx") is not None
        checks["content_extraction"] = _status(
            pdf_ok and docx_ok,
            "PDF + Word content search available" if (pdf_ok and docx_ok)
            else "filename search only; install pypdfium2 and python-docx for content search",
            pdf=pdf_ok, docx=docx_ok,
        )

        # Outbound push (Mongoose -> phone via ntfy/web-push) and the background scheduler.
        # CONSOLIDATION: push_to_phone and notifications (below) used to be two independently
        # computed copies of the SAME fact in this same file, which only "agreed" by
        # coincidence of near-identical copy-pasted logic. Both now delegate to setup_state's
        # one canonical notifications surface (chain-aware: if the phone itself isn't
        # reachable yet, THAT is the blocker, not "0 subscriptions").
        try:
            from .setup_state import setup_state as _setup_state
            _notif_surf = _setup_state(only=["notifications"])["surfaces"]["notifications"]
            checks["push_to_phone"] = _status(_notif_surf["ok"], _notif_surf["detail"])
        except Exception as exc:
            checks["push_to_phone"] = _status(False, f"push check error: {exc}")

        try:
            from .services.scheduler import job_status
            jobs = job_status()
            ran = [j for j in jobs if j.get("last_run")]
            checks["scheduler"] = _status(
                True,
                f"{len(jobs)} job(s) registered, {len(ran)} have run; "
                "start with `mongoose scheduler`",
                jobs=jobs,
            )
        except Exception as exc:
            checks["scheduler"] = _status(False, f"scheduler check failed: {exc}")

        # Capability readiness: does each feature have its dependencies installed?
        try:
            from .services.capabilities import capabilities_report
            cap = capabilities_report()
            not_ready = cap["missing"]
            checks["capabilities"] = _status(
                not not_ready,
                "all capabilities have their dependencies"
                if not not_ready
                else "missing libraries for: "
                     + ", ".join(m["capability"] for m in not_ready)
                     + " (fix with `mongoose install-missing` or `mongoose setup --capability <name>`)",
                ready=cap["ready"], missing=not_ready,
            )
        except Exception as exc:
            checks["capabilities"] = _status(False, f"capability check failed: {exc}")

        # WhatsApp session readiness: only if a provider is configured. Reports the
        # session state (SCAN_QR_CODE / WORKING / ...), not just 'container up'.
        import os as _os
        if _os.getenv("WHATSAPP_PROVIDER") or _os.getenv("WAHA_URL"):
            try:
                from .services.whatsapp_providers.base import get_provider
                p = get_provider()
                st = p.status() if p else {"ok": False, "error": "no provider"}
                state = st.get("state", "unknown")
                checks["whatsapp"] = _status(
                    st.get("ok") and state in ("WORKING", "working"),
                    f"WhatsApp session: {state}" if st.get("ok")
                    else f"WhatsApp not reachable: {st.get('error')}",
                    state=state)
            except Exception as exc:
                checks["whatsapp"] = _status(False, f"WhatsApp check failed: {exc}")
    except Exception as exc:
        checks["search_index"] = _status(False, f"index check failed: {exc}")

    # ── Machine health: the underlooked failure modes of an always-on install ─────────
    # Disk space: the Mac fills up quietly (backups, dumps, models); Mongoose should say so
    # before SQLite starts failing writes.
    try:
        import shutil as _shutil
        du = _shutil.disk_usage(str(settings.db_path.parent))
        free_gb = du.free / (1024 ** 3)
        # Sanity-cap: containers and overlay filesystems can report absurd free space (the
        # external audit saw billions of GB). A diagnostic panel is a trust surface; an
        # impossible number makes the user doubt every other line, so say "can't measure"
        # instead. 100 TB is beyond any Mac this will run on.
        if free_gb > 100_000 or free_gb < 0:
            checks["disk_space"] = _status(True,
                                           "free space not reliably measurable in this "
                                           "environment (filesystem reports an implausible "
                                           "figure)", free_gb=None)
        else:
            checks["disk_space"] = _status(free_gb >= 5.0,
                                           f"{free_gb:.1f} GB free on the data volume"
                                           + ("" if free_gb >= 5.0 else " (low; below 5 GB)"),
                                           free_gb=round(free_gb, 1))
    except Exception as exc:
        checks["disk_space"] = _status(False, f"disk check failed: {exc}")

    # Database integrity + size + dead-space fraction (retention/VACUUM handled nightly).
    try:
        from .services.db_maintenance import db_health
        h = db_health()
        qc_ok = h.get("quick_check") == "ok"
        mb = h.get("db_bytes", 0) / (1024 ** 2)
        checks["database_health"] = _status(qc_ok,
                                            f"quick_check={h.get('quick_check')}, {mb:.1f} MB, "
                                            f"freelist {h.get('freelist_fraction', 0):.0%}",
                                            **h)
    except Exception as exc:
        checks["database_health"] = _status(False, f"integrity check failed: {exc}")

    # Self-signed TLS expiry: the phone cockpit dies hard on an expired cert. The weekly
    # tls_renewal job regenerates within 30 days of expiry; surface the horizon here.
    try:
        from .self_signed_tls import days_until_expiry, self_signed_status
        st = self_signed_status()
        if st.get("present"):
            d = days_until_expiry()
            checks["tls_certificate"] = _status(bool(d is not None and d > 14),
                                                (f"expires in {d} days" if d is not None else "expiry unreadable")
                                                + ("" if (d or 0) > 14 else "; renewal job should fire, or run: mongoose tls self-signed"),
                                                days_remaining=d, expires=st.get("expires"))
        else:
            checks["tls_certificate"] = _status(True, "no self-signed cert (http-only or other TLS in use)")
    except Exception as exc:
        checks["tls_certificate"] = _status(False, f"tls check failed: {exc}")

    # Backup freshness: enabled backups that silently stopped are worse than no backups,
    # because they retire the worry without the protection.
    try:
        import os as _os
        if str(_os.environ.get("MONGOOSE_BACKUP_ENABLED", "")).strip().lower() in ("1", "true", "yes"):
            bdir = settings.db_path.parent / "backups"
            newest = max((f.stat().st_mtime for f in bdir.glob("*") if f.is_file()), default=0.0)
            import time as _time
            age_h = (_time.time() - newest) / 3600 if newest else None
            fresh = age_h is not None and age_h < 48
            checks["backup_freshness"] = _status(fresh,
                                                 (f"newest backup {age_h:.0f}h old" if age_h is not None
                                                  else "backups enabled but none found yet"),
                                                 age_hours=None if age_h is None else round(age_h, 1))
        else:
            checks["backup_freshness"] = _status(True, "backups not enabled (MONGOOSE_BACKUP_ENABLED)")
    except Exception as exc:
        checks["backup_freshness"] = _status(False, f"backup check failed: {exc}")

    # Brain location: is this server running against the ONE canonical brain? The real
    # computation now lives in setup_state.py (shared with /cockpit/setup-state and
    # Overview), so this and every other surface reading it can never disagree again.
    try:
        from .setup_state import _brain_location_facts
        _ok, _detail = _brain_location_facts()
        checks["brain_location"] = _status(_ok, _detail)
    except Exception as exc:
        checks["brain_location"] = _status(False, f"brain location check failed: {exc}")

    # Conversation census: every conversation id in the messages table, counted. The
    # disappeared-chats incident was invisible precisely because nothing listed which
    # conversation ids exist: messages written into an unexpected thread are safe in the DB
    # but absent from every history view, which reads as data loss. Expected shapes: the one
    # shared brain, person-keyed guest threads (guest:p:<name>), and legacy device-keyed guest
    # threads (guest:<device_id>, migrated forward on next use). Anything else is flagged.
    try:
        from .db import connect as _connect
        from .services.memory import DEFAULT_CONVERSATION_ID as _DCID
        with _connect() as _con:
            _rows = _con.execute(
                "SELECT conversation_id, COUNT(*) AS n FROM messages "
                "GROUP BY conversation_id ORDER BY n DESC").fetchall()
        _parts, _unexpected = [], []
        for _r in _rows:
            _cid, _n = _r["conversation_id"], _r["n"]
            _parts.append(f"{_cid}={_n}")
            if _cid != _DCID and not _cid.startswith("guest:") and not _cid.startswith("group:"):
                _unexpected.append(_cid)
        if not _rows:
            checks["conversations"] = _status(True, "no messages stored yet")
        elif _unexpected:
            checks["conversations"] = _status(
                False,
                f"UNEXPECTED conversation id(s) {', '.join(_unexpected)}, messages there are "
                f"safe in the database but invisible to every history view. Census: {'; '.join(_parts)}")
        else:
            checks["conversations"] = _status(True, "; ".join(_parts))
    except Exception as exc:
        checks["conversations"] = _status(False, f"conversation census failed: {exc}")

    # Public-facing chain summary (full detail lives at /remote/readiness and `mongoose remote`).
    try:
        from .services.remote_access import is_public_facing, public_base_url, remote_readiness
        if public_base_url() or is_public_facing():
            rr = remote_readiness(probe_outside=False)
            if rr["ok"]:
                detail = f"{rr.get('public_url') or 'no canonical URL'}"
            else:
                # Name the specific checks AND where to actually act on them, so this reads as
                # "here's the fix" rather than internal check names dropped with no next step.
                detail = (f"{rr.get('public_url') or 'no canonical URL'}; failing: "
                          f"{', '.join(rr['failing'])}. See Setup -> Remote access for what "
                          "each of these means and how to fix it.")
            checks["remote_access"] = _status(rr["ok"], detail, failing=rr["failing"])
        else:
            checks["remote_access"] = _status(True, "not configured for public access")
    except Exception as exc:
        checks["remote_access"] = _status(False, f"remote access check failed: {exc}")

    readiness = {
        "core": checks["database_path"]["ok"] and checks["ollama"]["ok"],
        "home_assistant": checks["home_assistant"]["ok"],
        "overnight_builds": checks.get("builder", {}).get("ok", False),
        "cloud_escalation": checks["cloud_escalation"]["ok"],
    }

    # "Ask Mongoose" surfaces. HA Assist forwards to /assist over the LAN, so it
    # needs remote access ON (bind 0.0.0.0 + ALLOW_REMOTE + token). Claude reaches
    # the local MCP server over stdio. Alexa INPUT is not supported (a cloud skill
    # would break local-first); Alexa is an output fallback only.
    remote_ready = bool(getattr(settings, "allow_remote", False)
                        and getattr(settings, "api_token", ""))
    binds_lan = str(getattr(settings, "host", "127.0.0.1")) in ("0.0.0.0", "::")
    ask_mongoose = {
        "home_assistant_assist": {
            "ready": remote_ready and binds_lan,
            "detail": ("HA can reach /assist over the LAN" if (remote_ready and binds_lan)
                       else "needs: MONGOOSE_HOST=0.0.0.0, MONGOOSE_ALLOW_REMOTE=true, "
                            "MONGOOSE_API_TOKEN set, and the same token in the HA component"),
        },
        "claude_mcp": {
            "ready": True,
            "detail": "register the local MCP server in Claude Desktop/Code (docs/MCP_SETUP.md); "
                      "stdio, no network exposure",
        },
        "alexa_ask": {
            "ready": False,
            "detail": "not supported: 'Alexa, ask Mongoose' needs a cloud skill (breaks "
                      "local-first). Alexa is an output fallback only.",
        },
    }

    # Notifications, END TO END: the owner reported never receiving one. The chain is
    # VAPID keypair -> phone grants permission -> PWA posts /push/subscribe -> events call
    # send_to_phone. Zero subscriptions means every send silently returns
    # "no_subscriptions", which is exactly what "it never worked" looks like. Say so, with
    # the fix. Also flag the guessable default ntfy topic (a privacy hole AND a reason
    # ntfy deliveries go nowhere the owner is subscribed).
    # Notifications: delegates to the same setup_state surface push_to_phone now uses, so the
    # two keys in this same doctor output can never disagree again.
    try:
        from .setup_state import setup_state as _setup_state
        _notif_surf2 = _setup_state(only=["notifications"])["surfaces"]["notifications"]
        checks["notifications"] = _status(_notif_surf2["ok"], _notif_surf2["detail"],
                                          hint=_notif_surf2.get("next_action") or "")
    except Exception as exc:
        checks["notifications"] = _status(False, f"notifications check error: {exc}")

    # Embeddings: the fuzzy-matching model. Delegates to setup_state's canonical surface,
    # which wraps embeddings.available() directly (the source of the old "multilingual
    # embeddings ?" bug was reading a key that function never returns).
    try:
        from .setup_state import setup_state as _setup_state
        _emb_surf = _setup_state(only=["embeddings"])["surfaces"]["embeddings"]
        checks["embeddings"] = _status(_emb_surf["ok"], _emb_surf["detail"])
    except Exception as exc:
        checks["embeddings"] = _status(False, f"embeddings check error: {exc}")

    return {
        "ok": checks["database_path"]["ok"] and checks["weather"]["ok"],
        "checks": checks,
        "readiness": readiness,
        "ask_mongoose": ask_mongoose,
        "warnings": warnings,
        "env_file_present": Path(".env").exists(),
        "cwd": os.getcwd(),
    }


def system_status() -> dict[str, Any]:
    """Backward-compatible concise status snapshot used by /system/status."""
    report = run_doctor(timeout_seconds=0.5)
    checks = report["checks"]
    return {
        "mongoose": {
            "ok": report["ok"],
            "warnings": report["warnings"],
            "cwd": report["cwd"],
        },
        "models": checks["ollama"],
        "home": {
            "home_assistant_configured": checks["home_assistant"]["ok"],
            "alexa_bridge_configured": checks["alexa_bridge"]["ok"],
        },
        "weather": checks["weather"],
        "connectors": checks["connectors"],
        "cloud": checks["cloud_escalation"],
    }
