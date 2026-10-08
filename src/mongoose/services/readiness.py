from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

# The readiness truth layer.
#
# This is NOT a dashboard and NOT new detection logic. It is a CONSOLIDATION layer: Mongoose
# already detects most of this (liveness, connected_sources, device freshness, scheduler
# last-run, settings config), scattered. This normalises those existing EVIDENCE signals into
# one capability/source/surface state model that voice, phone, Cockpit, Needs-Me and What-
# Changed all read from, so every surface tells the same truth.
#
# The ethic (the user's own): do NOT fake readiness. Every status traces to a real signal.
# Where a signal can't be obtained cheaply/locally (e.g. live HA Assist reachability), the
# status is `unknown` / `not_yet_measured`, never an optimistic guess. Placeholder config
# (a default path in .env) is `configured`, never `authorised` or `has_data`.

# --- normalised status vocabulary -------------------------------------------
NOT_CONFIGURED = "not_configured"     # nothing set up; not a problem, just not on
CONFIGURED = "configured"             # config present, but not yet authorised/proven
NEEDS_AUTH = "needs_authorisation"    # config present, missing the credential/consent
AUTHORISED = "authorised"             # credential present, but no data ingested yet
HAS_DATA = "has_data"                 # real data ingested and reasonably fresh
READY = "ready"                       # core capability up and usable
STALE = "stale"                       # was working, but data has aged past its window
DEGRADED = "degraded"                 # partially usable; something's missing
FAILING = "failing"                   # configured + expected up, but not working
DISABLED = "disabled"                 # turned off by the user
OFF_BY_DESIGN = "off_by_design"       # intentionally unsupported (local-first stance)
UNKNOWN = "unknown"                   # can't be determined from available signals

# severity drives ranking for voice/phone (higher = surfaced first)
_SEVERITY_RANK = {"critical": 4, "warning": 3, "info": 2, "ok": 1, "none": 0}

# which statuses are "fine" (no action needed) vs need attention
_ATTENTION_STATUSES = {NEEDS_AUTH, STALE, DEGRADED, FAILING}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReadinessItem:
    id: str
    domain: str                       # core | mail | calendar | messaging | files | surface | service | home | memory
    label: str
    status: str
    severity: str = "info"            # critical | warning | info | ok | none
    why: str = ""
    surface: str | None = None        # for device/surface items
    evidence: list[dict[str, Any]] = field(default_factory=list)
    affects: list[str] = field(default_factory=list)   # brief | what_changed | surpriser | search | voice | approvals
    next_action: dict[str, Any] | None = None          # {label, kind, command|url}
    privacy: str = "local"            # local | private
    last_checked: str | None = None
    last_success: str | None = None
    counts: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    @property
    def needs_attention(self) -> bool:
        return self.status in _ATTENTION_STATUSES


def _act(label: str, kind: str, **kw) -> dict[str, Any]:
    """Action metadata. kind in: safe_read, safe_repair, runs_sync, opens_url, starts_oauth,
    starts_service, manual_external, requires_approval. No action happens just because it's
    listed, the caller gates execution."""
    return {"label": label, "kind": kind, **kw}


# --- collectors (each grounds status in a real signal) ----------------------

def collect_core_readiness() -> list[ReadinessItem]:
    """Local brain + database + cockpit. The non-negotiable core."""
    items: list[ReadinessItem] = []
    now = _now_iso()

    # database: it self-heals on first run; if we can query it, it's ready.
    db_ok = True
    try:
        from ..db import connect
        with connect() as con:
            con.execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False
    items.append(ReadinessItem(
        id="database", domain="core", label="Local database",
        status=READY if db_ok else FAILING,
        severity="ok" if db_ok else "critical",
        why="The local SQLite store is reachable." if db_ok else "The database could not be opened.",
        evidence=[{"kind": "query", "status": "ok" if db_ok else "failed"}],
        affects=["brief", "what_changed", "search", "memory"], last_checked=now,
        next_action=None if db_ok else _act("Run readiness check", "safe_read")))

    # local model (Ollama): use doctor's check (cheap, cached-ish). Don't call the model.
    brain_ok = None
    detail = ""
    try:
        from ..doctor import run_doctor
        ollama = run_doctor(timeout_seconds=1.0).get("checks", {}).get("ollama", {})
        brain_ok = bool(ollama.get("ok"))
        detail = ollama.get("detail", "")
    except Exception:
        brain_ok = None
    if brain_ok is True:
        st, sev, why = READY, "ok", "The on-device model is reachable."
    elif brain_ok is False:
        st, sev, why = FAILING, "critical", detail or "Ollama isn't reachable; start it and pull the model."
    else:
        st, sev, why = UNKNOWN, "warning", "Couldn't check the local model."
    items.append(ReadinessItem(
        id="local_model", domain="core", label="Local brain (Ollama)",
        status=st, severity=sev, why=why,
        evidence=[{"kind": "ollama", "status": "ok" if brain_ok else ("failed" if brain_ok is False else "unknown")}],
        affects=["brief", "what_changed", "surpriser", "search", "voice"], last_checked=now,
        next_action=None if brain_ok else _act("Start Ollama and pull models", "manual_external",
                                                command="./scripts/pull_models.sh")))
    return items


