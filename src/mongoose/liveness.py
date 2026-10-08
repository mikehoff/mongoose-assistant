from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Liveness / rot detection.
#
# The single biggest real-world risk to a self-hosted, one-operator assistant is NOT a bug:
# it's silent decay. Tokens expire, a WhatsApp Web session drops, email quietly stops
# syncing after an OS update, a scheduler job dies. Nothing crashes; the system just goes
# stale while still looking up. By the time you notice, you've been getting a degraded
# picture for a week and don't know which parts to trust.
#
# This makes that decay VISIBLE and SPECIFIC: for each live subsystem, how fresh is it, and
# if it's stale, what's the likely cause and the one action to fix it. The goal is that
# neglect becomes recoverable (you see "email last synced 6 days ago -> re-auth Google")
# instead of catastrophic (you trusted a stale brief without knowing).
#
# Everything is computed from real timestamps. A subsystem you don't use is reported as
# "not configured", never as broken.

# How stale is too stale, per subsystem (hours). Tuned to the expected cadence: email/cal
# sync often; files less so. These are "should have refreshed by now" thresholds.
_STALE_HOURS = {
    "email": 12,
    "calendar": 12,
    "whatsapp": 48,      # sessions drop; 2 days quiet is worth a look
    "files": 72,
    "scheduler": 26,     # the daily jobs should have run within ~a day
}

OK = "ok"
STALE = "stale"
UNUSED = "not_configured"


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


def _newest_ts(*values: str | None) -> str | None:
    best: tuple[float, str] | None = None
    for value in values:
        age = _age_hours(value)
        if value and age is not None:
            # Smaller age means newer. Store negative age so max() picks the newest timestamp.
            item = (-age, value)
            if best is None or item > best:
                best = item
    return best[1] if best else None


