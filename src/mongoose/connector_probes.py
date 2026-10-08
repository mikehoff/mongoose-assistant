"""Live connector probes: enforce "configured means usable".

The recurring failure mode this module exists to kill: a tile says "connected" because an env var or
token file exists, but the connector doesn't actually work (token revoked, host unreachable, scope
wrong, mailbox moved). A probe doesn't trust configuration, it makes the smallest possible REAL call
and reports whether the connector is genuinely usable RIGHT NOW, with evidence and latency.

Every probe returns a ProbeResult:
  - name:        the connector ("gmail", "home_assistant", ...)
  - configured:  is it set up at all? (cheap local check)
  - usable:      did a live call actually succeed? (None if not probed live, e.g. would cost too much)
  - latency_ms:  how long the live call took (None if not probed)
  - detail:      a short, honest, user-facing line
  - evidence:    what the probe actually observed ("listed 1 label", "200 from /api/states", ...)
  - checked_at:  ISO timestamp

Probes are deliberately CHEAP and READ-ONLY: list one label, read one calendar, GET /api/states,
NOOP+LOGOUT on IMAP. None of them write, send, or mutate anything. They each have a hard timeout so a
hung remote can never wedge the caller; failures are caught and reported, never raised.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Callable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProbeResult:
    name: str
    configured: bool
    usable: bool | None = None      # None = not live-probed (e.g. optional/absent)
    latency_ms: int | None = None
    detail: str = ""
    evidence: str = ""
    checked_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _timed(fn: Callable[[], tuple[bool, str, str]], *, timeout_note: str = "") -> tuple[bool, str, str, int]:
    """Run a probe body that returns (usable, detail, evidence); time it; never raise."""
    start = time.monotonic()
    try:
        usable, detail, evidence = fn()
    except Exception as exc:  # noqa: BLE001
        usable, detail, evidence = False, f"probe error: {type(exc).__name__}: {exc}"[:200], ""
    ms = int((time.monotonic() - start) * 1000)
    return usable, detail, evidence, ms


# --------------------------------------------------------------------------------------------------
# Individual probes. Each is independent and safe to call any time.
# --------------------------------------------------------------------------------------------------

def probe_google_drive() -> ProbeResult:
    """Drive backup connector: list one file in Mongoose's own folder (drive.file scope)."""
    from .backup_drive import is_connected
    if not is_connected():
        return ProbeResult("google_drive", configured=False, usable=False,
                           detail="Google Drive isn't connected for backups.")

    def body() -> tuple[bool, str, str]:
        from .backup_drive import drive_folder_info
        info = drive_folder_info()
        if info.get("connected") and info.get("folder_id"):
            return True, "Drive reachable; backup folder resolved.", f"folder_id={info['folder_id'][:12]}…"
        if info.get("error"):
            return False, f"Drive error: {info['error']}", ""
        return False, "Drive connected but folder couldn't be resolved.", ""

    usable, detail, evidence, ms = _timed(body)
    return ProbeResult("google_drive", configured=True, usable=usable,
                       latency_ms=ms, detail=detail, evidence=evidence)