def collect_scheduler_readiness() -> list[ReadinessItem]:
    """Whether background jobs (the daily brief etc.) are actually running."""
    now = _now_iso()
    last_run = None
    try:
        from .scheduler import job_status
        runs = [j.get("last_run") for j in job_status() if j.get("last_run")]
        last_run = max(runs) if runs else None
    except Exception:
        last_run = None
    if last_run is None:
        st, sev, why = UNKNOWN, "warning", "No record of the scheduler running yet. If it's not installed, tomorrow's brief may not run."
    else:
        age_h = _age_hours(last_run)
        if age_h is not None and age_h <= 26:
            st, sev, why = READY, "ok", "Background jobs are running on schedule."
        else:
            st, sev, why = STALE, "warning", "The scheduler hasn't run recently; the daily brief may not be firing."
    return [ReadinessItem(
        id="scheduler", domain="service", label="Scheduler (background jobs)",
        status=st, severity=sev, why=why,
        evidence=[{"kind": "last_run", "value": last_run}],
        affects=["brief", "what_changed", "surpriser"], last_checked=now, last_success=last_run,
        next_action=_act("Install the scheduler service", "starts_service",
                         command="mongoose service install") if st != READY else None)]


def _mail_calendar_items() -> list[ReadinessItem]:
    """Gmail/Microsoft/IMAP/calendar, grounded in: token file exists? data rows? last sync age?"""
    from ..onboarding import connected_sources
    now = _now_iso()
    sources = connected_sources()   # real-evidence list (token exists / creds set / data rows)
    items: list[ReadinessItem] = []

    # helper to build a source item from evidence
    def _source(id_, label, domain, configured, authorised, data_label, table, affects, sync_cmd, auth_cmd):
        from ..db import connect
        rows = 0
        last_idx = None
        try:
            with connect() as con:
                rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                last_idx = _latest(con, table, "indexed_at")
        except Exception:
            rows = 0
        ev = [{"kind": "credential", "status": "present" if authorised else "absent"},
              {"kind": "indexed_rows", "value": rows}]
        if last_idx:
            ev.append({"kind": "last_sync", "value": last_idx})
        if not configured and not authorised:
            st, sev, why, act = (NOT_CONFIGURED, "none", f"{label} isn't set up.", None)
        elif configured and not authorised:
            st, sev, why = NEEDS_AUTH, "warning", f"{label} is configured but not signed in."
            act = _act(f"Sign in to {label}", "starts_oauth", command=auth_cmd)
        elif authorised and rows == 0:
            st, sev, why = AUTHORISED, "info", f"{label} is signed in but hasn't synced any data yet."
            act = _act(f"Run {label} sync", "runs_sync", command=sync_cmd)
        else:
            age_h = _age_hours(last_idx)
            if age_h is not None and age_h > 26:
                st, sev, why = STALE, "warning", f"{label} is signed in, but the last sync was a while ago, data may be stale."
                act = _act(f"Run {label} sync", "runs_sync", command=sync_cmd)
            else:
                st, sev, why = HAS_DATA, "ok", f"{label} is signed in and has fresh data."
                act = None
        return ReadinessItem(
            id=id_, domain=domain, label=label, status=st, severity=sev, why=why,
            evidence=ev, affects=affects, last_checked=now, last_success=last_idx,
            counts={data_label: rows} if rows else None, privacy="private", next_action=act)

    try:
        from ..settings import get_settings
        s = get_settings()
    except Exception:
        s = None

    google_secrets = getattr(s, "google_client_secrets_path", "") if s else ""
    # CONFIGURED requires the secrets FILE to exist, not just the default path string in .env
    # (a path default is scaffolding, not setup , the same over-claim class as v0.31's count).
    google_cfg = bool(google_secrets and os.path.exists(google_secrets))
    google_auth = "google" in sources or "email" in sources
    items.append(_source("gmail", "Gmail", "mail", google_cfg, google_auth, "messages",
                         "email_messages", ["brief", "what_changed", "surpriser", "search"],
                         "mongoose sync-gmail", "mongoose authorize google"))

    ms_auth = "microsoft" in sources
    # ms_tenant_id defaults to "common" (scaffolding), so it is NOT evidence of setup; only a
    # real refresh token counts. Show Microsoft only if actually authorised.
    if ms_auth:
        items.append(ReadinessItem(
            id="microsoft", domain="mail", label="Microsoft (mail/calendar)",
            status=(HAS_DATA if ms_auth else NEEDS_AUTH),
            severity=("ok" if ms_auth else "warning"),
            why=("Microsoft is signed in." if ms_auth else "Microsoft is configured but not signed in."),
            evidence=[{"kind": "refresh_token", "status": "present" if ms_auth else "absent"}],
            affects=["brief", "what_changed", "search"], last_checked=now, privacy="private",
            next_action=None if ms_auth else _act("Sign in to Microsoft", "starts_oauth",
                                                   command="mongoose authorize microsoft")))

    imap_cfg = bool(getattr(s, "imap_host", "")) if s else False
    if imap_cfg or "imap" in sources:
        items.append(ReadinessItem(
            id="imap", domain="mail", label="IMAP email",
            status=(HAS_DATA if "imap" in sources else NEEDS_AUTH),
            severity=("ok" if "imap" in sources else "warning"),
            why=("IMAP is configured." if "imap" in sources else "IMAP host is set but credentials look incomplete."),
            evidence=[{"kind": "imap_host", "status": "set" if imap_cfg else "absent"}],
            affects=["brief", "search"], last_checked=now, privacy="private",
            next_action=None if "imap" in sources else _act("Finish IMAP setup", "opens_url", url="/ui?tab=setup")))

    return items


