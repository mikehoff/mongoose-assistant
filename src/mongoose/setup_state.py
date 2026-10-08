"""setup_state.py: the ONE place that computes "is X ready" for every owner-facing setup
surface. Wraps the already-live, already-tested logic in doctor.py/app.py/self_signed_tls.py
etc; it does not reimplement any of it, because reimplementing the same fact twice is exactly
how doctor and the cockpit HTTPS panel disagreed earlier tonight, and how a "not configured"
message survived next to a working self-signed cert.

External context: a second reviewer's regression triage repeatedly found the same deep
failure mode across HTTPS, phone pairing, WhatsApp, embeddings, and brain-location: each
surface computed its own answer to the same underlying fact, so a fix landing in one place
never reached its siblings. This module is the fix for THAT pattern, not for any one symptom.

Every surface returns the same shape:
    ok: bool                 -- is this actually ready, right now
    status: str              -- machine-readable state (see per-surface docstrings)
    detail: str               -- one human sentence, safe to render as-is
    next_action: str | None  -- what the owner can do next, in plain language (no terminal)
    next_action_url: str | None  -- a route the UI can turn into a button, if one exists
    severity: str            -- "ok" | "info" | "warn" (never "error": a setup gap is not a
                                crash; "protected" states must never read as failures)
    facts: dict               -- surface-specific detail for anyone who wants it

Callers (doctor, cockpit Overview, /cockpit/setup-state, guest-invite gating) should treat
`facts` as informational only and never branch on it directly; branch on `ok`/`status`.
"""
from __future__ import annotations

from typing import Any


def _surface(*, ok: bool, status: str, detail: str, severity: str = "info",
             next_action: str | None = None, next_action_url: str | None = None,
             **facts: Any) -> dict[str, Any]:
    return {"ok": bool(ok), "status": status, "detail": detail, "severity": severity,
           "next_action": next_action, "next_action_url": next_action_url, "facts": facts}


def _brain_location_facts() -> tuple[bool, str]:
    """The real computation, moved here from doctor.py so doctor and this module delegate
    the SAME direction consistently (doctor -> setup_state, never the reverse, which would
    be a circular import the moment doctor's OTHER checks also delegate to setup_state).
    A forked brain (a second mongoose.sqlite3 in a download folder) is how a whole evening's
    conversation once vanished: it was safe, in a different folder's database. Canonical, or
    an explicit MONGOOSE_DB_PATH in the real process environment (tests/CI), are both fine; a
    .env-file override is refused at startup and surfaced here as protection, not failure,
    when the canonical brain is what's actually active."""
    import os as _os
    import sqlite3 as _sq
    from pathlib import Path as _P
    from . import settings as _settings_mod
    from .settings import get_settings
    _canon = _P(_os.path.expanduser("~")) / ".mongoose" / "data" / "mongoose.sqlite3"
    _eff = _P(str(get_settings().db_path)).expanduser()
    _env_override = "MONGOOSE_DB_PATH" in _os.environ
    _bits = []
    _ok = True
    if _eff == _canon:
        _bits.append(f"canonical brain: {_eff}")
    elif _env_override:
        _bits.append(f"explicit MONGOOSE_DB_PATH override: {_eff}")
    else:
        _ok = False
        _bits.append(f"NON-CANONICAL brain without explicit override: {_eff} (expected {_canon})")
    _rejected = dict(getattr(_settings_mod, "REJECTED_BRAIN_OVERRIDES", {}) or {})
    if _rejected:
        # A refused .env override IS the system protecting the canonical database; that is
        # only ever a real problem if the brain isn't canonical despite the refusal, which
        # the branch above already catches. When we're already on the canonical brain, a
        # refused override is a success story, not a red flag.
        if _eff == _canon:
            _bits[-1] = f"canonical brain: {_eff}; protected against a stale .env override"
        else:
            _ok = False
        _bits.append("a .env file tried to move the brain and was refused: "
                     + ", ".join(f"{k}={v}" for k, v in _rejected.items()))
    _strays = []
    for _cand in {(_P.cwd() / "data" / "mongoose.sqlite3").resolve(),
                  (_P(__file__).resolve().parent.parent.parent / "data" / "mongoose.sqlite3")}:
        try:
            if _cand.exists() and _cand != _eff.resolve():
                _con = _sq.connect(f"file:{_cand}?mode=ro", uri=True)
                try:
                    _n = _con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                except Exception:
                    _n = 0
                finally:
                    _con.close()
                if _n:
                    _strays.append(f"{_cand} ({_n} messages)")
        except Exception:
            continue
    if _strays:
        _ok = False
        _bits.append("ANOTHER brain with real conversation exists: " + "; ".join(_strays)
                     + ", your history may be split across brains")
    return _ok, "; ".join(_bits)