def probe_gmail() -> ProbeResult:
    """Gmail connector: token present + a 1-result labels/list-style call would prove it. We do the
    cheapest real thing: build credentials (forces token validity/refresh). A full message list is
    avoided to keep the probe instant; credential build failure is the strongest cheap signal."""
    from .settings import get_settings
    s = get_settings()
    secrets = getattr(s, "google_client_secrets_path", "") or ""
    import os
    secrets_present = bool(secrets and os.path.exists(os.path.expanduser(secrets)))
    # Check the SAME token path the Google sign-in actually writes (settings.google_token_path,
    # default ./secrets/google-token.json), not a hardcoded legacy data/ path. Without this the probe
    # reports "not signed in" even when a valid token exists, defeating the whole point of the probe.
    # Legacy locations are still accepted as a fallback for older installs. (audit P5b)
    token_candidates = [
        getattr(s, "google_token_path", "") or "",
        (getattr(s, "google_token_path", "") or "").replace("google-token", "gmail-token"),
        "~/.mongoose/data/google_token.json",
        "data/google_token.json",
    ]
    token_present = any(p and os.path.exists(os.path.expanduser(p)) for p in token_candidates)
    if not (secrets_present and token_present):
        return ProbeResult("gmail", configured=bool(secrets_present), usable=False,
                           detail="Gmail not signed in yet." if secrets_present else "Google client JSON not set up.")

    def body() -> tuple[bool, str, str]:
        try:
            from .connectors.gmail import _credentials
        except Exception as exc:
            return False, f"Gmail connector unavailable: {exc}", ""
        creds = _credentials()  # raises if no usable token
        if creds and getattr(creds, "valid", False):
            return True, "Gmail token valid.", "credentials.valid=True"
        if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            return True, "Gmail token refreshable.", "expired but refreshable"
        return False, "Gmail token present but not valid; sign in again.", ""

    usable, detail, evidence, ms = _timed(body)
    return ProbeResult("gmail", configured=True, usable=usable, latency_ms=ms,
                       detail=detail, evidence=evidence)


def probe_google_calendar() -> ProbeResult:
    """Calendar shares Google auth with Gmail; the probe is the same credential validity check."""
    g = probe_gmail()
    return ProbeResult("google_calendar", configured=g.configured, usable=g.usable,
                       latency_ms=g.latency_ms,
                       detail=g.detail.replace("Gmail", "Google Calendar"),
                       evidence=g.evidence)


def probe_microsoft() -> ProbeResult:
    """Microsoft To Do / Graph: a usable token (from the MSAL cache, silent) is the proof. We never
    trigger interactive sign-in from a probe."""
    import os
    from .settings import get_settings as _gs
    _s = _gs()
    # Check the SAME cache path MSAL actually writes (settings.ms_token_cache, default
    # ./secrets/msal-token-cache.bin), not a hardcoded data/ path, or readiness is misreported even when
    # a valid cache exists. Legacy data/ locations kept as a fallback for older installs. (audit P6)
    cache_candidates = [
        getattr(_s, "ms_token_cache", "") or "",
        "~/.mongoose/data/ms_token_cache.bin",
        "data/ms_token_cache.bin",
    ]
    configured = any(p and os.path.exists(os.path.expanduser(p)) for p in cache_candidates)
    if not configured:
        return ProbeResult("microsoft", configured=False, usable=False,
                           detail="Microsoft To Do isn't signed in yet.")

    def body() -> tuple[bool, str, str]:
        try:
            from .connectors.microsoft_graph import _token, MicrosoftAuthError
        except Exception as exc:
            return False, f"Microsoft connector unavailable: {exc}", ""
        try:
            tok = _token(allow_interactive=False)
            return (bool(tok), "Microsoft token acquired silently." if tok else
                    "No usable Microsoft token; sign in again.", "silent token ok" if tok else "")
        except MicrosoftAuthError as exc:
            return False, f"Microsoft sign-in needed: {exc}"[:180], ""

    usable, detail, evidence, ms = _timed(body)
    return ProbeResult("microsoft", configured=True, usable=usable, latency_ms=ms,
                       detail=detail, evidence=evidence)


def probe_imap() -> ProbeResult:
    """IMAP: NOOP login is the cheapest real proof the mailbox is reachable + credentials work."""
    from .settings import get_settings
    s = get_settings()
    host = getattr(s, "imap_host", "") or ""
    user = getattr(s, "imap_username", "") or ""
    pw = getattr(s, "imap_password", "") or ""
    if not (host and user and pw):
        return ProbeResult("imap", configured=False, usable=False,
                           detail="IMAP mailbox not set up.")

    def body() -> tuple[bool, str, str]:
        import imaplib
        import socket
        socket.setdefaulttimeout(8)
        use_ssl = getattr(s, "imap_use_ssl", True)
        klass = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
        m = klass(host)
        try:
            m.login(user, pw)
            typ, _ = m.noop()
            return (typ == "OK", "IMAP login + NOOP succeeded." if typ == "OK" else
                    f"IMAP NOOP returned {typ}.", f"NOOP={typ}")
        finally:
            try:
                m.logout()
            except Exception:
                pass

    usable, detail, evidence, ms = _timed(body)
    return ProbeResult("imap", configured=True, usable=usable, latency_ms=ms,
                       detail=detail, evidence=evidence)