def collect_files_readiness() -> list[ReadinessItem]:
    from ..db import connect
    now = _now_iso()
    rows = 0
    last_idx = None
    try:
        with connect() as con:
            rows = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            last_idx = _latest(con, "files", "indexed_at")
    except Exception:
        rows = 0
    if rows == 0:
        st, sev, why = NOT_CONFIGURED, "none", "No local files indexed yet."
        act = _act("Index files", "safe_repair", command="mongoose index-files")
    else:
        age_h = _age_hours(last_idx)
        if age_h is not None and age_h > 72:
            st, sev, why = STALE, "info", "Indexed files may be out of date."
            act = _act("Reindex files", "safe_repair", command="mongoose reindex-files")
        else:
            st, sev, why = HAS_DATA, "ok", "Local files are indexed and searchable."
            act = None
    return [ReadinessItem(
        id="files", domain="files", label="Local files",
        status=st, severity=sev, why=why,
        evidence=[{"kind": "indexed_rows", "value": rows}, {"kind": "last_sync", "value": last_idx}],
        affects=["search", "brief"], last_checked=now, last_success=last_idx,
        counts={"files": rows} if rows else None, privacy="private", next_action=act)]


def collect_messaging_readiness() -> list[ReadinessItem]:
    """WhatsApp via WAHA: session connected? data fresh?"""
    now = _now_iso()
    try:
        from ..liveness import _whatsapp_session
        wa = _whatsapp_session()
    except Exception:
        wa = None
    if wa is None:
        # not set up at all , that's fine, not a failure
        return [ReadinessItem(
            id="whatsapp", domain="messaging", label="WhatsApp",
            status=NOT_CONFIGURED, severity="none",
            why="WhatsApp (WAHA) isn't set up.",
            evidence=[{"kind": "waha_session", "status": "absent"}],
            affects=["brief", "surpriser"], last_checked=now, privacy="private",
            next_action=_act("Set up WhatsApp", "opens_url", url="/ui?tab=setup"))]
    # wa is a liveness check dict; state OK means session working
    working = wa.get("state") == "ok"
    return [ReadinessItem(
        id="whatsapp", domain="messaging", label="WhatsApp",
        status=(HAS_DATA if working else DEGRADED),
        severity=("ok" if working else "warning"),
        why=(wa.get("detail") or ("WhatsApp session is connected." if working
                                  else "WhatsApp session is disconnected; social signals are stale.")),
        evidence=[{"kind": "waha_session", "status": "working" if working else "disconnected"}],
        affects=["brief", "surpriser"], last_checked=now, privacy="private",
        next_action=None if working else _act("Reconnect WhatsApp", "opens_url",
                                              url="http://127.0.0.1:3000"))]