def _brain_location() -> dict[str, Any]:
    """Is this process reading the ONE canonical brain? The real computation lives in
    _brain_location_facts above; doctor.py's own check now delegates to setup_state for this
    too, so the two can never disagree again."""
    try:
        ok, detail = _brain_location_facts()
        return _surface(ok=ok, status="canonical" if ok else "non_canonical", detail=detail,
                        severity="ok" if ok else "warn",
                        next_action=None if ok else "Check MONGOOSE_DB_PATH in your real "
                                                    "process environment; a stray database "
                                                    "may hold real conversation history.")
    except Exception as exc:
        return _surface(ok=False, status="error", detail=f"brain location check failed: {exc}",
                        severity="warn")


def _https() -> dict[str, Any]:
    """Local HTTPS for LAN mic/push, self-signed or trusted. Wraps self_signed_status() the
    same way doctor's own https check does, so the cockpit TLS panel, doctor, and this surface
    can never disagree about the same cert again."""
    try:
        from .self_signed_tls import self_signed_status
        from .settings import get_settings
        import os
        ss = self_signed_status()
        settings = get_settings()
        configured = bool(ss.get("configured"))
        if not configured:
            tls_cert = getattr(settings, "tls_certfile", "")
            tls_key = getattr(settings, "tls_keyfile", "")
            configured = bool(tls_cert and tls_key and os.path.exists(tls_cert) and os.path.exists(tls_key))
        present = bool(ss.get("present"))
        if configured:
            status, detail, severity = "serving", "HTTPS is configured and serving", "ok"
            if ss.get("expires"):
                detail += f"; certificate expires {ss['expires']}"
            next_action, next_action_url = None, None
        elif present:
            status = "restart_pending"
            detail = "a certificate exists but settings don't point at it yet"
            severity = "warn"
            next_action, next_action_url = "Restart Mongoose to start serving it", None
        else:
            status, detail, severity = "not_configured", "HTTPS is not configured yet", "info"
            next_action = "Generate a certificate (one click, no terminal)"
            next_action_url = "/cockpit/tls/self-signed"
        return _surface(ok=configured, status=status, detail=detail, severity=severity,
                        next_action=next_action, next_action_url=next_action_url,
                        present=present, configured=configured)
    except Exception as exc:
        return _surface(ok=False, status="error", detail=f"https check error: {exc}", severity="warn")


def _phone_home(request: Any = None) -> dict[str, Any]:
    """Owner-phone-at-home readiness: LAN reachability, independent of remote-away and
    guest-invite readiness (those are separate surfaces on purpose, per the "phone-at-home
    must not be tangled with remote/guest" finding)."""
    try:
        from . import app as _app
        reachable = _app._lan_reachable_now()
        diag = _app._phone_reachability_diagnosis()
        if reachable:
            return _surface(ok=True, status="reachable",
                            detail="this Mac is reachable on the LAN; phone pairing works",
                            severity="ok", **diag)
        reason = diag.get("reason", "unknown")
        if reason == "configured_but_socket_dead":
            detail = ("settings say LAN access is on, but this running process started "
                      "before that took effect")
            next_action = "Repair phone access (one click, no terminal)"
        else:
            detail = "LAN access for phone pairing is not enabled yet"
            next_action = "Enable phone access (one click, no terminal)"
        return _surface(ok=False, status="not_reachable", detail=detail, severity="info",
                        next_action=next_action, next_action_url="/cockpit/enable-phone", **diag)
    except Exception as exc:
        return _surface(ok=False, status="error", detail=f"phone-home check error: {exc}", severity="warn")


