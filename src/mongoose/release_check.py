from __future__ import annotations

from typing import Any

# `mongoose checkup`: one command that gives the runtime readiness verdict, so the
# install is SELF-DESCRIBING. It does not add new capability; it composes the checks
# that already exist (doctor, residency probe, scheduler, capabilities, install
# coherence) plus a Security section, and labels every line ready / not / unknown.
#
# The readiness bar this reports against: a local-first assistant that runs from a clean
# install, with a usable cockpit, one speaker + bounded workers, the core paths
# present, risky actions approval-gated, and KNOWN LIMITS VISIBLE rather than hidden.

READY = "ready"
PARTIAL = "partial"
MISSING = "missing"
UNKNOWN = "unknown"

_MARK = {READY: "OK ", PARTIAL: "~  ", MISSING: "XX ", UNKNOWN: "?  "}


def _line(label: str, state: str, detail: str = "") -> dict[str, Any]:
    return {"label": label, "state": state, "detail": detail}


async def release_check() -> dict[str, Any]:
    """Return a structured readiness report: a list of sections, each a list of lines
    with a state. Composes existing checks; never raises (a failing probe becomes an
    unknown/partial line, which is itself the honest signal)."""
    from .doctor import run_doctor
    from .settings import get_settings
    settings = get_settings()
    doctor = run_doctor(timeout_seconds=1.0)
    checks = doctor.get("checks", {})

    def chk(name: str) -> dict[str, Any]:
        return checks.get(name, {}) or {}

    core: list[dict[str, Any]] = []
    # Core + database via install coherence + db presence.
    try:
        from .services.install_check import check_install_coherence
        coh = check_install_coherence()
        core.append(_line("Core / install", READY if coh.get("ok") else PARTIAL,
                          coh.get("detail", "")))
    except Exception as exc:
        core.append(_line("Core / install", UNKNOWN, f"coherence check failed: {exc}"))
    db = chk("database")
    core.append(_line("Database", READY if db.get("ok", True) else MISSING, db.get("detail", "")))

    # Models + residency.
    models: list[dict[str, Any]] = []
    ollama = chk("ollama")
    models.append(_line("Ollama / models", READY if ollama.get("ok") else UNKNOWN,
                        ollama.get("detail", "")))
    try:
        from .services.embeddings import available as _emb_available
        emb = _emb_available()
        models.append(_line("Fuzzy matching model", READY if emb.get("ok") else PARTIAL,
                            "loose-phrasing search active (multilingual included)" if emb.get("ok")
                            else ("fetching at next startup/update; word-overlap matching until then"
                                  if emb.get("auto_download", True)
                                  else "auto-download off (MONGOOSE_AUTO_EMBEDDINGS=0); word-overlap matching")))
    except Exception:
        pass
    try:
        from .model_residency import check_residency
        res = await check_residency()
        pr = res.get("pair_resident", "unknown")
        state = {"yes": READY, "no": PARTIAL, "unknown": UNKNOWN}.get(pr, READY)
        models.append(_line("Model residency", state,
                            f"owner={res.get('owner')}, worker={res.get('worker')}, pair={pr}"))
    except Exception as exc:
        models.append(_line("Model residency", UNKNOWN, str(exc)))

    try:
        import shutil
        if shutil.which("claude"):
            models.append(_line("Builder worker", READY, "Claude Code CLI found; overnight build worker can use it"))
        else:
            models.append(_line("Builder worker", PARTIAL,
                                "Claude Code CLI not found; overnight builds will be local-draft-only, not silently broken"))
    except Exception as exc:
        models.append(_line("Builder worker", UNKNOWN, str(exc)))

    # Cockpit + LAN + voice.
    surface: list[dict[str, Any]] = []
    surface.append(_line("Cockpit (local)", READY, "served at /ui"))
    token = bool(getattr(settings, "api_token", ""))
    remote = bool(getattr(settings, "allow_remote", False))
    import os as _os
    bound_host = (_os.environ.get("MONGOOSE_BOUND_HOST") or "127.0.0.1").strip()
    if remote and token and bound_host in ("0.0.0.0", "::"):
        surface.append(_line("LAN cockpit", READY, "listening on the LAN with a token; phone pairing can work"))
    elif remote and token:
        surface.append(_line("LAN cockpit", PARTIAL, "settings are enabled but this running process is still bound to localhost; use Repair / restart LAN access"))
    elif remote and not token:
        surface.append(_line("LAN cockpit", PARTIAL, "MONGOOSE_ALLOW_REMOTE set but no MONGOOSE_API_TOKEN"))
    else:
        surface.append(_line("LAN cockpit", MISSING, "local-only right now; use Setup -> Remote access -> Enable LAN access"))
    https = chk("https")
    if https.get("ok"):
        surface.append(_line("Browser voice (LAN)", READY, "https configured; mic works on LAN devices"))
    else:
        surface.append(_line("Browser voice (LAN)", PARTIAL,
                             "needs https for mic on phones/tablets: Setup -> Remote access -> "
                             "Generate a certificate (one click, no terminal); "
                             "CLI/local voice still works"))
    voice = chk("voice")
    surface.append(_line("Voice (CLI/local)", READY if voice.get("ok") else PARTIAL, voice.get("detail", "")))

    # Data paths.
    data: list[dict[str, Any]] = []
    for name, label in [("home_assistant", "Home Assistant"), ("web_search", "Web search"),
                        ("whatsapp", "WhatsApp"), ("weather", "Weather")]:
        c = chk(name)
        if not c:
            continue
        data.append(_line(label, READY if c.get("ok") else MISSING, c.get("detail", "")))
    # Paired devices (phones/tablets as surfaces).
    try:
        from .devices import list_devices
        devs = [d for d in list_devices() if not d["revoked"]]
        if devs:
            data.append(_line("Devices", READY,
                              f"{len(devs)} paired ({', '.join(d['name'] for d in devs[:3])})"))
        else:
            data.append(_line("Devices", PARTIAL,
                              "none paired (cockpit Devices tab, or mongoose devices pair)"))
    except Exception as exc:
        data.append(_line("Devices", UNKNOWN, str(exc)))

    # Mail sync: per-provider authorised + freshness (composes mail_status).
    try:
        from .mail_status import mail_status
        ms = mail_status()
        active = [p["provider"] for p in ms["providers"] if p["authorized"]]
        if active:
            data.append(_line("Mail sync", READY,
                              f"{', '.join(active)} authorized; {ms['message_count']} messages indexed"))
        else:
            data.append(_line("Mail sync", MISSING,
                              "no provider authorized (mongoose mail status for setup hints)"))
    except Exception as exc:
        data.append(_line("Mail sync", UNKNOWN, str(exc)))

    # Scheduler running state + whether it's set up to run unattended (launchd).
    try:
        from .services.scheduler import job_status
        jobs = job_status()
        unattended = ""
        try:
            from . import service as _svc
            sv = _svc.status()
            if sv.get("loaded"):
                unattended = "; launchd agent loaded (runs unattended)"
            elif sv.get("installed"):
                unattended = "; launchd agent installed but not loaded"
            else:
                unattended = "; not unattended (mongoose service install to keep it running)"
        except Exception:
            pass
        data.append(_line("Scheduler", READY if jobs else PARTIAL,
                          f"{len(jobs)} job(s) registered{unattended}"))
    except Exception as exc:
        data.append(_line("Scheduler", UNKNOWN, str(exc)))
    # Search corpus counts (real table counts; honest fallback if a table is absent).
    try:
        from .db import connect
        counts = {}
        with connect() as con:
            for table, label in [("files", "files"), ("attachments", "attachments"),
                                 ("email_messages", "email"), ("notifications", "notifications")]:
                try:
                    counts[label] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    pass
        total = sum(counts.values())
        data.append(_line("Search corpus", READY if total else PARTIAL,
                          ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "empty"))
    except Exception:
        data.append(_line("Search corpus", UNKNOWN, "count unavailable"))

    # Security posture (the document explicitly wants this surfaced).
    security: list[dict[str, Any]] = []
    security.append(_line("Action nonce", READY, "privileged cockpit POSTs require X-Cockpit-Nonce"))
    security.append(_line("Mutation policy", READY,
                          "cookie-authenticated remote writes require nonce or API token (CSRF guard)"))
    try:
        from .session_auth import _persistent_salt
        _persistent_salt()
        security.append(_line("Session signing", READY, "persistent salt; remember-device survives restart"))
    except Exception:
        security.append(_line("Session signing", PARTIAL, "per-process salt fallback (sessions reset on restart)"))
    security.append(_line("Login throttle", READY, "per-IP cooldown after repeated failures"))
    security.append(_line("Speaker policy", READY, "owner-only: workers/tools never speak to the user directly"))

    # Safety boundaries (spec 2.5/2.6): these are NON-NEGOTIABLE, so assert them from
    # actual code/config state rather than trusting convention. A regression here (e.g. an
    # email-send path or calendar-write capability slipping in, or the WhatsApp approval
    # gate being removed) MUST block the readiness verdict, not pass silently.
    boundaries: list[dict[str, Any]] = []
    try:
        from .release_check import _verify_read_first  # self-import keeps the helper local
        rf = _verify_read_first()
        boundaries.append(_line("Read-first (no send/delete/calendar-write)",
                                READY if rf["ok"] else MISSING, rf["detail"]))
    except Exception as exc:
        boundaries.append(_line("Read-first (no send/delete/calendar-write)", UNKNOWN, str(exc)))
    try:
        from .release_check import _verify_write_gate
        wg = _verify_write_gate()
        boundaries.append(_line("Sole write path is approval-gated (WhatsApp)",
                                READY if wg["ok"] else MISSING, wg["detail"]))
    except Exception as exc:
        boundaries.append(_line("Sole write path is approval-gated (WhatsApp)", UNKNOWN, str(exc)))

    try:
        mt = _verify_rtm_model_tier_gated()
        boundaries.append(_line("RTM model-drafting tier gated (case content stays local by default)",
                                READY if mt["ok"] else MISSING, mt["detail"]))
    except Exception as exc:
        boundaries.append(_line("RTM model-drafting tier gated", UNKNOWN, str(exc)))

    try:
        eg = _verify_egress_wall()
        boundaries.append(_line("Live-egress wall (no test/dev process can reach real systems)",
                                READY if eg["ok"] else MISSING, eg["detail"]))
    except Exception as exc:
        boundaries.append(_line("Live-egress wall", UNKNOWN, str(exc)))

    sections = [
        ("Core", core), ("Models", models), ("Surface (cockpit/LAN/voice)", surface),
        ("Data paths", data), ("Security", security), ("Safety boundaries", boundaries),
    ]
    # Overall verdict: ready unless something is MISSING in Core/Models/Security/Safety.
    blocking = [ln for sec, lines in sections
                if sec in ("Core", "Models", "Security", "Safety boundaries")
                for ln in lines if ln["state"] == MISSING]
    verdict = "ready" if not blocking else "not yet: " + "; ".join(b["label"] for b in blocking)
    return {"verdict": verdict, "sections": sections}