def collect_device_readiness() -> list[ReadinessItem]:
    """Paired phones/tablets: what each can do, and whether it's checked in recently."""
    now = _now_iso()
    items: list[ReadinessItem] = []
    try:
        from ..devices import list_devices
        devices = [d for d in list_devices() if not d.get("revoked")]
    except Exception:
        devices = []
    if not devices:
        return [ReadinessItem(
            id="paired_devices", domain="surface", surface="phone", label="Paired phone/tablet",
            status=NOT_CONFIGURED, severity="info",
            why="No phone or tablet is paired yet.",
            evidence=[{"kind": "paired_count", "value": 0}],
            affects=["approvals", "voice"], last_checked=now,
            next_action=_act("Pair a device", "opens_url", url="/ui?tab=devices"))]
    for d in devices:
        fresh = d.get("freshness", {})
        fresh_state = fresh.get("state")
        name = d.get("name", "device")
        enabled = d.get("capabilities", [])
        if fresh_state == "healthy":
            st, sev, why = (READY if enabled else DEGRADED), ("ok" if enabled else "info"), \
                f"{name} is paired and checked in {fresh.get('detail', 'recently')}."
        elif fresh_state == "stale":
            st, sev, why = STALE, "warning", f"{name} hasn't checked in: {fresh.get('detail', 'a while')}."
        else:
            st, sev, why = DEGRADED, "info", f"{name} is paired but {fresh.get('detail', 'has not checked in')}."
        next_step = d.get("next_step") or ""
        items.append(ReadinessItem(
            id=f"device_{d.get('device_id', name)}", domain="surface", surface="phone",
            label=name, status=st, severity=sev, why=why,
            evidence=[{"kind": "last_seen", "value": d.get("last_seen")},
                      {"kind": "capabilities", "value": enabled}],
            affects=["approvals", "voice"], last_checked=now, last_success=d.get("last_seen"),
            next_action=(_act(next_step, "manual_external") if next_step else None)))
    return items


def collect_voice_readiness() -> list[ReadinessItem]:
    """Browser voice needs HTTPS for mic; CLI voice needs the whisper toolchain. We can detect
    the toolchain locally; live mic-over-HTTPS is environment-dependent -> stated honestly."""
    now = _now_iso()
    import importlib.util
    whisper = importlib.util.find_spec("whisper") is not None
    return [ReadinessItem(
        id="voice", domain="surface", surface="voice", label="Voice",
        status=(READY if whisper else DEGRADED),
        severity=("ok" if whisper else "info"),
        why=("Voice transcription toolchain is installed." if whisper
             else "Voice output works; the mic toolchain (whisper) isn't installed, so spoken input is limited."),
        evidence=[{"kind": "whisper", "status": "installed" if whisper else "absent"},
                  {"kind": "browser_mic_https", "status": "not_measured"}],
        affects=["voice"], last_checked=now,
        next_action=None if whisper else _act("Set up voice input", "safe_repair",
                                              command="mongoose setup-voice"))]