def _remote_away() -> dict[str, Any]:
    """Reaching Mongoose from OUTSIDE the home network: DDNS + a publicly-trusted cert. This
    is a genuinely separate, additional requirement from phone-at-home and must never be
    presented as a prerequisite for it.

    Same class of fix as phone_home got earlier: "DDNS is configured" is not proof anyone can
    actually reach it. reachability_diagnosis() checks whether the DDNS name currently
    resolves to this network's real public IP (catches a stalled updater) and whether this
    Mac can reach its own public hostname:port at all (catches a router that can't loop back
    to its own address, or a missing port-forward). Honest limitation, stated rather than
    hidden: neither check proves a genuinely external client can reach this Mac; only a real
    external vantage point can prove that."""
    try:
        from .ddns import public_url, reachability_diagnosis
        from .setup_wizard import effective_env
        from .acme_tls import certificate_status
        env = effective_env()
        # Tunnel mode comes FIRST, and deliberately does not fall through to the DDNS
        # checks: a Cloudflare hostname resolves to Cloudflare's edge IPs, never to this
        # network's public IP, so reachability_diagnosis would misread a perfectly working
        # tunnel as "dns_stale". Tunnel proof is also strictly STRONGER than anything the
        # DDNS path can offer from this machine: the probe leaves the Mac, crosses the
        # public internet to Cloudflare's edge, and comes back down the tunnel, which is the
        # exact path a guest's phone uses, so the old "a self-test can't prove external
        # reachability" limitation genuinely does not apply here.
        from .cloudflare_tunnel import tunnel_state, tunnel_end_to_end_proof
        tst = tunnel_state(env)
        if tst["configured"]:
            proof = tunnel_end_to_end_proof(tst["hostname"])
            if proof.get("ok"):
                return _surface(ok=True, status="ready_via_tunnel",
                                detail=f"reachable at https://{tst['hostname']} through the "
                                       "Cloudflare tunnel, proven end-to-end over the public "
                                       "internet (the same path a guest's phone uses)",
                                severity="ok", public_url=f"https://{tst['hostname']}",
                                tunnel=tst, proof=proof)
            missing = ("cloudflared isn't running" if not tst["process_running"]
                      else proof.get("error") or "the public hostname did not answer with this Mongoose")
            return _surface(ok=False, status="tunnel_configured_not_proven",
                            detail=f"tunnel to https://{tst['hostname']} is configured, but {missing}",
                            severity="warn", next_action="Run: mongoose tunnel diagnose",
                            public_url=f"https://{tst['hostname']}", tunnel=tst, proof=proof)
        url = public_url(env) or ""
        cert = certificate_status()
        has_ddns = bool(url)
        has_cert = bool(cert.get("present"))
        if not has_ddns:
            return _surface(ok=False, status="not_configured",
                            detail="no Dynamic DNS set up yet; needed to reach Mongoose from outside home",
                            severity="info", next_action="Set up Dynamic DNS",
                            next_action_url="/cockpit/ddns")
        if not has_cert:
            return _surface(ok=False, status="ddns_only", detail=f"DDNS is set to {url}, but no trusted certificate yet",
                            severity="info", next_action="Get a trusted certificate (one click, no terminal)",
                            next_action_url="/cockpit/tls/acme-obtain", public_url=url)
        diag = reachability_diagnosis(env)
        # A genuinely external proof (the owner's own paired device, on cellular, reporting
        # it just reached the public URL) is stronger evidence than anything a self-test run
        # from the Mac can produce, since a self-test can never rule out that the Mac is only
        # reaching itself. Recent proof (kept to 14 days: routers, DDNS, and ISPs all change)
        # upgrades the status even if today's self-test alone can't confirm it.
        external_proof_at = ""
        try:
            from .db import get_app_state
            external_proof_at = get_app_state("remote_external_proof_at") or ""
        except Exception:
            pass
        externally_proven = False
        if external_proof_at:
            try:
                from datetime import datetime, timezone
                proof_dt = datetime.fromisoformat(external_proof_at.replace("Z", "+00:00"))
                if proof_dt.tzinfo is None:
                    proof_dt = proof_dt.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - proof_dt).total_seconds() / 86400
                externally_proven = age_days < 14
            except Exception:
                pass
        if diag.get("ok") or externally_proven:
            detail = f"reachable at {url} with a trusted certificate"
            detail += (" (confirmed from outside by your own phone)" if externally_proven
                      else " (self-test only; not yet confirmed from outside)")
            return _surface(ok=True, status="ready" if diag.get("ok") else "ready_externally_proven",
                            detail=detail, severity="ok",
                            public_url=url, diagnosis=diag, externally_proven=externally_proven)
        if diag.get("reason") == "listener_not_accepting":
            return _surface(ok=False, status="listener_not_accepting",
                            detail="Mongoose itself isn't accepting connections right now, most "
                                   "likely a process started before the current LAN settings took "
                                   "effect. This is not a router problem.",
                            severity="warn", next_action="Repair phone/remote access (restarts "
                                                          "the listener, no terminal)",
                            next_action_url="/device/remote/repair", public_url=url, diagnosis=diag)
        if diag.get("reason") == "dns_stale":
            return _surface(ok=False, status="dns_stale",
                            detail=f"{url} currently points at an out-of-date IP; the DDNS updater "
                                   "has fallen behind",
                            severity="warn", next_action="Run a DDNS update",
                            next_action_url="/cockpit/ddns", public_url=url, diagnosis=diag)
        if diag.get("reason") == "hairpin_or_no_portforward":
            return _surface(ok=False, status="not_reachable_from_here",
                            detail=f"{url} is configured correctly, and Mongoose itself is accepting "
                                   "connections; the remaining likely cause is on your router "
                                   "specifically, either it can't loop back to its own public "
                                   "address for a device on the same home network, or the port "
                                   "isn't forwarded to this Mac yet",
                            severity="info", public_url=url, diagnosis=diag)
        return _surface(ok=False, status="unverified",
                        detail=f"{url} is configured; could not confirm reachability from here",
                        severity="info", public_url=url, diagnosis=diag)
    except Exception as exc:
        return _surface(ok=False, status="error", detail=f"remote-away check error: {exc}", severity="warn")