def _latest_successful_job(con, names: tuple[str, ...]) -> str | None:
    try:
        placeholders = ",".join("?" for _ in names)
        row = con.execute(
            f"SELECT MAX(last_run) FROM scheduler_jobs "
            f"WHERE name IN ({placeholders}) AND last_status='ok'",
            names,
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _latest_crawl_completion(con, kind: str | None = None) -> str | None:
    try:
        if kind:
            row = con.execute(
                "SELECT MAX(last_completed) FROM crawl_sources WHERE kind=? AND last_status IN ('done','partial')",
                (kind,),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT MAX(last_completed) FROM crawl_sources WHERE last_status IN ('done','partial')"
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _human_age(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 0:
        # a future timestamp (clock skew) , don't render negative time
        return "just now"
    if hours < 1:
        return f"{int(hours * 60)} min ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)} days ago"


def _check(name: str, age_h: float | None, configured: bool, fix: str) -> dict[str, Any]:
    threshold = _STALE_HOURS.get(name)
    if not configured:
        state = UNUSED
        detail = "not set up"
    elif age_h is None:
        state = STALE
        detail = "configured but no data yet"
    elif threshold is not None and age_h > threshold:
        state = STALE
        detail = f"last activity {_human_age(age_h)} (expected within {threshold}h)"
    else:
        state = OK
        detail = f"fresh ({_human_age(age_h)})"
    out = {"subsystem": name, "state": state, "detail": detail, "age_hours": age_h}
    if state == STALE:
        out["fix"] = fix
    return out


def _scheduler_installed() -> bool:
    """Whether the scheduler is actually set up as a background service/agent. On macOS that's a
    launchd LaunchAgent; we also count a recorded 'service installed' marker. A never-installed
    scheduler is UNUSED (not broken), so this must not just return True."""
    try:
        from pathlib import Path
        # launchd agent installed by `mongoose service`?
        la = Path.home() / "Library" / "LaunchAgents"
        if la.exists():
            for p in la.glob("*mongoose*scheduler*.plist"):
                return True
            for p in la.glob("*mongoose*.plist"):
                # any mongoose agent that runs the scheduler counts
                try:
                    if "scheduler" in p.read_text().lower():
                        return True
                except Exception:
                    pass
        # a recorded marker (set when the service is installed) is also sufficient
        from .db import get_app_state
        if get_app_state("scheduler_service_installed"):
            return True
    except Exception:
        pass
    return False


def _microsoft_token_present() -> bool:
    try:
        from .oauth import microsoft_token_present
        return bool(microsoft_token_present())
    except Exception:
        return False


def _google_token_present() -> bool:
    try:
        from .oauth import google_token_present
        return bool(google_token_present())
    except Exception:
        return False


def _email_configured() -> bool:
    try:
        from .settings import get_settings
        s = get_settings()
        return bool(_google_token_present()
                    or getattr(s, "imap_host", "")
                    or _microsoft_token_present())
    except Exception:
        return False


def _calendar_configured() -> bool:
    try:
        return bool(_google_token_present() or _microsoft_token_present())
    except Exception:
        return False


def _token_health() -> list[dict[str, Any]]:
    """Credential freshness, the most common silent-decay cause. A token that's expired
    with no refresh token means sync is dead until you re-auth."""
    out: list[dict[str, Any]] = []
    try:
        from .settings import get_settings
        import os
        s = get_settings()
        gp = getattr(s, "google_token_path", "") or ""
        if gp and os.path.exists(gp):
            try:
                from google.oauth2.credentials import Credentials  # type: ignore
                creds = Credentials.from_authorized_user_file(gp)
                if creds.expired and not creds.refresh_token:
                    out.append({"subsystem": "google_token", "state": STALE,
                                "detail": "expired with no refresh token",
                                "fix": "run: mongoose authorize google"})
                elif creds.expired:
                    out.append({"subsystem": "google_token", "state": OK,
                                "detail": "expired but auto-refreshable"})
                else:
                    out.append({"subsystem": "google_token", "state": OK, "detail": "valid"})
            except Exception:
                # can't parse it -> treat as needing attention, not as fine
                out.append({"subsystem": "google_token", "state": STALE,
                            "detail": "token file present but unreadable",
                            "fix": "run: mongoose authorize google"})
    except Exception:
        pass
    return out


def _whatsapp_session() -> dict[str, Any] | None:
    """WAHA sessions drop on a cadence Meta controls. If WhatsApp is set up, surface whether
    the session is actually WORKING, because a dropped session looks like 'no new messages'."""
    try:
        from .settings import get_settings
        s = get_settings()
        if not (getattr(s, "waha_url", "") or getattr(s, "whatsapp_enabled", False)):
            return None
    except Exception:
        return None
    try:
        from .services.whatsapp import session_status
        st = session_status()
        state = (st or {}).get("status", "")
        if state == "WORKING":
            return {"subsystem": "whatsapp_session", "state": OK, "detail": "session WORKING"}
        return {"subsystem": "whatsapp_session", "state": STALE,
                "detail": f"session is {state or 'not connected'} (not WORKING)",
                "fix": "re-link: open http://127.0.0.1:3000 and scan with WhatsApp > Linked Devices"}
    except Exception:
        return {"subsystem": "whatsapp_session", "state": STALE,
                "detail": "WhatsApp configured but session state unreadable",
                "fix": "check the WAHA container: docker compose -f docker-compose.whatsapp.yml ps"}


def liveness() -> dict[str, Any]:
    """The honest answer to 'is this system actually alive, or quietly rotting?'. Per
    subsystem: fresh / stale / not-configured, with the fix for anything stale. Never
    raises."""
    from .db import connect
    checks: list[dict[str, Any]] = []

    # Core (the local brain) first: if Ollama isn't reachable, NOTHING that needs the model works,
    # so the liveness headline must not cheerfully say "everything fresh". This is what made the
    # cockpit show "core not ready" and "everything fresh and running" at the same time , liveness
    # didn't know about the brain. Now it does.
    core_ok = True
    core_detail = ""
    try:
        from .wizard import _core_status
        cs = _core_status()
        core_ok = bool(cs.get("ready"))
        if not core_ok:
            bad = [i for i in cs.get("items", []) if not i.get("ok")]
            core_detail = (bad[0].get("detail") if bad and bad[0].get("detail")
                           else "the local model (Ollama) isn't reachable")
    except Exception:
        core_ok = True  # don't manufacture a failure if the probe itself errors
    checks.append({"subsystem": "core (local brain)",
                   "state": OK if core_ok else STALE,
                   "detail": "running" if core_ok else core_detail,
                   **({} if core_ok else {"fix": "start Ollama and pull the chat model: ollama serve, then ollama pull <model>"})})

    try:
        with connect() as con:
            # Freshness means either new data arrived OR the sync job/crawler successfully checked and
            # found nothing new. After 2-3 weeks, quiet inboxes/calendars/file folders should not be
            # misreported as stale merely because there was no new row to index.
            email_ts = _newest_ts(_latest(con, "email_messages", "indexed_at"),
                                  _latest_successful_job(con, ("sync_email",)),
                                  _latest_crawl_completion(con, "imap"))
            cal_ts = _newest_ts(_latest(con, "events", "last_seen"),
                                _latest(con, "tasks", "last_seen"),
                                _latest_successful_job(con, ("sync_tasks",)))
            wa_ts = _latest(con, "whatsapp_messages", "indexed_at")
            file_ts = _newest_ts(_latest(con, "files", "indexed_at"),
                                 _latest_successful_job(con, ("reindex_files",)),
                                 _latest_crawl_completion(con, "files"))
            email_age = _age_hours(email_ts)
            cal_age = _age_hours(cal_ts)
            wa_age = _age_hours(wa_ts)
            file_age = _age_hours(file_ts)
    except Exception:
        email_age = cal_age = wa_age = file_age = None

    checks.append(_check("email", email_age, _email_configured(),
                         "re-auth: mongoose authorize google (or check IMAP creds), then mongoose sync-gmail"))
    checks.append(_check("calendar", cal_age, _calendar_configured(),
                         "re-auth: mongoose authorize google, then sync"))
    # whatsapp data-freshness only matters if it's set up
    wa_configured = _whatsapp_session() is not None
    checks.append(_check("whatsapp", wa_age, wa_configured,
                         "re-link the WhatsApp session (see whatsapp_session below)"))
    # files: only "configured" if the user has actually set index roots. On a fresh install with no
    # folders chosen, file search isn't set up , that's UNUSED (benign), not "configured but stale".
    import os as _os
    file_roots = (_os.getenv("MONGOOSE_INDEX_ROOTS", "") or "").strip()
    checks.append(_check("files", file_age, bool(file_roots),
                         "choose folders to index in Setup, then run: mongoose reindex-files"))

    # scheduler liveness: prefer the loop heartbeat over job timestamps. A scheduler with only daily
    # jobs can be perfectly alive even when no job has run in the last few hours; conversely a single
    # recent job from a manual --once run should not prove the long-running loop is alive forever.
    try:
        from .db import get_app_state
        sched_age = _age_hours(get_app_state("scheduler_heartbeat"))
    except Exception:
        sched_age = None
    # scheduler is only "configured" once it's actually installed as a service. If it was never
    # installed, it's UNUSED, not "configured but no data". Treat the presence of a service/agent as
    # the configured signal; a never-installed scheduler shouldn't read as broken on a fresh install.
    sched_configured = _scheduler_installed()
    checks.append(_check("scheduler", sched_age, sched_configured,
                         "install the scheduler service: mongoose service"))

    # tokens + whatsapp session are their own checks
    checks.extend(_token_health())
    wa = _whatsapp_session()
    if wa:
        checks.append(wa)

    # The in-process runtime watchdog: surface its heartbeat so its background self-healing is
    # observable (and so a watchdog that has stopped beating shows up as stale rather than silently).
    try:
        from .services.runtime_watchdog import current_status
        ws = current_status()
        if ws is not None:
            beats = ws.get("beats", 0)
            restarts = ws.get("scheduler_restarts", 0)
            lag = ws.get("loop_lag_ms")
            detail = f"watching background tasks (beat {beats}"
            if restarts:
                detail += f", repaired scheduler {restarts}x"
            if isinstance(lag, (int, float)):
                detail += f", loop lag {lag:.0f}ms"
            detail += ")"
            checks.append({"subsystem": "runtime watchdog", "state": OK, "detail": detail, "fix": ""})
    except Exception:
        pass

    # Resource sentinel: surface live memory/loop-lag pressure and whether it has had to auto-relieve.
    try:
        from .services.resource_sentinel import current_status as sentinel_status
        ss = sentinel_status()
        if ss is not None:
            sig = ss.get("signals", {}) or {}
            bits = []
            rss = sig.get("proc_rss_mb")
            if isinstance(rss, (int, float)):
                bits.append(f"RSS {rss:.0f}MB")
            sysf = sig.get("sys_available_fraction")
            if isinstance(sysf, (int, float)):
                bits.append(f"system free {sysf:.0%}")
            relief_n = ss.get("relief_count", 0)
            if relief_n:
                bits.append(f"auto-relieved {relief_n}x")
            action = ss.get("action", "observe")
            detail = "resource pressure: " + (", ".join(bits) if bits else "measuring")
            if action not in ("observe",):
                detail += f" [{action}]"
            checks.append({"subsystem": "resource sentinel", "state": OK, "detail": detail, "fix": ""})
    except Exception:
        pass

    stale = [c for c in checks if c["state"] == STALE]
    used = [c for c in checks if c["state"] != UNUSED]
    healthy = not stale
    core_down = any(c["subsystem"].startswith("core") and c["state"] == STALE for c in checks)
    if core_down:
        headline = ("The local brain isn't running, so Mongoose can't answer yet. "
                    "Start Ollama and pull the chat model, then this clears.")
    elif healthy:
        headline = "Everything that's set up is fresh and running."
    else:
        headline = (f"{len(stale)} subsystem(s) have gone stale, the brief may be working "
                    "from old data. See the fixes below.")
    return {
        "healthy": healthy,
        "checks": checks,
        "stale_count": len(stale),
        "headline": headline,
        "summary": f"{len(used) - len(stale)}/{len(used)} live subsystems fresh",
    }


def format_liveness(rep: dict[str, Any]) -> str:
    out = [rep.get("headline", ""), ""]
    sym = {OK: "OK   ", STALE: "STALE", UNUSED: "-    "}
    for c in rep.get("checks", []):
        # Degrade gracefully on a malformed check: the liveness view is a TRUST surface, so one
        # missing field must never crash the whole picture (better a "?" than no health report).
        state = c.get("state", "?")
        subsystem = c.get("subsystem", "?")
        detail = c.get("detail", "")
        line = f"  [{sym.get(state, '?')}] {subsystem}: {detail}"
        out.append(line)
        if c.get("fix"):
            out.append(f"          -> {c['fix']}")
    return "\n".join(out)