def collect_home_readiness() -> list[ReadinessItem]:
    """Home Assistant + Alexa fallback. We can see whether they're CONFIGURED (url/token set).
    Live reachability needs a network call -> not done here; status reflects config honestly."""
    now = _now_iso()
    items: list[ReadinessItem] = []
    try:
        from ..settings import get_settings
        s = get_settings()
    except Exception:
        s = None
    ha_cfg = bool(getattr(s, "ha_url", "") and getattr(s, "ha_token", "")) if s else False
    items.append(ReadinessItem(
        id="home_assistant", domain="home", label="Home Assistant",
        status=(CONFIGURED if ha_cfg else NOT_CONFIGURED),
        severity=("info" if ha_cfg else "none"),
        why=("Home Assistant is configured. Live reachability is checked when you control a device."
             if ha_cfg else "Home Assistant isn't set up (optional)."),
        evidence=[{"kind": "ha_url", "status": "set" if ha_cfg else "absent"},
                  {"kind": "live_reachability", "status": "not_measured"}],
        affects=[], last_checked=now,
        next_action=None if ha_cfg else _act("Connect Home Assistant", "opens_url", url="/ui?tab=setup")))
    alexa_cfg = bool(getattr(s, "alexa_fallback_url", "")) if s else False
    if alexa_cfg:
        items.append(ReadinessItem(
            id="alexa_fallback", domain="home", label="Alexa fallback",
            status=CONFIGURED, severity="info",
            why="Alexa fallback bridge is configured (used only if Home Assistant fails).",
            evidence=[{"kind": "alexa_url", "status": "set"}], affects=[], last_checked=now))
    return items


def collect_service_readiness() -> list[ReadinessItem]:
    """Optional local support services: SearXNG (web search), ntfy (phone push). Config-level
    (URL set), we don't ping them here to keep readiness cheap; reachability is not_measured."""
    now = _now_iso()
    items: list[ReadinessItem] = []
    try:
        from ..settings import get_settings
        s = get_settings()
    except Exception:
        s = None
    ntfy = bool(getattr(s, "ntfy_url", "")) if s else False
    items.append(ReadinessItem(
        id="ntfy", domain="service", label="Phone push (ntfy)",
        status=(CONFIGURED if ntfy else NOT_CONFIGURED),
        severity="info" if ntfy else "none",
        why=("Phone push is configured." if ntfy else "Phone push (ntfy) isn't set up."),
        evidence=[{"kind": "ntfy_url", "status": "set" if ntfy else "absent"},
                  {"kind": "reachability", "status": "not_measured"}],
        affects=["brief"], last_checked=now))
    sx = bool(getattr(s, "searxng_url", "")) if s else False
    items.append(ReadinessItem(
        id="searxng", domain="service", label="Local web search (SearXNG)",
        status=(CONFIGURED if sx else NOT_CONFIGURED),
        severity="info" if sx else "none",
        why=("Local web search is configured." if sx else "Local web search (SearXNG) isn't set up."),
        evidence=[{"kind": "searxng_url", "status": "set" if sx else "absent"},
                  {"kind": "reachability", "status": "not_measured"}],
        affects=["search"], last_checked=now))
    return items


def collect_memory_readiness() -> list[ReadinessItem]:
    from ..db import connect
    now = _now_iso()
    facts = 0
    try:
        with connect() as con:
            facts = con.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0]
    except Exception:
        facts = 0
    return [ReadinessItem(
        id="memory", domain="memory", label="Memory (things it knows about you)",
        status=(HAS_DATA if facts else CONFIGURED),
        severity="ok" if facts else "info",
        why=(f"Mongoose has remembered {facts} fact(s) about you." if facts
             else "No durable facts remembered yet, they accrue as you talk."),
        evidence=[{"kind": "facts", "value": facts}],
        affects=["brief", "surpriser"], last_checked=now,
        counts={"facts": facts} if facts else None, privacy="private")]


# --- the registry ------------------------------------------------------------


def _rtm_name() -> str:
    """The RTM's name from the site profile; a generic label if the profile cannot be read (readiness must
    still report, and an unreadable profile is surfaced on its own)."""
    try:
        from .. import profile as _profile
        return _profile.rtm_name()
    except Exception:
        return "RTM"