def _guest_invites() -> dict[str, Any]:
    """Guest invite links need remote_away's requirements (a friend is, by definition,
    outside the home). Reuses _remote_away rather than re-deriving readiness.

    One deliberate exception: remote_away's self-test cannot distinguish "this Mac's own
    router won't loop back to its own public address" (hairpin) from "there is genuinely no
    port-forward" using only a self-test, because BOTH make a same-network self-connect fail
    the same way. Hairpin specifically does NOT affect a genuinely external guest, who never
    routes through this Mac's own hairpin path at all; treating that ambiguous self-test
    result as a hard block would frustrate a probably-working guest invite over a limitation
    that doesn't even apply to the guest's actual situation. DNS staleness and "not
    configured" genuinely would block anyone, guest included, so those still block."""
    remote = _remote_away()
    if remote["ok"] or remote["status"] == "not_reachable_from_here":
        detail = ("guest invite links can be generated" if remote["ok"] else
                  "guest invite links can be generated; this Mac's own self-test could not "
                  "confirm reachability (commonly a router hairpin limitation that does not "
                  "affect a genuinely external guest), so ask your guest to report back if "
                  "their link does not load")
        return _surface(ok=True, status="ready" if remote["ok"] else "ready_unverified",
                        detail=detail, severity="ok" if remote["ok"] else "info")
    return _surface(ok=False, status="blocked", detail="guest invites need remote access set up first: "
                    + remote["detail"], severity="info", next_action=remote.get("next_action"),
                    next_action_url=remote.get("next_action_url"))