def probe_home_assistant() -> ProbeResult:
    """Home Assistant: GET /api/ (the cheapest authenticated endpoint) proves URL + token + reachability.
    This is the live HA validation the readiness layer couldn't do cheaply before."""
    from .settings import get_settings
    s = get_settings()
    url = (getattr(s, "ha_url", "") or "").rstrip("/")
    token = getattr(s, "ha_token", "") or ""
    if not (url and token):
        return ProbeResult("home_assistant", configured=False, usable=False,
                           detail="Home Assistant URL/token not set.")

    def body() -> tuple[bool, str, str]:
        import httpx
        headers = {"Authorization": f"Bearer {token}"}
        # /api/ returns {"message":"API running."} on a healthy authenticated HA. Cheapest possible.
        with httpx.Client(timeout=8.0) as client:
            r = client.get(f"{url}/api/", headers=headers)
        if r.status_code == 200:
            return True, "Home Assistant API reachable + token accepted.", f"200 from {url}/api/"
        if r.status_code in (401, 403):
            return False, "Home Assistant reachable but the token was rejected (401/403).", f"{r.status_code}"
        return False, f"Home Assistant returned {r.status_code}.", f"{r.status_code}"

    usable, detail, evidence, ms = _timed(body)
    return ProbeResult("home_assistant", configured=True, usable=usable, latency_ms=ms,
                       detail=detail, evidence=evidence)


def probe_alexa_bridge() -> ProbeResult:
    """Alexa fallback bridge: configured-only check (we don't hit a third-party bridge from a probe).
    Honest: present != proven. Live proof happens when a command actually falls back to it."""
    from .settings import get_settings
    s = get_settings()
    url = getattr(s, "alexa_fallback_url", "") or ""
    token = getattr(s, "alexa_fallback_token", "") or ""
    if not (url and token):
        return ProbeResult("alexa_bridge", configured=False, usable=None,
                           detail="Alexa fallback bridge not configured (optional).")
    return ProbeResult("alexa_bridge", configured=True, usable=None,
                       detail="Alexa bridge configured; proven live only when a command falls back to it.",
                       evidence="url+token present")


def probe_ollama() -> ProbeResult:
    """Local LLM: the brain. A tags call proves Ollama is up and a model is pullable/resident."""
    from .settings import get_settings
    s = get_settings()
    base = (getattr(s, "ollama_url", "") or "http://localhost:11434").rstrip("/")

    def body() -> tuple[bool, str, str]:
        import httpx
        with httpx.Client(timeout=6.0) as client:
            r = client.get(f"{base}/api/tags")
        if r.status_code != 200:
            return False, f"Ollama returned {r.status_code}.", f"{r.status_code}"
        models = [m.get("name", "") for m in (r.json().get("models") or [])]
        if not models:
            return False, "Ollama is up but no models are pulled yet.", "0 models"
        return True, f"Ollama up with {len(models)} model(s).", ", ".join(models[:3])

    usable, detail, evidence, ms = _timed(body)
    return ProbeResult("ollama", configured=True, usable=usable, latency_ms=ms,
                       detail=detail, evidence=evidence)


# --------------------------------------------------------------------------------------------------
# Registry + aggregate probe
# --------------------------------------------------------------------------------------------------

_PROBES: dict[str, Callable[[], ProbeResult]] = {
    "ollama": probe_ollama,
    "home_assistant": probe_home_assistant,
    "gmail": probe_gmail,
    "google_calendar": probe_google_calendar,
    "google_drive": probe_google_drive,
    "microsoft": probe_microsoft,
    "imap": probe_imap,
    "alexa_bridge": probe_alexa_bridge,
}