def collect_rtm_readiness() -> list[ReadinessItem]:
    """Surface the RTM's operational state in the unified readiness picture: arrears, bank credits
    waiting to allocate, payables due soon, and demands missing dispatch evidence. This makes the
    block part of the single 'what needs me' view rather than a separate silo. Read-only: it reports
    figures, it never sends or pays. If there are no flats yet, RTM is simply not set up, no noise."""
    items: list[ReadinessItem] = []
    try:
        from . import rtm_management as M
        flats = M.list_flats()
        if not flats:
            return []  # RTM not in use on this install; stay quiet
        dash = M.management_dashboard()
        actions = [a for a in dash.get("next_actions", []) if a.get("type") != "ok"]
        pos = dash.get("status", {})
        if not actions:
            items.append(ReadinessItem(
                id="rtm_management", domain="service", label=f"{_rtm_name()} RTM",
                status=READY, severity="ok",
                why="Demand register and ledger current; nothing needs you right now.",
                affects=["voice", "approvals"], privacy="private",
                next_action=_act("Open RTM dashboard", "command", command="mongoose rtm manage dashboard"),
                counts={"arrears": pos.get("arrears_outstanding", "\u00a30.00")},
                last_checked=_now_iso()))
            return items
        # map each action type to a readiness item
        sev_for = {"high": "warning", "medium": "info", "low": "info"}
        for a in actions:
            items.append(ReadinessItem(
                id=f"rtm_{a.get('type', 'action')}", domain="service",
                label=f"{_rtm_name()}: {a.get('type', 'action').replace('_', ' ')}",
                status=DEGRADED if a.get("urgency") == "high" else READY,
                severity=sev_for.get(a.get("urgency", "low"), "info"),
                why=a.get("title", ""), affects=["voice", "approvals"], privacy="private",
                next_action=_act("Open in RTM", "command", command=a.get("action", "mongoose rtm manage dashboard")),
                last_checked=_now_iso()))
        # Surface the RTM's real-data caveats (deed verification, wording-to-remove) as
        # readiness signals , things that aren't operational actions but should be visible before
        # relying on the data. Only the warning-level ones, to avoid noise.
        try:
            from . import rtm_example_court as PC
            for c in PC.caveats():
                if c.get("severity") == "warning":
                    items.append(ReadinessItem(
                        id=f"rtm_caveat_{c['code'].lower()}", domain="service",
                        label=f"{_rtm_name()}: {c['code'].replace('_', ' ').lower()}",
                        status=DEGRADED, severity="warning", why=c["detail"][:200],
                        affects=["approvals"], privacy="private", last_checked=_now_iso()))
        except Exception:
            pass
    except Exception as exc:
        items.append(ReadinessItem(
            id="rtm_management", domain="service", label=f"{_rtm_name()} RTM",
            status=UNKNOWN, severity="info",
            why=f"Couldn't read RTM figures: {exc}", privacy="private", last_checked=_now_iso()))
    return items