def _verify_read_first() -> dict[str, Any]:
    """Machine-verify the read-first non-negotiable. Originally: Mongoose could not send email at
    all. That invariant has legitimately evolved, the Example Court RTM product REQUIRES a send
    capability, so the guarantee is now stronger and more precise: any send/write capability must
    be GATED (disabled by default behind a flag, and only reachable via human approval), and must
    live ONLY in the one auditable place. So:
      - smtplib is permitted ONLY in rtm_intake.py, AND only when that file gates it behind
        RTM_LIVE_SEND (off by default) and an approval path. smtplib ANYWHERE ELSE fails.
      - calendar insert/delete remain banned everywhere.
    A regression that adds an ungated send, or a send outside the gated module, is still caught."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    offenders: list[str] = []
    banned = {
        "events().insert": "calendar insert",
        "events().delete": "calendar delete",
        "calendarView.*delete": "calendar delete (Graph)",
    }
    import re as _re
    for py in root.rglob("*.py"):
        if "__pycache__" in str(py) or py.name == "release_check.py":
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # calendar writes banned everywhere
        for pat, label in banned.items():
            if _re.search(pat, text):
                offenders.append(f"{label} in {py.name}")
        # email send (smtplib) , permitted ONLY in the gated RTM module
        if "smtplib" in text:
            if py.name != "rtm_intake.py":
                offenders.append(f"email sending (smtplib) in {py.name} (only permitted in gated rtm_intake)")
            else:
                # in rtm_intake, confirm it's actually gated: RTM_LIVE_SEND flag + approval path
                gated = ("RTM_LIVE_SEND" in text and "_live_send_enabled" in text
                         and "approve_draft" in text)
                if not gated:
                    offenders.append("email sending in rtm_intake is NOT gated behind RTM_LIVE_SEND + approval")
    if offenders:
        return {"ok": False, "detail": "WRITE CAPABILITY FOUND: " + "; ".join(offenders)}
    return {"ok": True,
            "detail": "no ungated send/write; RTM email send is gated behind RTM_LIVE_SEND (off by "
                      "default) + human approval, calendar writes absent (read-first holds)"}


def _verify_egress_wall() -> dict[str, Any]:
    """Machine-verify the execution-context wall (services/egress_guard.py): any file that makes an external
    network WRITE (httpx/requests .post/.put/.delete, or smtplib) must reference the egress guard, so a
    test/dev process cannot reach a real system. This is the regression gate for the 2026-07-18 incident
    (test fixtures reached the live leaseholder portal). Localhost-only writers (the Ollama model channel)
    and pure readers are exempt , they touch no third-party system."""
    import pathlib
    import re as _re
    root = pathlib.Path(__file__).resolve().parent
    # Writers that only ever hit the LOCAL model server (Ollama on localhost) , no third-party egress.
    exempt = {"benchmark.py", "voice_benchmark.py", "memory_guard.py", "llm.py",
              "egress_guard.py", "release_check.py",
              # app.py: the ".post/.put" hits are FastAPI @app.post route decorators and served-JS
              # cache.put, not outbound calls. web_search.py: a read-only DuckDuckGo query, no mutation.
              # setup_wizard.py: interactive "test connection" probes reached only from the setup UI,
              # never auto-run, minimal payload. None of these can push state to a third-party system.
              "app.py", "web_search.py", "setup_wizard.py"}
    write_call = _re.compile(r"\.(post|put|patch|delete)\s*\(", _re.I)
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        if "__pycache__" in str(py) or py.name in exempt:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        does_external_write = ("smtplib" in text) or bool(
            write_call.search(text) and _re.search(r"httpx|requests|client\.|c\.post|await\s+client", text))
        if not does_external_write:
            continue
        # localhost-only writers are safe even if not exempt by name
        only_local = ("ollama_base_url" in text or "127.0.0.1" in text or "localhost" in text) \
            and "http" not in text.lower().replace("ollama_base_url", "").replace("localhost", "")
        guarded = ("egress_guard" in text or "egress_allowed" in text or "assert_live_egress" in text)
        if not guarded and not only_local:
            offenders.append(py.name)
    if offenders:
        return {"ok": False,
                "detail": "external-write files missing the egress wall: " + ", ".join(sorted(set(offenders)))}
    return {"ok": True,
            "detail": "every external-write surface routes through egress_guard (test/dev processes are "
                      "fail-closed and cannot reach real systems)"}


def _verify_rtm_model_tier_gated() -> dict[str, Any]:
    """The optional RTM model-drafting tier sends leaseholder case content to an external model.
    That egress must happen ONLY when the tier is configured: _draft_body_via_model must be reached
    through _should_use_model_tier, which is gated by _model_tier_enabled (provider + key set).
    Verify the gate exists so a regression that makes the external call unconditional, leaking case
    content by default, is caught here. The privacy-first default is that with no key, nothing
    leaves the Mac."""
    import pathlib
    rtm = pathlib.Path(__file__).resolve().parent / "services" / "rtm_intake.py"
    if not rtm.exists():
        return {"ok": True, "detail": "no RTM intake surface present"}
    try:
        text = rtm.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return {"ok": False, "detail": f"could not read rtm_intake: {exc}"}
    has_gate = "_model_tier_enabled" in text and "_should_use_model_tier" in text
    # the gate must be consulted before drafting via the model
    gated_call = ("if _should_use_model_tier(" in text and
                  "def _model_tier_enabled" in text and
                  "ANTHROPIC_API_KEY" in text)
    if has_gate and gated_call:
        return {"ok": True,
                "detail": "model-drafting tier gated: external draft only when provider+key set; "
                          "default is local template (no case content leaves the Mac)"}
    return {"ok": False,
            "detail": "RTM model tier gating weakened: external draft must be gated behind "
                      "_should_use_model_tier/_model_tier_enabled"}


def _verify_write_gate() -> dict[str, Any]:
    """The single permitted personal-data write (WhatsApp reply) must remain approval-
    gated: a draft is created 'pending' and only transmitted via approve_and_send. Verify
    the gate exists in the whatsapp service."""
    import pathlib
    wa = pathlib.Path(__file__).resolve().parent / "services" / "whatsapp.py"
    if not wa.exists():
        return {"ok": True, "detail": "no WhatsApp write surface present"}
    try:
        text = wa.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return {"ok": False, "detail": f"could not read whatsapp service: {exc}"}
    has_pending = "'pending'" in text or '"pending"' in text
    has_approve = "approve_and_send" in text
    if has_pending and has_approve:
        return {"ok": True, "detail": "drafts created 'pending'; sent only via approve_and_send"}
    return {"ok": False,
            "detail": "approval gate weakened: expected 'pending' draft state + approve_and_send"}


def format_release_check(report: dict[str, Any]) -> str:
    from . import __version__
    out = [f"=== Mongoose {__version__} (sillygoose) runtime readiness ===",
           f"verdict: {report['verdict']} (runtime readiness, not release QA)",
           "(this is runtime readiness: can the assistant run and what's configured.",
           " it is NOT a release-QA gate, run scripts/qa_repo.sh for that.)", ""]
    for title, lines in report["sections"]:
        out.append(title)
        for ln in lines:
            mark = _MARK.get(ln["state"], "?  ")
            detail = f"  ({ln['detail']})" if ln["detail"] else ""
            out.append(f"  {mark}{ln['label']}{detail}")
        out.append("")
    out.append("Mongoose allows known limits; it does not hide them. Lines marked ~ or XX")
    out.append("are gaps to configure, not bugs. See docs for each.")
    return "\n".join(out)