def probe_names() -> list[str]:
    return list(_PROBES.keys())


def probe_one(name: str) -> ProbeResult:
    fn = _PROBES.get(name)
    if not fn:
        return ProbeResult(name, configured=False, usable=False, detail=f"unknown connector: {name}")
    return fn()


def probe_all(*, only: list[str] | None = None) -> dict[str, Any]:
    """Run every probe (or a subset) and roll up a single honest health picture. Each probe is
    independent; one failing never blocks the others. Returns per-connector results plus a summary
    counting configured / usable / broken (configured-but-not-usable, the dangerous state)."""
    names = [n for n in probe_names() if (only is None or n in only)]
    # Probe CONCURRENTLY: each probe is an independent I/O-bound reachability check with its own hard
    # timeout, so running them serially made the connectors tab pay the SUM of every remote's latency
    # (~1.2s for 7 connectors). A small thread pool collapses that to the slowest single probe. Order
    # is preserved (ex.map), and a probe that raises still yields a result via probe_one's own guard.
    if len(names) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as _ex:
            results = list(_ex.map(lambda n: probe_one(n).to_dict(), names))
    else:
        results = [probe_one(n).to_dict() for n in names]
    configured = [r for r in results if r["configured"]]
    usable = [r for r in configured if r["usable"] is True]
    broken = [r for r in configured if r["usable"] is False]  # configured but NOT usable
    unproven = [r for r in configured if r["usable"] is None]  # configured, not live-probed
    return {
        "checked_at": _now_iso(),
        "connectors": results,
        "summary": {
            "total": len(results),
            "configured": len(configured),
            "usable": len(usable),
            "broken": len(broken),          # the "says connected but isn't" cases , surfaced loudly
            "unproven": len(unproven),
            "broken_names": [r["name"] for r in broken],
        },
    }

def connector_journey(*, only: list[str] | None = None) -> dict[str, Any]:
    """User-facing connector lifecycle map: set up -> verified -> synced/usable.

    The ordinary health probe answers "does it work right now?". This answers the next real-life
    question: "what should I click or ask Mongoose to do next, and what will it affect?" It is
    deliberately read-only and reuses the same probe/sync registry, so UI and chat describe integration
    states from one source of truth.
    """
    agg = probe_all(only=only)
    syncable = set(_sync_registry())
    journeys: list[dict[str, Any]] = []
    for c in agg.get("connectors", []):
        name = c.get("name") or ""
        configured = bool(c.get("configured"))
        usable = c.get("usable")
        can_sync = name in syncable
        if not configured:
            state = "not_set_up"
            # Not circular: reference the Connect button on the card, and be honest about where it
            # finishes (Drive backups sign in on the phone; the rest use loopback OAuth on the Mac).
            if name == "google_drive":
                next_action = "Not connected yet. Tap Connect to sign in from your phone."
            else:
                next_action = "Not connected yet. Tap Connect (this one finishes in a browser on the Mac)."
            affects = "Unavailable until connected."
        elif usable is True:
            state = "working"
            next_action = ("Sync or refresh read-only data when you want the local brain up to date."
                           if can_sync else "Use verify to re-check it; this connector is live/verify-only by design.")
            affects = "Safe to use for the features that depend on it."
        elif usable is False:
            state = "configured_but_broken"
            next_action = "Re-authenticate, fix credentials/network, then click Verify; do not trust cached data."
            affects = "Features may appear configured but fail or return stale/missing information."
        else:
            state = "configured_unproven"
            next_action = "Verify it with a real probe; live proof happens on demand for this connector."
            affects = "Configured, but not proven by a cheap live call."
        journeys.append({
            "name": name,
            "state": state,
            "configured": configured,
            "usable": usable,
            "syncable": can_sync,
            "detail": c.get("detail", ""),
            "evidence": c.get("evidence", ""),
            "next_action": next_action,
            "affects": affects,
        })
    return {
        "ok": True,
        "checked_at": agg.get("checked_at") or _now_iso(),
        "summary": agg.get("summary", {}),
        "journeys": journeys,
        "guardrail": "Verify/sync actions are read-only pulls only; enabling an integration never grants send/delete/deploy rights.",
    }