def _notifications() -> dict[str, Any]:
    """Whether a phone will actually receive a notification. Chain-aware: if phone-at-home
    itself isn't reachable yet, that IS the blocker, and this surface says so instead of the
    circular 'open the launchpad and allow notifications' when the launchpad can't be
    opened."""
    try:
        phone = _phone_home()
        if not phone["ok"] and phone["status"] == "not_reachable":
            return _surface(ok=False, status="blocked_by_phone_home",
                            detail="notifications are blocked by phone setup: the launchpad "
                                   "isn't reachable yet, so it can't ask for permission",
                            severity="info", next_action=phone.get("next_action"),
                            next_action_url=phone.get("next_action_url"))
        from .services.web_push import subscription_count
        from .settings import get_settings
        subs = subscription_count()
        settings = get_settings()
        topic = getattr(settings, "ntfy_topic", "") or ""
        topic_private = bool(topic) and topic != "mongoose-private-topic"
        reachable = subs > 0 or (bool(getattr(settings, "ntfy_url", "")) and topic_private)
        if reachable:
            detail = f"{subs} phone subscription(s)" if subs > 0 else f"ntfy topic set ({topic})"
            return _surface(ok=True, status="reachable", detail=detail, severity="ok", subscriptions=subs)
        return _surface(ok=False, status="not_subscribed",
                        detail="no phone will receive notifications yet: 0 web-push subscriptions "
                               "and the ntfy topic is the shared default",
                        severity="info",
                        next_action="Open the launchpad on your phone and allow notifications",
                        next_action_url="/device/launchpad", subscriptions=subs)
    except Exception as exc:
        return _surface(ok=False, status="error", detail=f"notifications check error: {exc}", severity="warn")


def _embeddings() -> dict[str, Any]:
    """The fuzzy-matching model. Wraps embeddings.available() directly (the source of the
    old 'multilingual embeddings ?' bug was reading a key that function never returns)."""
    try:
        from .services.embeddings import available
        emb = available()
        ok = bool(emb.get("ok"))
        detail = "fuzzy-matching model active (multilingual included)" if ok else (emb.get("hint") or "not installed yet")
        return _surface(ok=ok, status="active" if ok else "not_active", detail=detail,
                        severity="ok" if ok else "info", auto_download=emb.get("auto_download", True))
    except Exception as exc:
        return _surface(ok=False, status="error", detail=f"embeddings check error: {exc}", severity="warn")


def _whatsapp() -> dict[str, Any]:
    """WhatsApp setup. Cockpit-first: points at the real Start-WhatsApp button
    (/cockpit/whatsapp/start-container), never a terminal command."""
    try:
        from .services.whatsapp_journey import connection_state
        st = connection_state()
        state = st.get("state", "")
        if state == "not_configured":
            return _surface(ok=False, status="not_configured",
                            detail="WhatsApp uses a local WAHA container; start it from the "
                                   "WhatsApp tab, no terminal needed",
                            severity="info", next_action="Start WhatsApp",
                            next_action_url="/cockpit/whatsapp/start-container",
                            can_start_container=bool(st.get("can_start_container", True)))
        connected = state == "connected"
        return _surface(ok=connected, status=state or "unknown",
                        detail=st.get("detail") or f"WhatsApp state: {state}",
                        severity="ok" if connected else "info")
    except Exception as exc:
        return _surface(ok=False, status="error", detail=f"whatsapp check error: {exc}", severity="warn")


_SURFACE_BUILDERS = {
    "brain_location": _brain_location,
    "https": _https,
    "phone_home": _phone_home,
    "remote_away": _remote_away,
    "guest_invites": _guest_invites,
    "notifications": _notifications,
    "embeddings": _embeddings,
    "whatsapp": _whatsapp,
}


def setup_state(*, only: list[str] | None = None) -> dict[str, Any]:
    """The canonical answer for every owner-facing setup surface, in one call. `only`
    restricts to specific surface names (e.g. a screen that only cares about phone_home
    should not pay for a WhatsApp/WAHA probe it doesn't need)."""
    names = only if only else list(_SURFACE_BUILDERS.keys())
    surfaces: dict[str, Any] = {}
    for name in names:
        builder = _SURFACE_BUILDERS.get(name)
        if builder is None:
            continue
        try:
            surfaces[name] = builder()
        except Exception as exc:
            surfaces[name] = _surface(ok=False, status="error", detail=f"{name} check error: {exc}",
                                      severity="warn")
    return {"surfaces": surfaces}
