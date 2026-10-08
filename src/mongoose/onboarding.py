from __future__ import annotations

from typing import Any

# The onboarding spine: ONE guided journey from "the app just opened" to "it's running,
# my data is connecting, and my phone is paired".
#
# This is the connective tissue the product was missing. The pieces already existed (core
# health, the integrations wizard, device pairing), but nothing sequenced them into a path
# a person walks. In particular the PHONE HANDOFF was a separate tab you had to discover;
# here it is the natural closing step of the same flow.
#
# The model is deliberately a STATE MACHINE over real signals, not a static script: it
# reads actual system state (is the local brain up? is anything connected? is a phone
# paired?) and reports which step the person is on and what the single next action is. So
# if they quit halfway and reopen, they resume exactly where they were, no lost place, no
# repeated steps.

# The ordered steps of first-run. Each is computed, not assumed.
STEP_BRAIN = "brain"          # the local brain (Ollama + model) must be reachable
STEP_CONNECT = "connect"      # connect at least one source of real data (optional but urged)
STEP_PHONE = "phone"          # pair a phone: the handoff, the moment the product becomes yours
STEP_DONE = "done"            # set up enough to be useful; the flow steps aside

_STEP_ORDER = [STEP_BRAIN, STEP_CONNECT, STEP_PHONE, STEP_DONE]


def _brain_ready() -> tuple[bool, str]:
    try:
        from .wizard import _core_status
        cs = _core_status()
        ok = bool(cs.get("ready"))
        # surface the brain line detail if not ready
        detail = ""
        for it in cs.get("items", []):
            if not it.get("ok"):
                detail = f"{it.get('label')}: {it.get('detail') or 'not ready'}"
                break
        return ok, detail
    except Exception as exc:
        return False, str(exc)


def _connected_count() -> int:
    """How many data sources are ACTUALLY connected, with evidence, not just config
    presence. A fresh .env.example ships default paths and values (GOOGLE_CLIENT_SECRETS_PATH,
    NTFY_URL, MS_TENANT_ID=common, ...) that are scaffolding, NOT connected data; counting
    those made first-run claim 'information connected' on a brand-new install (QA-audit P0.1).

    Evidence means: a real OAuth token file exists on disk, OR a credential secret is set, OR
    the source has actually ingested data. Config/placeholder presence does not count."""
    return len(connected_sources())


def connected_sources() -> list[str]:
    """The data sources with real evidence of being connected. See _connected_count."""
    import os
    sources: list[str] = []
    try:
        from .settings import get_settings
        s = get_settings()
    except Exception:
        s = None

    def _token_exists(attr: str) -> bool:
        p = getattr(s, attr, "") if s else ""
        return bool(p and os.path.exists(p))

    # Single source of truth (see oauth.google_token_present / microsoft_token_present): both
    # check the configured path AND known legacy fallback locations, so this can never again
    # disagree with the other UI surface that reports the same fact.
    try:
        from .oauth import google_token_present, microsoft_token_present
        google = google_token_present()
        microsoft = microsoft_token_present()
    except Exception:
        # oauth module unavailable for some reason: fall back to the narrower direct check
        # rather than crashing onboarding status.
        google = _token_exists("google_token_path")
        ms_cache = getattr(s, "ms_token_cache", "") if s else ""
        microsoft = bool(ms_cache and os.path.exists(os.path.expanduser(ms_cache))) if s else False
    # IMAP: host AND password actually set.
    imap = bool(getattr(s, "imap_host", "") and getattr(s, "imap_password", "")) if s else False

    if google:
        sources.append("google")
    if microsoft:
        sources.append("microsoft")
    if imap:
        sources.append("imap")

    # WhatsApp + any source that has actually ingested data is real evidence too.
    try:
        from .db import connect
        with connect() as con:
            for table, label in (("email_messages", "email"), ("events", "calendar"),
                                 ("whatsapp_messages", "whatsapp"), ("files", "files")):
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    n = 0
                if n and label not in sources:
                    sources.append(label)
    except Exception:
        pass
    return sources


def _paired_count() -> int:
    try:
        from .devices import list_devices
        return len([d for d in list_devices() if not d.get("revoked")])
    except Exception:
        return 0


def onboarding_state() -> dict[str, Any]:
    """Where is the person in first-run, and what is the single next action? Computed from
    real signals so it's resumable and honest."""
    brain_ok, brain_detail = _brain_ready()
    connected = _connected_count()
    paired = _paired_count()

    # Determine the current step: the first step not yet satisfied, in order.
    # A persisted "skip connect" choice (survives reloads, unlike the old browser-only var)
    # lets a user who chose phone-first move past the connect step. (QA-audit P1)
    skip_connect = False
    try:
        from .db import get_app_state
        skip_connect = (get_app_state("onboarding_skip_connect", "false") == "true")
    except Exception:
        skip_connect = False

    if not brain_ok:
        current = STEP_BRAIN
    elif paired == 0 and connected == 0 and not skip_connect:
        # nothing connected and no phone: urge connecting data first (the assistant is
        # thin without it), but the person may skip to phone.
        current = STEP_CONNECT
    elif paired == 0:
        current = STEP_PHONE
    else:
        current = STEP_DONE

    steps = []
    for s in _STEP_ORDER:
        if s == STEP_DONE:
            continue
        if s == STEP_BRAIN:
            done = brain_ok
            steps.append({"key": s, "title": "Start the local brain",
                          "done": done,
                          "detail": ("The on-device model is running." if done
                                     else (brain_detail or "Start Ollama and pull the model."))})
        elif s == STEP_CONNECT:
            done = connected > 0
            steps.append({"key": s, "title": "Connect your information",
                          "done": done,
                          "detail": (f"{connected} source(s) connected." if done
                                     else "Connect calendar, email, or messages so Mongoose has something to work with.")})
        elif s == STEP_PHONE:
            done = paired > 0
            steps.append({"key": s, "title": "Add your phone",
                          "done": done,
                          "detail": (f"{paired} device(s) paired." if done
                                     else "Scan a code to make your phone a Mongoose surface (approvals, voice, what changed).")})

    pct = int(100 * sum(1 for st in steps if st["done"]) / max(1, len(steps)))
    return {
        "current_step": current,
        "complete": current == STEP_DONE,
        "steps": steps,
        "progress_pct": pct,
        "next_action": _next_action(current),
        "signals": {"brain_ready": brain_ok, "connected": connected, "paired": paired},
    }


def _next_action(step: str) -> dict[str, str]:
    if step == STEP_BRAIN:
        return {"label": "Check the local brain",
                "hint": "Mongoose needs its on-device model running. Start Ollama, then this updates automatically."}
    if step == STEP_CONNECT:
        return {"label": "Connect your information",
                "hint": "Pick a source (calendar, email, messages). You can do one now and more later."}
    if step == STEP_PHONE:
        return {"label": "Pair your phone",
                "hint": "Scan the code with your phone to hand off approvals, voice, and notifications."}
    return {"label": "You're set up",
            "hint": "Mongoose has what it needs. You can always connect more or add devices later."}