def collect_remote_access_readiness() -> list[ReadinessItem]:
    """Dynamic DNS + TLS, the 'can I be reached from outside, securely' picture. Previously these
    lived only in their own modules (ddns.py, acme_tls.py) and never made it into the unified readiness
    view, so a user couldn't see at a glance whether remote access was set up. Defensive: never raises;
    a probe failure yields an UNKNOWN item rather than taking down collect_all().

    Both halves are config/state reads (no outbound network call here), so status reflects what's
    configured and what local state says (last published IP, cert presence + expiry). Live external
    reachability is environment-dependent and stated honestly as not measured."""
    now = _now_iso()
    items: list[ReadinessItem] = []

    # --- DDNS / remote-address half ---
    try:
        from ..ddns import status as _ddns_status
        d = _ddns_status()
        provider = d.get("provider") or ""
        kind = d.get("kind") or ""
        configured = bool(d.get("configured"))
        enabled = bool(d.get("enabled"))
        last_ip = d.get("last_ip")
        if not provider:
            items.append(ReadinessItem(
                id="ddns", domain="remote", label="Dynamic DNS / remote address",
                status=NOT_CONFIGURED, severity="none",
                why="No dynamic DNS or VPN remote-access provider is set up (optional). Mongoose works "
                    "fully on your home network without it.",
                evidence=[{"kind": "provider", "status": "absent"}],
                affects=[], last_checked=now,
                next_action=_act("Set up remote access", "manual_external",
                                 command="mongoose ddns setup")))
        else:
            # A provider is chosen. Decide how usable it is.
            if not configured:
                status, sev = CONFIGURED, "warning"
                why = (f"{d.get('provider_name') or provider} is selected but not fully configured "
                       f"(missing credentials or the {kind} client). Remote access won't work yet.")
                act = _act("Finish remote-access setup", "manual_external", command="mongoose ddns setup")
            elif not enabled:
                status, sev = CONFIGURED, "info"
                why = (f"{d.get('provider_name') or provider} is configured but not enabled, so the "
                       f"address isn't being kept up to date.")
                act = _act("Enable dynamic DNS", "manual_external", command="mongoose ddns enable")
            else:
                status, sev = READY, "ok"
                why = (f"{d.get('provider_name') or provider} is configured and enabled"
                       + (f"; last published address {last_ip}." if last_ip else "."))
                act = None
            # If this provider needs a router port-forward, surface Mongoose's own friendly, filled-in
            # router card as the next action , so the user hears the setup from Mongoose (warmly, with
            # the exact numbers) instead of from a bare table elsewhere.
            if d.get("needs_portforward"):
                try:
                    from ..ddns import port_forward_card
                    card = port_forward_card()
                    if card.get("needed"):
                        act = _act("Set up the router port-forward", "instruction",
                                   command=card["speech"])
                except Exception:
                    pass
            items.append(ReadinessItem(
                id="ddns", domain="remote", label="Dynamic DNS / remote address",
                status=status, severity=sev, why=why,
                evidence=[{"kind": "provider", "status": provider},
                          {"kind": "kind", "status": kind},
                          {"kind": "configured", "status": "yes" if configured else "no"},
                          {"kind": "enabled", "status": "yes" if enabled else "no"},
                          {"kind": "last_ip", "status": last_ip or "none"},
                          {"kind": "needs_portforward",
                           "status": "yes" if d.get("needs_portforward") else "no"},
                          {"kind": "external_reachability", "status": "not_measured"}],
                affects=[], last_checked=now, next_action=act))
    except Exception as exc:
        items.append(ReadinessItem(
            id="ddns", domain="remote", label="Dynamic DNS / remote address",
            status=UNKNOWN, severity="warning",
            why=f"Couldn't determine remote-address status: {exc}", last_checked=now))

    # --- TLS / certificate half ---
    try:
        from ..acme_tls import certificate_status as _cert_status
        c = _cert_status()
        present = bool(c.get("present"))
        host = c.get("host") or ""
        expires = c.get("expires") or ""
        days_left = None
        if present and expires:
            try:
                from datetime import datetime, timezone
                # openssl notAfter, e.g. 'Sep  3 12:00:00 2026 GMT'
                exp = datetime.strptime(expires.strip(), "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=timezone.utc)
                days_left = (exp - datetime.now(timezone.utc)).days
            except Exception:
                days_left = None
        if not present:
            items.append(ReadinessItem(
                id="tls_cert", domain="remote", label="HTTPS certificate",
                status=NOT_CONFIGURED, severity="none",
                why=("No TLS certificate is installed (optional). Needed only for HTTPS remote access "
                     "or browser mic over a public address."
                     + (f" Target host: {host}." if host else "")),
                evidence=[{"kind": "certificate", "status": "absent"},
                          {"kind": "host", "status": host or "unset"}],
                affects=["voice"], last_checked=now,
                next_action=_act("Set up an HTTPS certificate", "manual_external",
                                 command="mongoose tls install-certbot")))
        else:
            # Cert present , judge by expiry.
            if days_left is not None and days_left < 0:
                status, sev = FAILING, "critical"
                why = f"The HTTPS certificate for {host or 'this host'} has EXPIRED ({expires})."
                act = _act("Renew the certificate", "manual_external", command="mongoose tls renew")
            elif days_left is not None and days_left <= 14:
                status, sev = DEGRADED, "warning"
                why = (f"The HTTPS certificate for {host or 'this host'} expires in {days_left} day(s) "
                       f"({expires}); renew soon.")
                act = _act("Renew the certificate", "manual_external", command="mongoose tls renew")
            else:
                status, sev = READY, "ok"
                why = (f"HTTPS certificate is installed for {host or 'this host'}"
                       + (f", valid for ~{days_left} more day(s)." if days_left is not None
                          else f" (expires {expires})." if expires else "."))
                act = None
            items.append(ReadinessItem(
                id="tls_cert", domain="remote", label="HTTPS certificate",
                status=status, severity=sev, why=why,
                evidence=[{"kind": "certificate", "status": "present"},
                          {"kind": "host", "status": host or "unset"},
                          {"kind": "expires", "status": expires or "unknown"},
                          {"kind": "days_left",
                           "status": str(days_left) if days_left is not None else "unknown"}],
                affects=["voice"], last_checked=now, next_action=act))
    except Exception as exc:
        items.append(ReadinessItem(
            id="tls_cert", domain="remote", label="HTTPS certificate",
            status=UNKNOWN, severity="warning",
            why=f"Couldn't determine certificate status: {exc}", last_checked=now))

    return items


def collect_all() -> list[ReadinessItem]:
    """Run every collector. Each is defensive (never raises); a broken collector yields an
    `unknown` item rather than taking down the whole readiness picture."""
    collectors = [
        collect_core_readiness, collect_scheduler_readiness, _mail_calendar_items,
        collect_files_readiness, collect_messaging_readiness, collect_device_readiness,
        collect_voice_readiness, collect_home_readiness, collect_service_readiness,
        collect_memory_readiness, collect_rtm_readiness, collect_remote_access_readiness,
    ]
    items: list[ReadinessItem] = []
    for c in collectors:
        try:
            items.extend(c())
        except Exception as exc:
            items.append(ReadinessItem(
                id=f"collector_{c.__name__}", domain="core", label=c.__name__,
                status=UNKNOWN, severity="warning",
                why=f"Couldn't determine this: {exc}", last_checked=_now_iso()))
    return items


def readiness_report() -> dict[str, Any]:
    """The full readiness picture as a dict (for the API / Cockpit). Includes an overall
    headline that's honest about whether the core is usable. This runs the collectors LIVE;
    for the hot path (voice/phone) prefer cached_readiness(), which reads a stored snapshot."""
    items = collect_all()
    by_status: dict[str, int] = {}
    for it in items:
        by_status[it.status] = by_status.get(it.status, 0) + 1

    core = [it for it in items if it.domain == "core"]
    core_ok = all(it.status in (READY, HAS_DATA) for it in core) if core else False
    attention = [it for it in items if it.needs_attention]
    critical = [it for it in attention if it.severity == "critical"]

    if not core_ok or critical:
        overall = "not_ready" if not core_ok else "degraded"
    elif attention:
        overall = "degraded"
    else:
        overall = "ready"

    return {
        "overall": overall,
        "generated_at": _now_iso(),
        "counts_by_status": by_status,
        "attention_count": len(attention),
        "items": [it.to_dict() for it in items],
    }


# --- cached layer ------------------------------------------------------------
# Voice/phone must not run live collectors on every query (some touch the DB, the filesystem,
# settings). The report is cheap, but the right design is a refreshed SNAPSHOT: a scheduler job
# refreshes it, the user can force a refresh ("check now"), and the hot paths read the cache.

_CACHE_KEY = "readiness_snapshot"
_CACHE_TTL_SECONDS = 900   # 15 min: stale-but-fine for a status glance; refresh job runs more often


def refresh_readiness() -> dict[str, Any]:
    """Recompute the readiness report and store it as the cached snapshot. Called by the
    scheduler and on demand. Returns the fresh report."""
    report = readiness_report()
    try:
        import json
        from ..db import set_app_state
        set_app_state(_CACHE_KEY, json.dumps(report))
    except Exception:
        pass
    return report


def cached_readiness(max_age_seconds: int = _CACHE_TTL_SECONDS) -> dict[str, Any]:
    """The readiness snapshot for hot paths (voice/phone). Returns the stored snapshot if it's
    fresh enough; otherwise recomputes and caches. Never raises, falls back to a live report."""
    try:
        import json
        from ..db import get_app_state
        raw = get_app_state(_CACHE_KEY, "")
        if raw:
            snap = json.loads(raw)
            age = _age_hours(snap.get("generated_at"))
            if age is not None and age * 3600 <= max_age_seconds:
                snap["cached"] = True
                return snap
    except Exception:
        pass
    return refresh_readiness()


# --- shared helpers (mirrors liveness, kept local to avoid import cycles) ----

def _age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return None


def _latest(con, table: str, col: str) -> str | None:
    try:
        row = con.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
        return row[0] if row else None
    except Exception:
        return None