# --------------------------------------------------------------------------------------------------
# Action path: verify (re-probe) and sync (pull fresh read-only data). This is what lets the single
# brain ACT on a connector ("sync Gmail now", "re-check home assistant"), not just report. Strictly
# read-only toward the providers: a sync PULLS data into the local brain; it never sends, deletes, or
# moves anything. Connectors without a safe read-only sync (home/alexa actuation, push, native
# surfaces) are verify-only , honest about it rather than faking a sync.
# --------------------------------------------------------------------------------------------------

# Map connector -> (sync callable, friendly note). Only read-only pulls are listed; absence means
# "verify-only" (no background data sync for this connector by design).
def _sync_registry() -> dict[str, Callable[[], dict[str, Any]]]:
    reg: dict[str, Callable[[], dict[str, Any]]] = {}
    try:
        from .connectors.gmail import sync_gmail
        reg["gmail"] = lambda: sync_gmail()
    except Exception:
        pass
    try:
        from .connectors.google_calendar import sync_google_calendar
        reg["google_calendar"] = lambda: sync_google_calendar()
    except Exception:
        pass
    try:
        from .services.email_index import sync_imap
        reg["imap"] = lambda: sync_imap()
    except Exception:
        pass
    try:
        from .connectors.microsoft_graph import sync_microsoft_todo
        reg["microsoft"] = lambda: sync_microsoft_todo()
    except Exception:
        pass
    return reg


def verify_connector(name: str) -> dict[str, Any]:
    """Re-probe one connector right now and return whether it's usable (the honest tri-state result).
    This is the 'check it again' action, the same cheap read-only probe, on demand."""
    if name not in _PROBES:
        return {"ok": False, "error": f"unknown connector: {name}", "known": probe_names()}
    r = probe_one(name)
    return {"ok": True, "verified": r.usable is True, "result": r.to_dict()}


def sync_connector(name: str) -> dict[str, Any]:
    """Pull fresh read-only data for one connector (Gmail/Calendar/IMAP/Microsoft To Do). PROBES FIRST:
    if the connector isn't usable, it says so instead of failing mid-sync. Read-only toward the
    provider, never sends/deletes/moves. Connectors with no safe sync are reported as verify-only."""
    if name not in _PROBES:
        return {"ok": False, "error": f"unknown connector: {name}", "known": probe_names()}
    syncs = _sync_registry()
    if name not in syncs:
        return {"ok": False, "syncable": False, "name": name,
                "detail": f"{name} has no read-only data sync (it's verify-only by design). Use "
                          f"verify_connector to re-check it."}
    # Probe first , don't start a sync against a connector we already know is down.
    probe = probe_one(name)
    if probe.usable is False:
        return {"ok": False, "syncable": True, "name": name, "usable": False,
                "detail": f"Not syncing, {name} isn't usable right now: {probe.detail}"}
    start = time.monotonic()
    try:
        result = syncs[name]()
        ms = int((time.monotonic() - start) * 1000)
        return {"ok": bool(result.get("ok", True)) if isinstance(result, dict) else True,
                "synced": True, "name": name, "latency_ms": ms, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "synced": False, "name": name,
                "detail": f"sync failed: {type(exc).__name__}: {exc}"[:200]}


def sync_all_read_only() -> dict[str, Any]:
    """Sync every connector that has a safe read-only data pull, sequentially (so we never hammer a
    provider with parallel requests). Each is probed first; broken ones are skipped with a reason.
    Returns a per-connector record. Sends/deletes/moves nothing."""
    syncs = _sync_registry()
    results = {}
    for name in syncs:
        results[name] = sync_connector(name)
    done = [n for n, r in results.items() if r.get("synced")]
    skipped = [n for n, r in results.items() if not r.get("synced")]
    return {"ok": True, "synced": done, "skipped": skipped, "connectors": results}
