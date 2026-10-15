from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable, Optional

from ..db import connect, utc_now_iso, audit, get_app_state, set_app_state
from ..settings import get_settings

# Mongoose's background scheduler: the thing that makes it run on its own instead
# of only when you type a command. It runs a small set of recurring JOBS, the
# daily brief, periodic email sync, periodic file reindex, on an interval, and
# persists each job's last-run time so a restart doesn't double-fire or forget.
#
# Design:
#  - One async loop; it wakes every `tick` seconds, asks each due job to run.
#  - "Due" is decided two ways: interval jobs (every N minutes) and daily jobs
#    (once per local day at/after a target hour:minute). Both are checked against
#    the persisted last_run, so the loop is cheap and restart-safe.
#  - Jobs are best-effort and isolated: one failing job is logged and never stops
#    the loop or the others.
#  - It pushes the daily brief to the phone (ntfy) and the bus, so a scheduled run
#    actually reaches you.

JobFn = Callable[[], Awaitable[dict[str, Any]]]


def _ensure_table() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_jobs (
                name TEXT PRIMARY KEY,
                last_run TEXT,
                last_status TEXT,
                last_detail TEXT
            )
            """
        )


def _get_last_run(name: str) -> datetime | None:
    with connect() as con:
        try:
            row = con.execute("SELECT last_run FROM scheduler_jobs WHERE name = ?", (name,)).fetchone()
        except Exception:
            # table not created yet (scheduler hasn't started) , treat as never-run
            return None
    if not row or not row["last_run"]:
        return None
    try:
        dt = datetime.fromisoformat(row["last_run"].replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _record_run(name: str, status: str, detail: str = "") -> None:
    with connect() as con:
        con.execute(
            """
            INSERT INTO scheduler_jobs(name, last_run, last_status, last_detail)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run,
                last_status=excluded.last_status, last_detail=excluded.last_detail
            """,
            (name, utc_now_iso(), status, detail[:500]),
        )


_DISABLED_JOBS_KEY = "scheduler_disabled_jobs_v1"
_MEMORY_RELIEF_HOURS = 0.5  # 30 min. A transient spike must not blackout the assistant for hours.
_MEMORY_RELIEF_ACTIONS = {"free_memory", "memory", "quiet", "relieve_memory", "free_memory_all"}

# Jobs that are useful but safe to pause when the owner says something like
# "stop scheduler jobs to free memory". This intentionally leaves safety/continuity
# jobs (alerts, DDNS, readiness, backups, v3 reconcile, RTM operational refresh) running.
# HEAVY jobs: genuine memory/CPU hogs. These are paused FIRST under pressure and are the only thing
# paused under mild pressure. Pausing these is nearly free , they are batch/background work.
_HEAVY_DEFERRABLE = {
    "benchmark", "benchmark_reveal", "tuning_check", "reindex_files",
    "rtm_build_daily", "rtm_build", "rtm_docs_recorrelate", "rtm_docs_analyse",
}
# PROACTIVE jobs: the assistant's whole reason for existing (keeping the brief current, surfacing
# surprisers, proactive messages, delight). Pausing THESE makes the product feel dead, so they are only
# paused under SEVERE, sustained pressure , never as the first or casual relief step. On a Mac where
# macOS keeps "available" memory chronically low, this is the difference between a working assistant and
# a silent one.
_PROACTIVE_DEFERRABLE = {
    "delight_topup", "surpriser", "self_reflection", "sync_email",
    "sync_tasks", "sync_checkpoint_1", "sync_checkpoint_2", "sync_checkpoint_3",
}
# Backwards-compatible union (existing callers that pause "the deferrable set" still work). The tiers
# above let the sentinel choose WHICH tier to pause based on how severe the pressure actually is.
_DEFERRABLE_FOR_MEMORY = _HEAVY_DEFERRABLE | _PROACTIVE_DEFERRABLE

# A scheduler job that loops forever must not freeze the whole scheduler. Each due job is run in a
# worker thread with a hard wait budget. Python cannot safely kill a runaway thread, so timed-out jobs
# are tracked and skipped on later ticks until their thread finishes; this prevents one bad provider
# from spawning an unbounded pile of stuck workers.
_ACTIVE_JOB_TASKS: dict[str, asyncio.Task] = {}
_DEFAULT_JOB_TIMEOUT_SECONDS = 240.0
_JOB_TIMEOUT_SECONDS: dict[str, float] = {
    "benchmark": 3600.0,
    "rtm_build": 1800.0,
    "rtm_build_daily": 900.0,
    "backup": 900.0,
    "brain_snapshot": 600.0,
    "reindex_files": 900.0,
    "sync_email": 420.0,
    "sync_tasks": 420.0,
    "knowledge_research": 900.0,  # up to 6 SearXNG+model research passes + a synthesis pass, serial
    "rtm_docs_analyse": 600.0,  # up to 10 docs of Vision/model/subprocess work per run
    "rtm_queue_sync": 600.0,  # drains sync_wp_queues; each WP call is 20-60s, a backlog can stack up
    "sync_checkpoint_1": 300.0,
    "sync_checkpoint_2": 300.0,
    "sync_checkpoint_3": 300.0,
}


def _job_timeout_seconds(name: str) -> float:
    import os as _os
    env_specific = _os.getenv(f"MONGOOSE_JOB_TIMEOUT_{name.upper()}_S")
    env_default = _os.getenv("MONGOOSE_JOB_TIMEOUT_S")
    raw = env_specific if env_specific not in (None, "") else env_default
    try:
        if raw not in (None, ""):
            return max(1.0, float(raw))
    except (TypeError, ValueError):
        pass
    return _JOB_TIMEOUT_SECONDS.get(name, _DEFAULT_JOB_TIMEOUT_SECONDS)


def _run_job_coro_in_thread(fn: JobFn) -> Any:
    """Execute an async scheduler job in a private event loop inside a worker thread.

    This keeps synchronous/blocking provider code inside an `async def` from blocking FastAPI's main
    event loop. If the job is genuinely async, it still gets a clean private loop.
    """
    import inspect as _inspect
    res = fn()
    if _inspect.isawaitable(res):
        return asyncio.run(res)
    return res


def active_job_status() -> list[dict[str, Any]]:
    """Current scheduler worker tasks, including tasks still running after a timeout."""
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for name, task in list(_ACTIVE_JOB_TASKS.items()):
        started = getattr(task, "_mongoose_started_at", None)
        timeout = getattr(task, "_mongoose_timeout_s", None)
        started_iso = getattr(task, "_mongoose_started_iso", None)
        if task.done():
            _ACTIVE_JOB_TASKS.pop(name, None)
            continue
        age = None
        if isinstance(started, datetime):
            age = (now - started).total_seconds()
        out.append({
            "job": name,
            "running": True,
            "started_at": started_iso,
            "age_seconds": round(age, 1) if isinstance(age, (int, float)) else None,
            "timeout_seconds": timeout,
            "over_timeout": bool(isinstance(age, (int, float)) and isinstance(timeout, (int, float)) and age > timeout),
        })
    return out


async def _run_job_with_timeout(name: str, fn: JobFn) -> dict[str, Any]:
    existing = _ACTIVE_JOB_TASKS.get(name)
    if existing is not None and not existing.done():
        # Previous thread is still executing. Do not pile up another copy of the same job.
        return {"ok": False, "timeout": True, "still_running": True,
                "error": "previous run is still active after its timeout",
                "active": active_job_status()}
    if existing is not None and existing.done():
        _ACTIVE_JOB_TASKS.pop(name, None)

    timeout = _job_timeout_seconds(name)
    started = datetime.now(timezone.utc)
    task = asyncio.create_task(asyncio.to_thread(_run_job_coro_in_thread, fn))
    setattr(task, "_mongoose_started_at", started)
    setattr(task, "_mongoose_started_iso", started.isoformat())
    setattr(task, "_mongoose_timeout_s", timeout)
    _ACTIVE_JOB_TASKS[name] = task
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        # The worker thread may still be running; keep it in _ACTIVE_JOB_TASKS so future ticks skip
        # this job rather than spawning another stuck copy.
        return {"ok": False, "timeout": True, "error": f"scheduler job timed out after {timeout:g}s"}
    finally:
        if task.done():
            _ACTIVE_JOB_TASKS.pop(name, None)


def _resource_pressure_snapshot() -> dict[str, Any]:
    try:
        from .resource_guard import assess_runtime_pressure
        return assess_runtime_pressure(include_process_table=False)
    except Exception as exc:
        return {"ok": True, "level": "unknown", "warnings": [], "error": str(exc)[:160]}


def _skip_deferrable_under_pressure(name: str, pressure: dict[str, Any],
                                    spec: tuple | None = None, now: datetime | None = None) -> bool:
    if name not in _DEFERRABLE_FOR_MEMORY:
        return False
    # Window-protected jobs (e.g. benchmark, 5-8am) are exempt from memory-pressure gating WHILE inside
    # their window. The whole point of the quiet window is to run heavy model work then; the RSS/loop
    # pressure such a job creates is intended, not a symptom. Gating it on its own pressure would let the
    # benchmark skip itself out of the very slot reserved for it. A truly stuck run is still bounded by
    # the per-job timeout and, ultimately, the process watchdog , not by pausing it here.
    if spec and len(spec) >= 3 and spec[0] == "window":
        try:
            if _window_in_hours(int(spec[1]), int(spec[2]), now=now):
                return False
        except Exception:
            pass
    try:
        from .resource_guard import should_skip_deferrable_work
        return should_skip_deferrable_work(pressure)
    except Exception:
        return False


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load_disabled_jobs() -> dict[str, dict[str, Any]]:
    raw = get_app_state(_DISABLED_JOBS_KEY, "{}") or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    changed = False
    now = datetime.now(timezone.utc)
    for name, meta in data.items():
        if not (isinstance(name, str) and isinstance(meta, dict)):
            changed = True
            continue
        auto_resume_at = _parse_dt(meta.get("auto_resume_at"))
        if auto_resume_at and auto_resume_at <= now:
            changed = True
            continue
        out[name] = meta
    if changed:
        try:
            _save_disabled_jobs(out)
        except Exception:
            pass
    return out


def _save_disabled_jobs(disabled: dict[str, dict[str, Any]]) -> None:
    set_app_state(_DISABLED_JOBS_KEY, json.dumps(disabled, sort_keys=True))


def disabled_jobs() -> dict[str, dict[str, Any]]:
    """Return the durable owner-controlled scheduler pause list.

    This is intentionally separate from the registry: the code still knows every
    job that exists, but `run_due_jobs()` and `job_status()` honour the owner's
    current enable/disable choices across restarts.
    """
    return _load_disabled_jobs()


def _normalise_job_names(jobs: Any, *, action: str) -> list[str]:
    registry = _build_registry()
    if jobs in (None, "", [], ()):  # default only for the explicit memory relief action
        if action in {"free_memory", "memory", "quiet", "relieve_memory", "free_memory_all"}:
            # Default relief pauses HEAVY jobs only (benchmark, reindex, rtm builds) , the real hogs,
            # whose pause is nearly free. The PROACTIVE layer (sync, surprisers, delight) is the product's
            # whole point and stays running unless relief is explicitly the "all" tier for severe,
            # sustained pressure. On a Mac, "available" memory runs chronically low, so pausing proactive
            # jobs on every mild dip is what made the assistant feel dead.
            severe = action == "free_memory_all"
            target = _DEFERRABLE_FOR_MEMORY if severe else _HEAVY_DEFERRABLE
            out = []
            for name in registry:
                if name not in target:
                    continue
                spec = registry[name][0]
                if spec and len(spec) >= 3 and spec[0] == "window" and _window_in_hours(int(spec[1]), int(spec[2])):
                    continue
                out.append(name)
            return out
        return []
    if isinstance(jobs, str):
        raw = [j.strip() for j in jobs.replace(";", ",").split(",")]
    elif isinstance(jobs, (list, tuple, set)):
        raw = []
        for item in jobs:
            raw.extend(j.strip() for j in str(item).replace(";", ",").split(","))
    else:
        raw = [str(jobs).strip()]
    return [j for j in raw if j]


def scheduler_control(action: str, jobs: Any = None, *, reason: str = "", actor: str = "owner") -> dict[str, Any]:
    """Owner control plane for background jobs.

    Used by CLI/chat/UI surfaces through one implementation, so a chat request such
    as "pause the heavy scheduler jobs to free memory" has the same durable, audited
    effect as a future button. Unknown jobs are reported and no partial mutation is
    applied, because silently accepting a typo is worse than being annoying.
    """
    registry = _build_registry()
    disabled = _load_disabled_jobs()
    act = (action or "status").strip().lower().replace(" ", "_").replace("-", "_")
    if act in {"status", "list"}:
        return {"ok": True, "disabled": disabled, "jobs": job_status()}
    if act in {"stop", "pause", "disable", "suspend", "free_memory", "memory", "quiet", "relieve_memory", "free_memory_all"}:
        enable = False
    elif act in {"start", "resume", "enable"}:
        enable = True
    else:
        return {"ok": False, "error": f"unknown scheduler action {action!r}; use status, disable, enable, or free_memory"}

    names = _normalise_job_names(jobs, action=act)
    unknown = [name for name in names if name not in registry]
    if unknown:
        return {"ok": False, "error": "unknown scheduler job(s): " + ", ".join(unknown),
                "known_jobs": sorted(registry.keys())}
    if not names:
        return {"ok": False, "error": "no jobs specified"}

    changed: list[str] = []
    if enable:
        for name in names:
            if disabled.pop(name, None) is not None:
                changed.append(name)
    else:
        now = utc_now_iso()
        temporary_memory_relief = act in _MEMORY_RELIEF_ACTIONS
        auto_resume_at = (datetime.now(timezone.utc) + timedelta(hours=_MEMORY_RELIEF_HOURS)).isoformat() if temporary_memory_relief else None
        for name in names:
            disabled[name] = {
                "disabled_at": now,
                "disabled_by": actor or "owner",
                "reason": (reason or ("owner requested temporary memory relief" if temporary_memory_relief else "owner requested pause"))[:300],
            }
            if auto_resume_at:
                disabled[name]["auto_resume_at"] = auto_resume_at
                disabled[name]["temporary"] = True
            changed.append(name)
    _save_disabled_jobs(disabled)
    audit("scheduler", "job_control", True, route=act,
          payload={"jobs": names, "enabled": enable, "actor": actor or "owner", "reason": (reason or "")[:160]})
    return {
        "ok": True,
        "action": "enabled" if enable else "disabled",
        "changed": changed,
        "disabled": disabled,
        "note": ("Temporary memory-relief pauses auto-resume after " + str(_MEMORY_RELIEF_HOURS) +
                 " hours. Explicitly disabled jobs persist across restarts until resumed."),
    }


def _local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(get_settings().timezone))
    except Exception:
        return datetime.now(timezone.utc)


def _interval_due(name: str, minutes: int, now: datetime | None = None) -> bool:
    last = _get_last_run(name)
    if last is None:
        return True
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (ref - last) >= timedelta(minutes=minutes)


def _window_in_hours(start_hour: int, end_hour: int, now: datetime | None = None) -> bool:
    """True if the current LOCAL hour is within [start_hour, end_hour). Stateless (no run-today check),
    used to tell whether we are inside a protected window so its heavy job is exempt from pressure
    gating while it legitimately runs."""
    now_local = now or _local_now()
    return start_hour <= now_local.hour < end_hour


def _window_due(name: str, start_hour: int, end_hour: int, now: datetime | None = None) -> bool:
    """Due if the current local hour is within [start_hour, end_hour) AND the job hasn't run yet
    today. Used for the benchmark job, which must run only in the quiet early-morning window (e.g.
    5am-8am) so it never competes with the user's daytime use of the single local model."""
    now_local = now or _local_now()
    if not (start_hour <= now_local.hour < end_hour):
        return False
    last = _get_last_run(name)
    if last is None:
        return True
    try:
        from zoneinfo import ZoneInfo
        last_local = last.astimezone(ZoneInfo(get_settings().timezone))
    except Exception:
        last_local = last
    return last_local.date() < now_local.date()


def _daily_due(name: str, hour: int, minute: int, now: datetime | None = None) -> bool:
    """Due if we're at/after the target local time today and haven't run since the
    start of today (local). `now` (local) can be injected for simulation."""
    now_local = now or _local_now()
    target_passed = (now_local.hour, now_local.minute) >= (hour, minute)
    if not target_passed:
        return False
    last = _get_last_run(name)
    if last is None:
        return True
    try:
        from zoneinfo import ZoneInfo
        last_local = last.astimezone(ZoneInfo(get_settings().timezone))
    except Exception:
        last_local = last
    return last_local.date() < now_local.date()


def _weekly_due(name: str, weekday: int, hour: int, minute: int, now: datetime | None = None) -> bool:
    """Due if it's the target weekday (0=Mon..6=Sun) at/after the target local time, and we haven't
    run in the last ~6 days (so a weekly job fires once per week, not repeatedly that day). `now`
    (local) can be injected for simulation."""
    now_local = now or _local_now()
    if now_local.weekday() != weekday:
        return False
    if (now_local.hour, now_local.minute) < (hour, minute):
        return False
    last = _get_last_run(name)
    if last is None:
        return True
    try:
        from zoneinfo import ZoneInfo
        last_local = last.astimezone(ZoneInfo(get_settings().timezone))
    except Exception:
        last_local = last
    # ran already today, or within the last 6 days -> not due
    return (now_local.date() - last_local.date()).days >= 6


# --- the jobs ---------------------------------------------------------------

async def _job_daily_brief() -> dict[str, Any]:
    """Generate the cross-domain brief, drop it on the bus, and push it to the
    phone so a scheduled run reaches the user."""
    from .brief import generate_brief
    from .notifications import send_to_phone
    result = await generate_brief(post_to_bus=True)
    text = result.get("brief", "")
    # An ops-health problem (e.g. a silently failing off-machine backup) must LEAD the daily push, not be
    # buried under the brief body, so it reaches the owner even when the brief is long.
    try:
        from .ops_health import brief_health_line
        hl = brief_health_line()
    except Exception:
        hl = ""
    push_text = (hl + "\n\n" + text) if hl else text
    pushed = await send_to_phone("Daily brief", push_text[:300],
                                 priority=("high" if hl else "default"), tags="sunrise")
    return {"brief_len": len(text), "pushed": pushed.get("sent", False), "ops_warning": bool(hl)}


async def _job_delight_topup() -> dict[str, Any]:
    """Keep the delight cache full so delights scale to the hundreds/thousands without a model call per
    pull. Tops up both the owner ('general') and guest wells. Runs the model off the request path, on a
    relaxed interval. Best-effort; if no local model is up it simply does nothing this cycle."""
    try:
        from .delight_cache import top_up
        # top_up -> _generate_batch bails out (returns []) if it detects a running event loop, because
        # it drives the model via asyncio.run() and must not nest. This job runs under asyncio.run() in
        # a scheduler worker thread, so a running loop IS present here and a direct call would always
        # generate 0. Offloading to a plain worker thread gives that call NO running loop, so the guard
        # passes and generation actually happens. (The guard stays intact; we just satisfy it.)
        gen = await asyncio.to_thread(top_up, audience="general")
        guest = await asyncio.to_thread(top_up, audience="guest")
        return {"ok": True, "general": gen, "guest": guest}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}



def _provider_result_ok(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("skipped"):
            return True
        if "ok" in result:
            return bool(result.get("ok"))
        # Crawler/source dicts often use status instead of ok. done/partial/skipped are successful
        # scheduler runs; error/degraded are not.
        status = str(result.get("status", "")).lower()
        if status:
            return status not in {"error", "degraded"}
        # A provider dict without an explicit failure flag is treated as successful for compatibility.
        return True
    return result is not False


def _summarise_provider_results(out: dict[str, Any], *, skipped: str) -> dict[str, Any]:
    """Add a top-level ok flag so the scheduler does not record all-provider failures as green.

    After weeks of unattended use, a nested {"gmail": {"ok": false}} recorded as job status "ok" is
    worse than no scheduler at all: self-heal/liveness believe the lane is healthy while data rots.
    """
    if not out:
        return {"skipped": skipped}
    # Evaluate every configured provider that ran this cycle. ok stays any(...) so a single dead provider
    # does NOT fail the whole lane (a working Gmail must keep the lane green). But a provider that is
    # configured-but-failed THIS run is named in `degraded` so a chronically-dead one (e.g. an expired
    # Microsoft token returning ok:False every run) is visible in the job detail/status instead of being
    # buried in last_detail forever. `skipped` providers (not configured) are treated as ok, not degraded.
    per_ok = {name: _provider_result_ok(res) for name, res in out.items()}
    ok = any(per_ok.values())
    out["ok"] = ok
    degraded = [name for name, good in per_ok.items() if not good]
    if degraded:
        out["degraded"] = degraded
    if not ok:
        out["error"] = "all configured providers failed"
    return out

async def _job_sync_email() -> dict[str, Any]:
    """Best-effort sync of every configured mail provider (IMAP, Gmail, Microsoft),
    each of which also indexes attachments. A provider that isn't configured or
    fails is skipped, never sinks the others."""
    out: dict[str, Any] = {}
    settings = get_settings()

    if getattr(settings, "imap_host", ""):
        try:
            # Cursor-based freshness: only fetch mail newer than the last UID we
            # saw, instead of re-pulling the last 50 every cycle.
            from .crawler import crawl_imap_fresh
            out["imap"] = crawl_imap_fresh("INBOX")
        except Exception as exc:
            out["imap"] = {"ok": False, "error": str(exc)}

    # Gmail / Microsoft only run if their OAuth token is present; the connectors
    # raise if not authorised, so we guard on the token file/credentials existing.
    if _provider_ready("google"):
        try:
            from ..connectors.gmail import sync_gmail
            # Use a generous overlap so a 2-3 day network outage or paused scheduler does not create
            # a permanent blind spot. Gmail sync de-duplicates by source id, so re-reading the overlap
            # is cheaper than silently missing the gap.
            out["gmail"] = sync_gmail(query="newer_than:30d", max_results=100)
        except Exception as exc:
            out["gmail"] = {"ok": False, "error": str(exc)}
    if _provider_ready("microsoft"):
        try:
            from ..connectors.microsoft_graph import sync_microsoft_mail
            out["microsoft"] = sync_microsoft_mail(max_results=50)
        except Exception as exc:
            out["microsoft"] = {"ok": False, "error": str(exc)}

    return _summarise_provider_results(out, skipped="no_mail_providers_configured")


async def _job_sync_email_sent() -> dict[str, Any]:
    """Index the IMAP Sent folder on its own cadence and timeout budget, so a Sent backlog can never
    time out the primary inbox/Gmail/Microsoft sync (which is what happened during the first Sent
    backfill). Separate UID cursor (imap:Sent), so it never disturbs the INBOX cursor."""
    settings = get_settings()
    if not getattr(settings, "imap_host", ""):
        return {"ok": True, "skipped": "no_imap"}
    try:
        from .crawler import crawl_imap_fresh
        return {"ok": True, "imap_sent": crawl_imap_fresh("Sent")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _job_sync_email_archive() -> dict[str, Any]:
    """Index the IMAP Archive folder on its own cadence. M files the WordPress Bcc copies (receipts,
    demand mailings) into Zimbra's Archive, so without this those audit copies are never indexed and a
    sent receipt looks unprovable even though the proof is sitting in Archive. Separate UID cursor
    (imap:Archive), so it never disturbs the INBOX or Sent cursors."""
    settings = get_settings()
    if not getattr(settings, "imap_host", ""):
        return {"ok": True, "skipped": "no_imap"}
    try:
        from .crawler import crawl_imap_fresh
        return {"ok": True, "imap_archive": crawl_imap_fresh("Archive")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _job_v3_handshake() -> dict[str, Any]:
    """Auto-handshake: when v3 is enabled, post mongoose's version-handshake to peers as soon as the Pi
    link opens. Idempotent (fires only on the unreachable->reachable transition). Strictly M-gated, the
    underlying function does nothing if protocol_v3_enabled is False. Runs off-thread so the SSH probe
    can't block the scheduler tick."""
    import asyncio as _aio
    from .protocol_v3 import post_handshake_when_open
    return await _aio.to_thread(post_handshake_when_open)


async def _job_v3_reconcile() -> dict[str, Any]:
    """When v3 is enabled and the Pi is reachable: catch up on the bus + voicemail backlog since our
    last processed message, ack anything addressed to us, run the protocol-version check, and post a
    heartbeat. Read/ack/heartbeat only, takes NO household action (those stay M-gated). Off-thread so
    the SSH round-trips can't block the scheduler tick. Strictly M-gated by protocol_v3_enabled."""
    import asyncio as _aio
    from .protocol_v3_runtime import reconcile_on_wake
    return await _aio.to_thread(reconcile_on_wake)


async def _job_rtm_verify() -> dict[str, Any]:
    """Publish/verify heartbeat: GET the live build endpoint on example-court.org and record what the
    site reports running, so the RTM strip's 'last successful WP heartbeat' and build-match verdict are
    driven by an active check. Off-thread (network round-trip) and self-degrading: only registered when
    WordPress is configured, and verify_live never raises."""
    import asyncio as _aio
    from .rtm_deploy_state import verify_live
    return await _aio.to_thread(verify_live)


async def _job_rtm_demand_prepare() -> dict[str, Any]:
    """On/after the scheduled prepare-date, dry-run the next half-year demand run and notify the owner to
    review + confirm. Never issues or sends by itself (a service-charge demand to 18 leaseholders is a
    human-confirmed step). No-op until a run is scheduled and its prepare-date arrives; fires once."""
    from . import rtm_scheduled_demands as SD
    return await SD.run_prepare()


async def _job_rtm_scheduled_letters() -> dict[str, Any]:
    """Email any armed, due batch of scheduled letters (rtm_scheduled_letters), and tell the owner how it
    went. No-op when nothing is due; each letter is sent at most once."""
    import asyncio as _aio
    from . import rtm_scheduled_letters as SL

    def _notify(title: str, body: str) -> None:
        try:
            import asyncio as _a2
            from .notifications import publish_ntfy
            _a2.run(publish_ntfy(title, body, priority="high"))
        except Exception:
            pass
    return await _aio.to_thread(SL.run_due, notify=_notify)


async def _job_rtm_queue_sync() -> dict[str, Any]:
    """Drain the WordPress-queued director actions (mark-paid, membership changes, document requests),
    apply them into the ledger and republish if anything changed, so a director's portal click takes
    effect on its own, no manual Cockpit pull. This is the resilient safety net; a WordPress 'you have
    work' nudge (webhook) applies it within a second when the Mac is up, and this timer catches anything
    the nudge missed (e.g. the Mac was asleep at click time). Off-thread (sync httpx to WordPress); only
    registered when WordPress is configured, and every call degrades honestly if the site is unreachable."""
    import asyncio as _aio
    from .rtm_portal import sync_wp_queues
    res = await _aio.to_thread(sync_wp_queues)
    # Alarm if the portal snapshot publish is failing (the silent 400 that froze WordPress on stale
    # data). Deduped to once per few hours so a persistent failure pings the owner once, not every cycle.
    try:
        from .rtm_portal import should_alarm_snapshot
        msg = await _aio.to_thread(should_alarm_snapshot)
        if msg:
            from .notifications import publish_ntfy
            await publish_ntfy("RTM portal sync failing", msg, priority="high")
    except Exception:
        pass
    return res


async def _job_verify_receipt_sends() -> dict[str, Any]:
    """Delivery-truth check: alarm when a receipt Mongoose recorded as 'sent' has NO audit copy in the info@
    archive (a real non-delivery the mail transport falsely reported as success , the Patil case). This is
    the catch that makes 'no Bcc means it did not send' automatic instead of something the owner spots."""
    import asyncio as _aio
    from .rtm_portal import verify_receipt_deliveries
    res = await _aio.to_thread(verify_receipt_deliveries)
    unconf = res.get("unconfirmed") or []
    if unconf:
        try:
            from .notifications import publish_ntfy
            who = ", ".join(f"{u['flat_id']} ({u.get('email') or 'no email'})" for u in unconf[:6])
            await publish_ntfy(
                "RTM receipt NOT delivered",
                f"Recorded as sent but no delivery copy in info@: {who}. A re-send is needed.",
                priority="high")
        except Exception:
            pass
    return res


async def _job_portal_snapshot_guard() -> dict[str, Any]:
    """Intelligent self-healing publish + tamper watch (fixes the manual-publish gap). Each interval it
    republishes ONLY when Mongoose-side data actually changed (content hash moved), and if WordPress is
    serving a snapshot Mongoose never pushed it auto-engages the security hold and pings the owner. No
    blind timer re-pushes; a benign no-op when nothing changed."""
    import asyncio as _aio
    from .rtm_portal import snapshot_guard, snapshot_publish_health
    res = await _aio.to_thread(snapshot_guard)
    if res.get("alarm"):
        try:
            from .notifications import publish_ntfy
            await publish_ntfy(
                "RTM portal TAMPER: sync auto-held",
                res.get("reason") or "WordPress is serving data Mongoose did not push. Two-way sync paused.",
                priority="high")
        except Exception:
            pass
    # A publish that keeps FAILING is just as bad as tampering and was previously silent: WordPress
    # simply freezes on the last good snapshot while Mongoose's ledger stays correct, so a payment
    # marked in the portal appears never to have registered. That happened for hours (a new top-level
    # key rejected 400 by an older plugin) and only surfaced because the owner noticed a stale figure.
    # Alarm once per outage, not once per interval, so it informs without becoming noise.
    try:
        h = await _aio.to_thread(snapshot_publish_health)
        fails = int(h.get("consecutive_failures") or 0)
        if fails >= 3:
            from ..db import get_app_state, set_app_state
            if str(get_app_state("portal_publish_alarm_sent") or "") != "1":
                from .notifications import publish_ntfy
                await publish_ntfy(
                    "RTM portal NOT updating",
                    f"{fails} snapshot publishes failed in a row, so the leaseholder portal is frozen on "
                    f"old data. Last error: {str(h.get('last_error') or '')[:140]}",
                    priority="high")
                set_app_state("portal_publish_alarm_sent", "1")
        elif fails == 0:
            from ..db import get_app_state, set_app_state
            if str(get_app_state("portal_publish_alarm_sent") or "") == "1":
                set_app_state("portal_publish_alarm_sent", "")   # recovered: re-arm for next time
        res["publish_health"] = {"consecutive_failures": fails, "ok": bool(h.get("ok"))}
    except Exception:
        pass
    return res


async def _job_sync_tasks() -> dict[str, Any]:
    """Keep Microsoft To Do tasks (and the Outlook calendar) current in the background, so To Do, the
    whole point of the Microsoft integration, doesn't go stale between manual `mongoose sync` runs.
    Runs NON-INTERACTIVELY: if the Microsoft token is missing/expired it records 'needs sign-in' and
    moves on rather than blocking on a device-code prompt (which would hang the scheduler). Only runs
    when Microsoft is plausibly authorised."""
    out: dict[str, Any] = {}
    if _provider_ready("microsoft"):
        try:
            from ..connectors.microsoft_graph import sync_microsoft_todo
            out["todo"] = sync_microsoft_todo(allow_interactive=False)
        except Exception as exc:
            out["todo"] = {"ok": False, "error": str(exc)}
        try:
            from ..connectors.microsoft_graph import sync_microsoft_calendar
            out["calendar"] = sync_microsoft_calendar(allow_interactive=False)
        except Exception as exc:
            out["calendar"] = {"ok": False, "error": str(exc)}
    return _summarise_provider_results(out, skipped="microsoft_not_configured")


async def _job_self_reflection() -> dict[str, Any]:
    """Periodic bounded self-reflection: notice if a capability that exists in code stopped being
    reachable from the single brain (a tool dropped from READ_TOOLS, a status area that crashes, a
    probe with no chat path). AUDIT-ONLY by default (reports gaps, changes nothing); applies the
    bounded reversible auto-fixes only when SELF_REFLECTION_AUTOFIX is enabled. When v3 is on and a gap
    was found/closed, it notifies the peers so Claude Chat gets visibility. No code authoring, no
    network beyond the (M-gated) bus post."""
    try:
        import os as _os
        from .self_reflection import reflect, reflect_and_notify
        autofix = str(_os.getenv("SELF_REFLECTION_AUTOFIX", "")).lower() in ("1", "true", "yes")
        if autofix:
            return await asyncio.to_thread(reflect_and_notify, apply_fixes=True)
        return await asyncio.to_thread(reflect, apply_fixes=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _job_self_heal() -> dict[str, Any]:
    """Detect silently-failing subsystems (stale past their sync interval) and attempt one recovery
    run each. Reuses the same job functions the scheduler runs, so there's one code path."""
    try:
        from .self_heal import run_self_heal
        return await run_self_heal()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

async def _job_self_harden_scan() -> dict[str, Any]:
    """Gentle, cheap, LOCAL self-harden observation scan. Records observations from scheduler/job
    errors, runtime watchdog restart/lag signals, and failed audit-log rows, mapping them to the
    claim/invariant registry. EVIDENCE ONLY: it makes no network calls, touches no live connectors,
    calls no external models, edits no code, installs nothing, and sends no data. Safe to run often."""
    try:
        from .self_harden import scan_runtime
        return scan_runtime()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _job_brain_snapshot() -> dict[str, Any]:
    """Daily: take a consistent snapshot of the brain (conversation + memory + identity) so there's
    always a recent local restore point even between full backups, then prune to keep the newest few.
    Uses SQLite online backup, safe while the server runs. Touches no code, sends nothing."""
    out: dict[str, Any] = {}
    try:
        from ..brain_preservation import snapshot_brain, prune_snapshots
        snap = await asyncio.to_thread(snapshot_brain, "daily")
        out["snapshot"] = {"ok": snap.get("ok"), "dir": snap.get("snapshot_dir")}
        out["prune"] = await asyncio.to_thread(prune_snapshots, 5)   # 5 SPREAD: 2 daily, 1 weekly, 2 monthly (+ newest preupdate, oldest)
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _job_backup() -> dict[str, Any]:
    """Daily: create a brain+config backup, prune the local rota (grandfather-father-son: 7 daily, 4
    weekly, 12 monthly), and, if Google Drive is connected, upload it off-machine and prune the Drive
    rota the same way. If Drive isn't connected it still keeps the local backup. Off by default; runs
    only when MONGOOSE_BACKUP_ENABLED is on. Uploads files only; never reads the wider Drive."""
    try:
        s = get_settings()
        if not getattr(s, "backup_enabled", False):
            return {"skipped": "backups disabled (set MONGOOSE_BACKUP_ENABLED=true)"}
        from ..backup_drive import backup_to_drive
        return await asyncio.to_thread(backup_to_drive)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _job_benchmark() -> dict[str, Any]:
    """Run model benchmarks, but ONLY in the quiet early-morning window (default 5am-8am, gated by the
    'window' timing). Benchmarking loads models and uses the machine hard, so it must never compete
    with daytime use of the single local model. Records a visible run marker (app_state
    benchmark_last_run / benchmark_last_summary) so the cockpit and the second-lane surpriser can show
    WHEN benchmarks ran and WHAT they found. Off by default; runs only when MONGOOSE_BENCHMARK_ENABLED
    is on AND model benchmarking has something to compare."""
    from ..db import set_app_state
    try:
        s = get_settings()
        if not getattr(s, "benchmark_enabled", False):
            return {"skipped": "benchmarks disabled (set MONGOOSE_BENCHMARK_ENABLED=true)"}
        set_app_state("benchmark_last_started", utc_now_iso())
        from ..model_tuning import models_to_benchmark
        # models_to_benchmark() calls asyncio.run(installed_models()) internally, which THROWS when
        # called from a running event loop (this job is async). The except-fallback then dedupes the
        # configured roles , all pinned to the SAME model here , down to 1, i.e. < 2, so it returned []
        # and the lane recorded "no candidate models" every single night despite 9 models installed.
        # Run it in a worker thread (no running loop) so the model listing actually succeeds.
        models = await asyncio.to_thread(models_to_benchmark)
        if not models:
            set_app_state("benchmark_last_summary", json.dumps(
                {"ran": False, "reason": "no candidate models to benchmark", "at": utc_now_iso()}))
            return {"skipped": "no candidate models to benchmark"}
        from ..benchmark import run_benchmark
        results = await run_benchmark(models, measure_cold=True, auto_pull=False, rounds=1, repeats=2)
        summary = {
            "ran": True,
            "at": utc_now_iso(),
            "models": models,
            "result_count": len(results),
        }
        set_app_state("benchmark_last_run", utc_now_iso())
        set_app_state("benchmark_last_summary", json.dumps(summary))
        return {"ok": True, **summary}
    except Exception as exc:  # noqa: BLE001
        try:
            set_app_state("benchmark_last_summary", json.dumps(
                {"ran": False, "error": str(exc), "at": utc_now_iso()}))
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}


async def _job_benchmark_reveal() -> dict[str, Any]:
    """Second lane: if the background benchmarks have a fresh, worth-sharing finding, have Mongoose
    volunteer it as a genuine unprompted message in the permanent conversation (and a phone push if
    notable). Quiet by nature, silent unless there's something new, de-duped, rate-limited. Reads
    benchmark results and writes a chat message; never switches a model or changes code by itself.
    Runs on a gentle interval; it self-gates so most cycles say nothing."""
    try:
        from .benchmark_surpriser import run_benchmark_revelation_cycle
        return await run_benchmark_revelation_cycle()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

def _provider_ready(provider: str) -> bool:
    """Cheap check that a cloud-mail provider is plausibly authorised, so the
    scheduler doesn't fire a sync that's guaranteed to raise. Routes through the
    central plausibility-guarded checks in oauth so a tiny placeholder token file
    (an interrupted write, a touch'd file, bare {}) can never read as ready."""
    try:
        from ..oauth import google_token_present, microsoft_token_present
        if provider == "google":
            return google_token_present()
        if provider == "microsoft":
            return microsoft_token_present()
    except Exception:
        pass
    return False


async def _job_ddns_update() -> dict[str, Any]:
    """The 'automatic script that updates the IP': if a DDNS provider is configured, detect the
    current public IP and push it to the provider when it changes. No-op for Tailscale (its own
    daemon) and when nothing is configured. Network calls are wrapped so a transient failure is
    just a failed run, not a crash."""
    try:
        from ..ddns import update_now, status
        st = status()
        if not st.get("provider") or not st.get("enabled"):
            return {"skipped": "no DDNS provider enabled"}
        res = update_now()
        return {"provider": res.get("provider"), "ok": res.get("ok"),
                "changed": res.get("changed"), "detail": res.get("detail")}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def _job_reindex_files() -> dict[str, Any]:
    """Keep configured file roots fresh via Crawler v1, so scheduled reindexing uses
    the crawler's cursor semantics, resumable chunks, and load controls instead of a
    full re-walk. Roots come from MONGOOSE_INDEX_ROOTS (path:device pairs or paths)."""
    from .crawler import crawl_files
    roots = getattr(get_settings(), "index_roots", ())
    if not roots:
        return {"skipped": "no_index_roots"}
    out = {}
    for spec in roots:
        path, _, device = spec.partition(":")
        if not path:
            continue
        # fresh mode: a bounded, resumable, cursor-tracked pass.
        out[path] = crawl_files(path, device=device or "local", mode="fresh")
    return out


async def _job_metro_watch() -> dict[str, Any]:
    """Drain the Metro CSV drop folder: import any new export, archive it, surface the
    review queue. The user's only manual act is clicking 'Export transactions to CSV'
    in Metro into the watched folder; everything after is automatic. Touches no bank,
    moves no money. Pushes a phone notice ONLY when a run imported something that needs
    manual review (partial/unmatched), so a clean auto-reconciled drop stays silent."""
    from .metro_watch import scan_once
    res = scan_once()
    if res.get("skipped"):
        return res
    if res.get("files_imported"):
        files = res.get("files_imported", 0)
        review = res.get("needs_review", 0)
        if review:
            try:
                from .notifications import send_to_phone
                await send_to_phone(
                    "Metro import: review needed",
                    f"{files} statement(s) imported; {review} payment(s) need allocating.",
                    priority="default", tags="bank")
            except Exception:
                pass
    return res


# Registry: name -> (schedule_spec, job_fn). schedule_spec is either
# ("interval", minutes) or ("daily", hour, minute).
def run_job_now(name: str) -> dict[str, Any]:
    """Run a single registered job by name, right now, synchronously from the caller's thread. For
    on-demand kickoffs (e.g. an initial sync the moment a connector is saved, so first search has data).
    Bypasses the paused/deferred list because it is an explicit, user-triggered run. Never raises."""
    try:
        registry = _build_registry()
        entry = registry.get(name)
        if not entry:
            return {"ok": False, "error": f"unknown job {name!r}"}
        fn = entry[1]
        res = _run_job_coro_in_thread(fn)
        try:
            _record_run(name, "ok", "manual run_job_now")
        except Exception:
            pass
        return {"ok": True, "job": name, "result": res if isinstance(res, dict) else {"ran": True}}
    except Exception as exc:
        try:
            _record_run(name, "error", f"run_job_now: {exc}")
        except Exception:
            pass
        return {"ok": False, "job": name, "error": str(exc)[:200]}


async def _job_knowledge_reflection() -> dict[str, Any]:
    """Reflect across imported knowledge claims (domain/body/posture patterns, sensitive and review
    pressure) and QUEUE research for pending sensitive items. Surfaces nothing on its own."""
    from ..knowledge_import.reflection import run_reflection_cycle
    out = run_reflection_cycle(kind="scheduled", queue_research=True)
    return {"ok": bool(out.get("ok", True)), "queued_research": len(out.get("research_queued", []) or [])}


async def _job_knowledge_research() -> dict[str, Any]:
    """Auto-run queued knowledge research missions, bounded per cycle, then DIGEST what landed.

    Only runs when web search is enabled. Safe/current missions run first; sensitive health missions
    (medical source policy) run last and stay source-carded, non-instructional evidence for review, never
    advice and never a proactive nudge. After new evidence lands, a synthesis pass digests it into
    connected cards (sensitive clusters stay review/research material). Bounded by
    `knowledge_research_max_per_cycle` (default 6) so a cycle never hammers SearXNG."""
    from .. import web_search
    if not web_search.is_enabled():
        return {"skipped": "web search disabled"}
    from ..knowledge_import.research_broker import (
        queue_pending_research, pending_research_requests, run_research_by_request_id)
    budget = max(1, int(getattr(get_settings(), "knowledge_research_max_per_cycle", 6)))
    try:
        queued = queue_pending_research(limit=10)
    except Exception:
        queued = []
    pending = pending_research_requests(limit=100)
    # Safe/current first, sensitive health last (still source-carded and non-instructional when it runs).
    pending.sort(key=lambda r: (bool(r.get("sensitive")),))
    ran = failed = sensitive_ran = 0
    for req in pending:
        if ran + failed >= budget:
            break
        try:
            res = await run_research_by_request_id(req["request_id"])
            if res.get("ok"):
                ran += 1
                sensitive_ran += 1 if req.get("sensitive") else 0
            else:
                failed += 1
        except Exception:
            failed += 1
    digested = 0
    if ran:
        try:
            from ..knowledge_import.synthesis import run_synthesis_cycle
            dig = run_synthesis_cycle(limit=40)
            digested = int(dig.get("inserted", 0)) if isinstance(dig, dict) else 0
        except Exception:
            digested = 0
    # Honest health: if there were missions to run and EVERY one failed (with none succeeding), the job
    # is not green , surface it so a broken SearXNG/model path is visible instead of reading as "quiet".
    return {"ok": not (failed > 0 and ran == 0), "queued": len(queued), "ran": ran, "failed": failed,
            "sensitive_ran": sensitive_ran, "digested": digested}


def _music_scopes() -> list[tuple[str, str]]:
    """Everyone Mongoose should court about music, as (scope, notify_user_id) pairs: the owner, plus
    every currently-paired trusted guest (so a guest like Jordan is invited even if they have never once
    opened their private chat). A guest's music scope IS their account user_id."""
    from .auth_accounts import owner_account, account_for_device
    out: list[tuple[str, str]] = []
    try:
        acct = owner_account()
        if acct and acct.get("user_id"):
            out.append((None, acct["user_id"]))  # None -> OWNER_SCOPE, filled in below
    except Exception:
        pass
    try:
        from ..devices import list_devices
        seen: set[str] = set()
        for d in list_devices():
            if d.get("profile") != "trusted_guest" or d.get("revoked"):
                continue
            try:
                uid = account_for_device(d.get("device_id", ""))
            except Exception:
                uid = ""
            if uid and uid not in seen:
                seen.add(uid)
                out.append((uid, uid))  # guest scope == user_id
    except Exception:
        pass
    return out


def _conversation_for_scope(scope: str) -> Optional[str]:
    """The PRIVATE-chat conversation id a music scope reads from, so a proactive question can be
    posted straight into the thread the person actually opens. Owner -> the shared default thread;
    a guest -> their person thread (guest:p:<owner_label>), the exact id /device/chat writes to, so
    the seeded question and their later replies live in ONE thread. Returns None if it can't be
    resolved (never guess a thread; a wrong id would strand the message where nobody looks)."""
    from . import music_taste as MT
    from .memory import DEFAULT_CONVERSATION_ID, ensure_conversation
    if scope == MT.OWNER_SCOPE:
        return DEFAULT_CONVERSATION_ID
    try:
        from .auth_accounts import account_for_device, get_account
        from ..devices import list_devices
        from ..db import connect
        # A person can have SEVERAL paired guest devices (a re-invite mints a new device_id). Live data
        # shows the two links can even split across devices: the device account-linked to the music
        # scope may carry NO owner_label, while the device that DOES carry the label is not account-
        # linked (Jordan is exactly this case). The phone's read path (_guest_conv_id) keys the thread on
        # the PERSON, guest:p:<owner_label>, and _music_scope_for resolves the scope by the account
        # display NAME when the device link is missing. Mirror both here so we land on the SAME person
        # thread the phone reads, instead of a device-scoped id nobody opens (why Jordan's question never
        # appeared). Prefer any owner_label reachable via the account's device link OR its display name;
        # only fall back to a device-scoped id when the person has no labelled device at all.
        acct = get_account(scope) or {}
        acct_name = " ".join(((acct.get("display_name") or "")).split()).casefold()
        first_did = ""
        for d in list_devices():
            if d.get("profile") != "trusted_guest" or d.get("revoked"):
                continue
            did = str(d.get("device_id") or "")
            if not did:
                continue
            linked = account_for_device(did) == scope
            dev_name = " ".join(((d.get("display_name") or d.get("name") or "")).split()).casefold()
            name_match = bool(acct_name) and dev_name == acct_name
            if not (linked or name_match):
                continue
            if linked and not first_did:
                first_did = did
            with connect() as con:
                row = con.execute("SELECT owner_label FROM guest_profiles WHERE device_id=?",
                                  (did,)).fetchone()
            label = " ".join(((row["owner_label"] if row else "") or "").split()).casefold()
            if label:
                conv = f"guest:p:{label}"
                ensure_conversation(conv)
                return conv
        if first_did:
            conv = f"guest:{first_did}"
            ensure_conversation(conv)
            return conv
    except Exception:
        return None
    return None


async def _job_music_nudge() -> dict[str, Any]:
    """Keep the music interview alive for the owner and every paired guest, proactively.

    Two jobs in one sweep, so Mongoose reaches out FIRST instead of waiting for someone to open their
    private chat (the logged bug this fixes). Both are deliberately RARE and RANDOMLY timed so the whole
    thing feels like a spontaneous surpriser, not a daily task reminder:
      - re-nudge: an unanswered question earns at most ONE gentle reminder, only ~half the time, and
        only after a randomly-rolled 1 to 3 day wait (most questions are simply left be);
      - invite: if the person is onboarding and nothing is awaiting them, proactively invite them into
        the interview, spaced by a fresh random 3 to 9 day gap (capped, quiet-hours-aware).
    All the copy is drawn from varied pools so no reach-out reads the same twice in a row.
    """
    from . import music_taste as MT
    from .notifications import send_to_phone
    from .auth_accounts import owner_account
    nudged, invited, errors = 0, 0, 0

    def _uid_for(scope: str) -> str:
        if scope == MT.OWNER_SCOPE:
            acct = owner_account()
            return acct["user_id"] if acct else ""
        return scope  # a guest scope IS that guest's account user_id

    # 1. Re-nudge: any scope with a question left unanswered ~40 minutes (once per question). Sweeps
    #    every scope that has state, so a pending question is never dropped.
    nudged_scopes: set[str] = set()
    for scope in MT.all_scopes():
        try:
            if not MT.nudge_due(scope):
                continue
            uid = _uid_for(scope)
            if not uid:
                continue
            # Guarantee the outstanding question actually lives in the person's OWN chat thread before
            # we tell them to "come answer" it. A facet can be marked asked without its question
            # surviving in history (the chat-reply append path only put it in the transient HTTP
            # response), which is exactly why Jordan got the push and found an empty chat. Re-post the
            # outstanding question into the thread here, and if we cannot resolve that thread, do not
            # push at all , a reminder that points at nothing is worse than silence.
            facet = MT.unanswered_asked_facet(scope)
            conv = _conversation_for_scope(scope)
            if not facet or not conv:
                continue
            try:
                from .memory import add_message
                display = ""
                if scope != MT.OWNER_SCOPE:
                    from .auth_accounts import get_account
                    _acct = get_account(scope) or {}
                    display = (_acct.get("display_name") or "").split(" ")[0]
                q = MT.question_for(facet, name=display)
                # Freshly-phrased, gentle lead-in (never the same twice in a row) so the rare reminder
                # reads as a passing thought, not a recycled nag.
                add_message(conv, "assistant", MT.reminder_opener() + q)
            except Exception:
                errors += 1
                continue  # could not place the question in the thread, so never push about it
            res = await send_to_phone(
                MT.reminder_push_title(),
                MT.reminder_push_body(),
                target_user_id=uid, click="/device/launchpad#talk",
                log_source="music", log_app="Mongoose")
            if (res or {}).get("sent"):
                MT.mark_nudged(scope)
                nudged_scopes.add(scope)
                nudged += 1
        except Exception:
            errors += 1
            continue

    # 2. Proactive first-contact invite for the owner and every paired guest (this is what makes the
    #    interview reach out FIRST instead of waiting for someone to message). Quiet-hours-aware and
    #    rate-limited inside invite_due. invite_due already returns False when a question is pending, so
    #    a just-nudged person is never also invited in the same sweep.
    try:
        from .surpriser import _in_quiet_hours
        from datetime import datetime
        from zoneinfo import ZoneInfo
        quiet = _in_quiet_hours(datetime.now(ZoneInfo(get_settings().timezone)))
    except Exception:
        quiet = False
    if not quiet:
        for raw_scope, uid in _music_scopes():
            scope = raw_scope if raw_scope is not None else MT.OWNER_SCOPE
            if not uid or scope in nudged_scopes:
                continue
            try:
                MT.begin_if_new(scope)  # start the clock for a guest who has never chatted
                if not MT.invite_due(scope):
                    continue
                # Seed the FIRST question straight into their private thread BEFORE the notification,
                # so tapping it opens a chat that already has Mongoose's opening question waiting,
                # not an empty screen (the logged bug: Jordan got the invite, opened chat, found nothing
                # because the interview question was only appended AFTER he first messaged). Only when
                # nothing has been asked yet; mark_asked opens the capture window so his reply lands.
                seeded = False
                try:
                    if MT.next_unasked_facet(scope) and not MT.pending_facet(scope):
                        conv = _conversation_for_scope(scope)
                        if conv:
                            from .memory import add_message
                            facet = MT.next_unasked_facet(scope)
                            display = ""
                            if scope != MT.OWNER_SCOPE:
                                from .auth_accounts import get_account
                                _acct = get_account(scope) or {}
                                display = (_acct.get("display_name") or "").split(" ")[0]
                            q = MT.question_for(facet, name=display)
                            # Freshly-phrased, warm lead-in (never the same twice in a row) so the
                            # invite lands as a spontaneous thought rather than a scripted greeting.
                            add_message(conv, "assistant", MT.invite_opener() + q)
                            MT.mark_asked(facet, scope)
                            seeded = True
                except Exception:
                    seeded = False
                # Only send the invite push if we actually placed the opening question in their own
                # thread. Otherwise the notification would open an empty chat (and, unpushed, we keep
                # invite_count untouched so we retry cleanly next sweep once the thread resolves).
                if not seeded:
                    continue
                res = await send_to_phone(
                    MT.invite_push_title(),
                    MT.invite_push_body(),
                    target_user_id=uid, click="/device/launchpad#talk",
                    log_source="music", log_app="Mongoose")
                if (res or {}).get("sent"):
                    MT.mark_invited(scope)
                    invited += 1
            except Exception:
                errors += 1
                continue
    # Honest health: if every scope we tried errored (push transport down, thread resolver broken) and
    # nothing went out, the job is NOT green , surface it instead of reading as a quiet no-op.
    return {"ok": not (errors > 0 and (nudged + invited) == 0),
            "nudged": nudged, "invited": invited, "errors": errors}


async def _job_memory_guard() -> dict[str, Any]:
    """Keep the pinned main model hot but shed the other fat when RAM is tight. Only acts under real
    pressure, and only takes the server-SAFE action (evict any secondary Ollama model, never the primary);
    external hogs (browsers/Spotlight/Docker) are returned as recommendations for M, not killed here.
    See services/memory_guard.py and the m-keep-main-model-pinned preference."""
    try:
        from .memory_guard import snapshot, apply_safe_relief
        snap = snapshot()
        if snap.get("level") not in ("red", "high", "critical"):
            return {"ok": True, "level": snap.get("level"), "action": "none (pressure not high)"}
        res = apply_safe_relief()
        return {"ok": True, "level": res.get("level"),
                "evicted_secondary_models": res.get("evicted_secondary_models"),
                "recommend": [h.get("name") for h in (res.get("recommendations") or [])]}
    except Exception as exc:  # never let the guard crash the scheduler
        return {"ok": False, "error": str(exc)}


def _build_registry() -> dict[str, tuple[tuple, JobFn]]:
    s = get_settings()
    brief_h, brief_m = getattr(s, "brief_time", (7, 30))
    reg = {
        "daily_brief": (("daily", brief_h, brief_m), _job_daily_brief),
        "sync_email": (("interval", getattr(s, "email_sync_minutes", 30)), _job_sync_email),
        "sync_email_sent": (("interval", getattr(s, "email_sent_sync_minutes", 7)), _job_sync_email_sent),
        "sync_email_archive": (("interval", getattr(s, "email_archive_sync_minutes", 11)), _job_sync_email_archive),
        "sync_tasks": (("interval", getattr(s, "tasks_sync_minutes", 30)), _job_sync_tasks),
        "self_harden_scan": (("interval", getattr(s, "harden_scan_minutes", 15)), _job_self_harden_scan),
        "self_heal": (("interval", getattr(s, "self_heal_minutes", 20)), _job_self_heal),
        "self_reflection": (("interval", getattr(s, "self_reflection_minutes", 360)), _job_self_reflection),
        "delight_topup": (("interval", getattr(s, "delight_topup_minutes", 90)), _job_delight_topup),
        "brain_snapshot": (("daily", getattr(s, "brain_snapshot_hour", 4), 30), _job_brain_snapshot),
        "backup": (("daily", getattr(s, "backup_hour", 4), 45), _job_backup),
        "benchmark": (("window", getattr(s, "benchmark_window_start", 5), getattr(s, "benchmark_window_end", 8)), _job_benchmark),
        "benchmark_reveal": (("interval", getattr(s, "benchmark_reveal_minutes", 90)), _job_benchmark_reveal),
        "reindex_files": (("interval", getattr(s, "reindex_minutes", 360)), _job_reindex_files),
        "knowledge_reflection": (("interval", getattr(s, "knowledge_reflection_minutes", 360)), _job_knowledge_reflection),
        "knowledge_research": (("interval", getattr(s, "knowledge_research_minutes", 360)), _job_knowledge_research),
        "music_nudge": (("interval", getattr(s, "music_nudge_minutes", 10)), _job_music_nudge),
        "memory_guard": (("interval", getattr(s, "memory_guard_minutes", 4)), _job_memory_guard),
        "ops_health": (("interval", getattr(s, "ops_health_minutes", 360)), _job_ops_health),
        "model_trial": (("window", getattr(s, "benchmark_window_start", 5), getattr(s, "benchmark_window_end", 8)), _job_model_trial),
    }
    # The surpriser runs on an interval but is OFF by default: a resident companion that
    # offers unprompted suggestions is opt-in. Even when on, it self-rate-limits, respects
    # quiet hours, and only surfaces novel high-value correlations (most cycles are silent).
    if getattr(s, "surpriser_enabled", False):
        reg["surpriser"] = (("interval", getattr(s, "surpriser_minutes", 45)), _job_surpriser)
    # Wellness check-ins are always in the registry so the UI/chat can show and control the lane, but
    # the job itself is silent until the owner enables it. The time is stored in app_state so a chat
    # command can change it without editing .env or restarting.
    try:
        from .wellness_checkins import schedule_time
        _wch, _wcm = schedule_time()
    except Exception:
        _wch, _wcm = 20, 30
    reg["wellness_checkins"] = (("daily", _wch, _wcm), _job_wellness_checkins)
    # Lane 2: check model-tuning vesting once a day. Cheap (just reads usage + benchmark
    # coverage); only acts/pushes when it has genuinely vested with a better setup.
    reg["tuning_check"] = (("daily", getattr(s, "tuning_check_hour", 9), 15), _job_tuning_check)
    # Readiness: refresh the cached snapshot on a short interval so voice/phone read a fresh
    # picture without running live collectors per query. Cheap (local DB + file checks).
    reg["readiness_refresh"] = (("interval", getattr(s, "readiness_refresh_minutes", 10)), _job_readiness_refresh)
    # Keep ALL cockpit SWR status caches (readiness, liveness, tuning, daily, release_check) hot on an
    # interval, so they never go cold enough to make an interactive poll pay the full compute.
    reg["warm_status_caches"] = (("interval", getattr(s, "warm_caches_minutes", 12)), _job_warm_status_caches)
    reg["resource_guard"] = (("interval", getattr(s, "resource_guard_minutes", 5)), _job_resource_guard)
    # DDNS: if a remote-reach provider is configured and enabled, keep the DNS record pointed at the
    # current public IP. Only registered when enabled, so it costs nothing otherwise. Tailscale is a
    # no-op here (its own daemon manages connectivity).
    try:
        import os as _os
        if str(_os.getenv("DDNS_ENABLED", "")).lower() in ("1", "true", "yes") and _os.getenv("DDNS_PROVIDER"):
            reg["ddns_update"] = (("interval", getattr(s, "ddns_interval_minutes", 15)), _job_ddns_update)
    except Exception:
        pass
    # RTM publish/verify heartbeat: when WordPress is configured, periodically GET the live build
    # endpoint on example-court.org and record what it reports, so 'is the site running this exact build'
    # is answered by an active heartbeat rather than a stale webhook. Only registered when WP creds
    # exist, and the job itself degrades honestly if the site is unreachable.
    try:
        import os as _os2
        if _os2.getenv("WP_URL") and _os2.getenv("WP_USERNAME") and _os2.getenv("WP_APP_PASSWORD"):
            reg["rtm_verify"] = (("interval", getattr(s, "rtm_verify_minutes", 30)), _job_rtm_verify)
            # Drain the WordPress director-action queues on a short cadence so a portal click applies on
            # its own (the nudge makes it instant when the Mac is up; this is the catch-up safety net).
            reg["rtm_queue_sync"] = (("interval", getattr(s, "rtm_queue_sync_minutes", 2)), _job_rtm_queue_sync)
            # Once a day, check whether a scheduled half-year demand run has reached its prepare-date; if
            # so, preview it and alert the owner to confirm. No-op on every other day.
            reg["rtm_demand_prepare"] = (("daily", getattr(s, "rtm_demand_prepare_hour", 8), getattr(s, "rtm_demand_prepare_minute", 30)), _job_rtm_demand_prepare)
            # Scheduled letters (e.g. notices emailed at 9am): checked every minute, sends only armed batches.
            reg["rtm_scheduled_letters"] = (("interval", 1), _job_rtm_scheduled_letters)
    except Exception:
        pass
    # post mongoose's version-handshake to peers the moment the connection opens (fires only on the
    # unreachable->reachable transition, so peers get exactly one greeting). Strictly gated: only
    # registered when protocol_v3_enabled is True, and the job itself re-checks the gate. Costs nothing
    # when v3 is off.
    if getattr(s, "protocol_v3_enabled", False):
        reg["v3_handshake"] = (("interval", getattr(s, "v3_handshake_minutes", 2)), _job_v3_handshake)
        reg["v3_reconcile"] = (("interval", getattr(s, "v3_reconcile_minutes", 3)), _job_v3_reconcile)
    # Time Alerts: fire due alarms/timers/reminders. Runs every minute (the scheduler tick), so
    # alarms fire within ~60s of their time , fine for wake-ups, and stated honestly as such.
    reg["fire_alerts"] = (("interval", 1), _job_fire_alerts)
    # Diagnostics: check whether the accumulated two-lane diagnostics have crossed the "report
    # ready" line (weight threshold OR max age) and, if so, notify ONCE that a report can be
    # uploaded to Claude. Cheap (reads un-reported event weights); only notifies on the transition,
    # and collection carries on regardless. On by default , it never sends data anywhere, it just
    # tells the user a local report is ready to export.
    reg["diagnostics_notify"] = (("interval", getattr(s, "diagnostics_check_minutes", 30)),
                                 _job_diagnostics_notify)
    # RTM operational (hourly): refresh overdue demand statuses so the dashboard/arrears stay current
    # without the director running anything. Read/compute only , sends nothing, moves no money.
    reg["rtm_refresh"] = (("interval", getattr(s, "rtm_refresh_minutes", 60)), _job_rtm_refresh)
    # Metro CSV folder-watch: only registered when a drop folder is configured, so it
    # costs nothing otherwise. Imports any newly-dropped Metro export and archives it;
    # the irreducible manual step is the export click in Metro online banking (no
    # scheduled/emailed CSV exists, and an AISP feed or login-driving script are both
    # deliberately out of scope). Local file read only; no bank, no money movement.
    if getattr(s, "metro_watch_dir", ""):
        reg["metro_watch"] = (("interval", getattr(s, "metro_watch_minutes", 10)), _job_metro_watch)
    # RTM operational (daily): a director digest , compute the dashboard and nudge ONCE if there are
    # real next-actions (arrears, payments to allocate, payables due, budget overruns). Points the
    # director at what needs them; never sends demands.
    _rtm_dh, _rtm_dm = getattr(s, "rtm_digest_time", (8, 0))
    reg["rtm_digest"] = (("daily", _rtm_dh, _rtm_dm), _job_rtm_digest)
    # Postal packs, nightly and BEFORE the morning digest, so the demand and chaser PDFs the director can
    # download already match the day's figures. Change-detected, so a quiet night converts nothing.
    _pk_h, _pk_m = getattr(s, "rtm_packs_time", (4, 40))
    reg["rtm_packs"] = (("daily", _pk_h, _pk_m), _job_rtm_packs)
    # Recipe drift, checked often. The nightly rebuild covers DATA changes; this covers changes to the
    # LETTER itself, which would otherwise sit stale until the next morning.
    reg["rtm_documents_fresh"] = (("interval", getattr(s, "rtm_docs_fresh_minutes", 15)),
                                  _job_rtm_documents_fresh)
    # RTM development (weekly): keep BUILDING the RTM product , advance the example-court-management
    # build plan (WordPress + admin surface) by its ready steps, but ONLY if the plan is approved.
    # Same human gate as every build; the first run just surfaces the plan for approval. Default
    # Monday 03:00 local (a quiet window), configurable.
    # RTM development , TWO cadences (weekly alone isn't enough): a daily light tick that advances
    # the management/WordPress build a couple of steps, and a weekly deeper push. Both lock-guarded
    # (defer to ad-hoc building) and approval-gated. Defaults: daily 03:30, weekly Mon 03:00.
    _rtm_bdh, _rtm_bdm = getattr(s, "rtm_build_daily_time", (3, 30))
    reg["rtm_build_daily"] = (("daily", _rtm_bdh, _rtm_bdm), _job_rtm_build_daily)
    _rtm_bd, _rtm_bh, _rtm_bm = getattr(s, "rtm_build_schedule", (0, 3, 0))
    reg["rtm_build"] = (("weekly", _rtm_bd, _rtm_bh, _rtm_bm), _job_rtm_build)
    # RTM portal: always registered; each run checks readiness itself and returns
    # {"skipped": reason} until the portal is genuinely ready (keypair + WP configured).
    # This way configuring the portal after boot needs no restart, and registry builds
    # stay free of DB access.
    reg["portal_process_claims"] = (("interval", 15), _job_portal_process_claims)
    reg["portal_pull_issues"] = (("interval", 60), _job_portal_pull_issues)
    reg["portal_snapshot_guard"] = (("interval", getattr(s, "portal_guard_minutes", 12)), _job_portal_snapshot_guard)
    reg["verify_receipt_sends"] = (("interval", getattr(s, "verify_sends_minutes", 15)), _job_verify_receipt_sends)
    reg["rtm_docs_recorrelate"] = (("interval", 240), _job_rtm_docs_recorrelate)
    reg["rtm_docs_analyse"] = (("interval", 10), _job_rtm_docs_analyse)

    # Song exchange: weekly ask (Sunday 18:00 local) + daily reminder/feedback-request pass
    # TLS certificate renewal: weekly check, regenerates the self-signed cert in place when it
    # is within 30 days of expiry (the phone cockpit dies hard on an expired cert otherwise).
    reg["tls_renewal"] = (("weekly", 1, 4, 45), _job_tls_renewal)
    # Database retention + maintenance: nightly trim of log/cache tables, WAL checkpoint,
    # and a space-reclaiming VACUUM only when the freelist is genuinely large.
    reg["db_maintenance"] = (("daily", 4, 15), _job_db_maintenance)
    reg["song_exchange_weekly"] = (("weekly", 6, 18, 0), _job_song_exchange_weekly)
    reg["song_exchange_reminders"] = (("daily", 10, 0), _job_song_exchange_reminders)
    # Sync checkpoints , THREE a day: Mongoose checks in with the owner's Claude and chat ("this is done,
    # this came up from the data, what do you think?"). Default morning/midday/evening, configurable.
    _cp_times = getattr(s, "sync_checkpoint_times", ((8, 30), (13, 0), (18, 30)))
    for i, (ch, cm) in enumerate(_cp_times):
        reg[f"sync_checkpoint_{i+1}"] = (("daily", ch, cm), _job_sync_checkpoint)
    return reg


async def _job_diagnostics_notify() -> dict[str, Any]:
    """If the two-lane diagnostics have crossed the report-ready threshold, push one notification
    that a report can be exported and uploaded to Claude. Notifies only on the transition; never
    exports automatically (the user exports when they choose); collection continues."""
    from .diagnostics import maybe_notify_report_ready
    res = await maybe_notify_report_ready()
    return {"notified": res.get("notified", False),
            "ready": res.get("status", {}).get("ready", False)}


async def _job_fire_alerts() -> dict[str, Any]:
    """Fire any time alerts that are due (phone push + optional speaker announce)."""
    from ..services.time_alerts_fire import fire_due_alerts
    return await fire_due_alerts()


async def _job_ops_health() -> dict[str, Any]:
    """Proactively check critical subsystems (off-machine backup freshness first) and PUSH the owner if
    one has silently failed, deduped to once/day per issue. This is the wire that turns a RECORDED failure
    into an ALERT: a backup that stops working must reach the phone, not just the logs and the Backups
    card (a real 7-week silent Drive-backup failure is why this exists)."""
    from .ops_health import run_ops_health_check
    return await run_ops_health_check()


async def _job_model_trial() -> dict[str, Any]:
    """Window-only (5-8am), ~weekly: run the conversational model trial, the current chat model plus
    candidates put through the real persona on genuine tests (identity honesty, handling a correction,
    warmth, no-confabulation, a joke), recording latency + the full replies, then push M a latency
    summary. Heavy (loading a candidate evicts the pinned chat model), so it is window-gated and the
    harness re-pins the live model at the end. Born from the qwen3:14b conversational regression: it makes
    'is the current model good enough, is a candidate better' a repeatable measurement, not a guess."""
    import os
    import subprocess
    from pathlib import Path
    from ..db import get_app_state, set_app_state
    last = get_app_state("model_trial_last_run")
    if last:
        try:
            if (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days < 6:
                return {"skipped": "model trial ran within the last 6 days"}
        except Exception:
            pass
    app_dir = Path(__file__).resolve().parents[3]          # .../app
    script = Path.home() / ".mongoose" / "model_trial.py"
    if not script.exists():
        return {"skipped": "model_trial.py harness not present"}
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"

    def _run() -> Any:
        return subprocess.run([str(app_dir / ".venv" / "bin" / "python"), str(script)],
                              cwd=str(app_dir), env=env, capture_output=True, text=True, timeout=1800)
    try:
        await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    set_app_state("model_trial_last_run", utc_now_iso())
    summary = "Model trial ran."
    try:
        data = json.loads((Path.home() / ".mongoose" / "model_trial_results.json").read_text())
        parts = [f"{m.get('model')}: avg {m.get('avg_latency_s')}s" for m in data.get("models", [])
                 if m.get("avg_latency_s") is not None]
        if parts:
            summary = "Model trial done, " + "; ".join(parts) + ". Replies captured for a quality read."
    except Exception:
        pass
    try:
        from .notifications import send_to_phone
        await send_to_phone("Model trial done", summary[:280], priority="default", tags="benchmark")
    except Exception:
        pass
    return {"ok": True, "summary": summary}


async def _job_resource_guard() -> dict[str, Any]:
    """Cheap OS-level pressure check. Records warnings and lets status/chat see them.

    This job itself never kills a process; it is the early-warning lane. The scheduler uses the same
    assessment to skip deferrable work when pressure is amber/red.
    """
    from .resource_guard import assess_runtime_pressure
    snap = assess_runtime_pressure(include_process_table=False)
    # Stuck-worker alarm (observability only, never kills a thread): timed-out jobs keep running in the
    # shared ThreadPoolExecutor until they finish on their own (Python can't safely kill them). A genuinely
    # hung network/model call can pile these up. Surface it through the same warnings/level channel the
    # resource guard already uses so status/chat can see "something is degraded" instead of it being silent.
    _STUCK_WORKER_ALARM_THRESHOLD = 5
    try:
        active = active_job_status()
        over = [a for a in active if a.get("over_timeout")]
        if len(over) >= _STUCK_WORKER_ALARM_THRESHOLD:
            names = ", ".join(sorted({str(a.get("job")) for a in over}))
            snap.setdefault("warnings", []).append(
                f"{len(over)} scheduler worker threads are stuck past their timeout ({names}); "
                "they can't be killed and are holding shared threadpool slots")
            snap["stuck_workers"] = len(over)
            snap["stuck_worker_jobs"] = sorted({str(a.get("job")) for a in over})
            if snap.get("level") not in ("red",):
                snap["level"] = "amber"
            snap["ok"] = False
    except Exception as exc:
        snap["stuck_worker_check_error"] = str(exc)[:160]
    return snap


async def _job_readiness_refresh() -> dict[str, Any]:
    """Recompute and cache the readiness snapshot so voice/phone stay fresh and fast."""
    from ..services.readiness import refresh_readiness
    rep = refresh_readiness()
    return {"overall": rep.get("overall"), "attention": rep.get("attention_count", 0)}


async def _job_warm_status_caches() -> dict[str, Any]:
    """Keep the cockpit SWR status caches hot WHILE the server runs, not only at boot, so a poll after a
    quiet spell is instant instead of paying a cold compute (which starves the loop and drops the tunnel
    origin). Calls each cached read endpoint (all no-arg); a per-endpoint error can't sink the rest."""
    from .. import app as _app
    warmed = 0
    for fn in (_app.cockpit_readiness, _app.cockpit_liveness, _app.cockpit_tuning, _app.cockpit_daily):
        try:
            fn(); warmed += 1
        except Exception:
            pass
    for afn in (_app.release_check_endpoint,):
        try:
            await afn(); warmed += 1
        except Exception:
            pass
    return {"ok": True, "warmed": warmed}


async def _job_surpriser() -> dict[str, Any]:
    """Offer at most one novel, well-timed, cross-domain suggestion (or stay silent)."""
    from .surpriser import run_surpriser_cycle
    return await run_surpriser_cycle()


async def _job_wellness_checkins() -> dict[str, Any]:
    """Gently nudge owner/guests to do private structured wellness self-journal check-ins."""
    from .wellness_checkins import run_checkin_cycle
    return await run_checkin_cycle(source="scheduler")


async def _job_tuning_check() -> dict[str, Any]:
    """Lane 2: evaluate model tuning. If it has newly VESTED with an actionable (or just
    auto-applied) recommendation, push ONE phone nudge, then never again for that same
    recommendation (so it surfaces at the right moment without nagging)."""
    from ..model_tuning import evaluate
    from ..db import get_app_state, set_app_state
    from .notifications import send_to_phone
    ev = evaluate(allow_auto_apply=True)  # this is where a large+safe win auto-applies
    if not ev.get("vested") or not (ev.get("actionable") or ev.get("auto_applied")):
        return {"vested": ev.get("vested", False), "nudged": False}
    sig = ev.get("signature", "")
    if get_app_state("lane2_nudged_sig", "") == sig:
        return {"vested": True, "nudged": False, "reason": "already nudged this recommendation"}
    _auto = bool(ev.get("auto_applied"))
    title = "Mongoose tuned itself" if _auto else "A better setup for Mongoose"
    # Deterministic fallback text (the floor if the model is unreachable).
    body = ("I switched to a clearly better, safe model setup based on real benchmarks. Open the cockpit "
            "to see or undo it.") if _auto else ("Enough real use to suggest better models for how you "
            "actually use it. Open the cockpit to review.")
    # Ground the body in the REAL tuning facts so it is Mongoose's voice, not a fixed line (always-think).
    try:
        from ..llm import ask_persona
        facts = (f"auto_applied={_auto}. Reason: {ev.get('reason','')}. "
                 f"Proposed model change: {ev.get('proposed') or ev.get('recommendation')}.")
        exp = ("Write ONE short phone-notification line (1-2 sentences) telling the owner you " +
               ("just auto-applied a better, safe model setup" if _auto else "have a better model setup to suggest") +
               ", grounded in the facts, and point them to the cockpit to review" +
               (" or undo it" if _auto else "") + ". Your own voice, no hype, do not invent model names.")
        out = (await ask_persona(f"{exp}\n\nFacts (use only these): {facts}", role="chat")).strip()
        if out:
            body = out
    except Exception:
        pass
    pushed = await send_to_phone(title, body, priority="default", tags="gear")
    # Record "already nudged this recommendation" only once the nudge truly landed; a failed push must
    # not silence the one chance to surface a better model setup.
    if (pushed or {}).get("sent"):
        set_app_state("lane2_nudged_sig", sig)
    return {"vested": True, "nudged": (pushed or {}).get("sent", False), "auto_applied": ev.get("auto_applied", False)}


def _portal_ready_or_skip() -> dict[str, Any] | None:
    """Cheap readiness gate for portal jobs: no network, no DB writes on the hot path.
    Returns a skip result when the portal cannot act yet, else None."""
    # Security kill-switch: MONGOOSE_PORTAL_SYNC=off halts ALL WordPress sync (push AND pull) so a
    # possibly-compromised WordPress can neither be trusted for inbound data nor written to. Set back to
    # anything else to resume once the site is verified clean.
    if (os.getenv("MONGOOSE_PORTAL_SYNC", "on") or "").strip().lower() == "off":
        return {"skipped": "portal sync paused (MONGOOSE_PORTAL_SYNC=off, security hold)"}
    try:
        from .rtm_portal import _load_keypair, _nacl, _wp_configured
        if _nacl() is None:
            return {"skipped": "pynacl not installed (run: mongoose install-missing)"}
        if not _wp_configured():
            return {"skipped": "WordPress not configured (WP_URL/WP_USERNAME/WP_APP_PASSWORD)"}
        if not _load_keypair():
            return {"skipped": "no claim keypair (run: mongoose rtm portal generate-keypair)"}
    except Exception as exc:
        return {"skipped": f"portal readiness check errored: {exc}"}
    return None

async def _job_tls_renewal() -> dict[str, Any]:
    """Weekly: renew the self-signed TLS cert if it is within 30 days of expiry."""
    try:
        from ..self_signed_tls import renew_if_expiring
        return renew_if_expiring(days_threshold=30)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _job_db_maintenance() -> dict[str, Any]:
    """Nightly: apply retention to log/cache tables and keep SQLite healthy."""
    try:
        from .db_maintenance import run_maintenance
        return run_maintenance()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _job_rtm_docs_recorrelate() -> dict[str, Any]:
    """Living correlation for RTM documents: refresh recent documents' links so they pick up
    voicemails/emails/records that arrived after ingest. Idempotent and best-effort."""
    try:
        from .rtm_docs import recorrelate_recent
        return recorrelate_recent()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _job_rtm_docs_analyse() -> dict[str, Any]:
    """Give each ingested RTM document a thumbnail, image-classification labels (photos), and a short
    description. Bounded per run so a backlog is worked through gradually. Runs OFF the event loop
    (thumbnails, Vision, and the local model are all subprocess/blocking work). Best-effort."""
    try:
        import asyncio as _asyncio
        from .rtm_docs import documents_needing_analysis, analyse_document
        ids = await _asyncio.to_thread(documents_needing_analysis, 20)
        ids = ids[:10]
        done = 0
        for did in ids:
            res = await _asyncio.to_thread(analyse_document, did)
            if (res or {}).get("ok"):
                done += 1
        return {"ok": True, "pending": len(ids), "analysed": done}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def _job_portal_process_claims() -> dict[str, Any]:
    """Decrypt and process pending claim submissions from the WordPress portal.
    Safe to call frequently: idempotent and read-only on failure."""
    skip = _portal_ready_or_skip()
    if skip:
        return skip
    try:
        from .rtm_portal import process_pending_claims
        return process_pending_claims()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

async def _job_portal_pull_issues() -> dict[str, Any]:
    """Pull leaseholder issues from WordPress portal into RTM intake. Idempotent."""
    skip = _portal_ready_or_skip()
    if skip:
        return skip
    try:
        from .rtm_portal import pull_portal_issues
        return pull_portal_issues()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

async def _job_rtm_refresh() -> dict[str, Any]:
    """RTM operational, hourly: keep the demand register honest by flipping past-due demands to
    Overdue, so the dashboard and arrears view are current without the director running anything.
    Read/compute only, refreshes statuses, sends nothing, moves no money."""
    try:
        from .rtm_management import refresh_all_demand_statuses
        refresh_all_demand_statuses()
        # Recompute company-secretary compliance from stored profile dates too, so statutory deadlines
        # advance on their own: due_soon -> overdue as a date passes, and a filed/rolled deadline moves
        # to next year, without the director ever pressing Discover. Cheap local compute; best-effort so
        # it can never sink the demand refresh.
        compliance_ok = False
        try:
            from .rtm_secretary import compute_compliance, get_profile
            if get_profile().get("incorporation_date"):
                compute_compliance()
                compliance_ok = True
        except Exception:
            pass
        return {"ok": True, "refreshed": True, "compliance_recomputed": compliance_ok}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def _job_rtm_documents_fresh() -> dict[str, Any]:
    """Keep the published letters matching the current recipe, WITHOUT waiting for the nightly rebuild.

    Comparing one build id is cheap enough to run often, and it renders nothing when nothing has changed.
    This exists because stale documents are invisible: the letters still open and still look right, they
    are simply the previous wording, so a director posts last week's letter believing it is today's. The
    nightly job alone left a whole working day in which that could happen.
    """
    try:
        import asyncio as _aio
        from .rtm_overdue import refresh_documents_if_stale
        return await _aio.to_thread(refresh_documents_if_stale)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _job_rtm_packs() -> dict[str, Any]:
    """Nightly: regenerate each flat's postal pack and push the PDFs to WordPress.

    Runs AFTER the snapshot so the documents agree with the figures the portal is showing that morning.
    Without this the packs go stale the moment someone pays: the owner would be posting a chaser to a
    leaseholder who settled last week, which is worse than sending nothing.

    only_changed=True means a flat whose balance, demand, overdue stage and addressing are all unmoved is
    skipped entirely, so the usual night converts nothing and the job is nearly free. Sends no email and
    touches no ledger: it renders documents and uploads bytes.
    """
    try:
        import asyncio as _aio
        from .rtm_overdue import publish_packs
        r = await _aio.to_thread(publish_packs, only_changed=True)
        return {"ok": bool(r.get("ok")), "pushed": r.get("pushed", 0),
                "unchanged": r.get("unchanged", 0), "errors": r.get("errors", [])[:3]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _job_rtm_digest() -> dict[str, Any]:
    """RTM operational, daily: compute the director dashboard and, if there are real next-actions
    (bank credits to allocate, arrears, payables due soon, budget overruns, missing evidence), push
    ONE phone nudge with the top action. Notify-once per distinct action set so it surfaces without
    nagging. Read/compute only, it points the director at what needs them, sends no demands."""
    try:
        from .rtm_management import management_dashboard
        from ..db import get_app_state, set_app_state
        from .notifications import send_to_phone
        dash = management_dashboard()
        actions = [a for a in dash.get("next_actions", []) if a.get("type") != "ok"]
        # Fold in statutory obligations that are overdue or due soon, so the company surfaces its own
        # forward legal deadlines (acquisition, accounts, insurance renewal) without the director
        # tracking dates. These become first-class next-actions in the same notify-once stream.
        try:
            from .rtm_obligations import obligations_summary
            osum = obligations_summary()
            for o in osum.get("due_soon", []):
                _dr = o["days_remaining"]
                _when = "overdue" if _dr < 0 else f"in {_dr}d"
                actions.append({
                    "type": "statutory_deadline",
                    "title": f"{o['title']}: {_when} ({o['due_date']})",
                })
        except Exception:
            pass
        if not actions:
            return {"ok": True, "nudged": False, "reason": "no actions"}
        # signature of the current action set, so we nudge once per change
        sig = "|".join(sorted(f"{a.get('type')}:{a.get('title')}" for a in actions))
        if get_app_state("rtm_digest_sig", "") == sig:
            return {"ok": True, "nudged": False, "reason": "already nudged this action set"}
        n = len(actions)
        from .. import profile as _profile
        org = _profile.rtm_name()
        title = f"{org}: action needed" if n == 1 else f"{org}: {n} actions"
        # List EVERY action, not just the first , the count said "2 actions" while the body showed one.
        if n == 1:
            body = actions[0].get("title", "Open the RTM dashboard.")
        else:
            body = "\n".join("- " + (a.get("title") or "action") for a in actions)
        pushed = await send_to_phone(title, body, priority="default", tags="house")
        set_app_state("rtm_digest_sig", sig)
        return {"ok": True, "nudged": pushed.get("sent", False), "actions": n}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def _advance_rtm_build(holder: str, *, max_steps: int) -> dict[str, Any]:
    """Shared RTM build advance, used by both the daily and weekly cadence jobs. Takes the build
    LOCK first so it never collides with ad-hoc building; if the lock is held (someone's iterating
    by hand), it defers cleanly. Only advances an APPROVED plan; otherwise surfaces it. Records a
    build candidate of origin 'scheduled' so the work is tracked in the candidate model."""
    from .build_plans import build_example_court_management_plan, plan_name
    from . import builder
    from . import rtm_capability as C
    lock = C.acquire_build_lock(holder)
    if not lock["ok"]:
        return {"ok": True, "built": False, "deferred": True,
                "note": f"build lock held by {lock.get('held_by')}; deferring to ad-hoc work"}
    try:
        plans = builder.list_plans() if hasattr(builder, "list_plans") else []
        mgmt = next((p for p in plans if p.get("name") == plan_name("management")), None)
        if not mgmt:
            created = build_example_court_management_plan()
            C.create_candidate(plan_name("management"), approach="generated management plan",
                               plan_id=created.get("plan_id"), origin="scheduled")
            return {"ok": True, "built": False, "created_plan": created.get("plan_id"),
                    "note": "management build plan created; approve it to let the cadence build it"}
        if not mgmt.get("approved"):
            return {"ok": True, "built": False, "plan_id": mgmt.get("plan_id"),
                    "note": "management build plan awaiting approval; not building until approved"}
        summary = builder.run_plan(mgmt["plan_id"], max_steps=max_steps)
        return {"ok": True, "built": True, "plan_id": mgmt.get("plan_id"),
                "max_steps": max_steps, "summary": summary}
    finally:
        C.release_build_lock(lock["token"])


async def _job_rtm_build_daily() -> dict[str, Any]:
    """RTM development, DAILY (lighter): advance the management build plan by a small number of steps
    each day, so the product moves forward continuously rather than only weekly. Lock-guarded and
    approval-gated. Weekly cadence alone is not enough, this is the steady drip."""
    return await _advance_rtm_build("scheduled:daily", max_steps=2)


async def _job_rtm_build() -> dict[str, Any]:
    """RTM development, WEEKLY (deeper): a larger build push (more steps) once a week, for the bigger
    chunks. Lock-guarded and approval-gated, same as the daily tick."""
    return await _advance_rtm_build("scheduled:weekly", max_steps=8)


async def _job_sync_checkpoint() -> dict[str, Any]:
    """Run one sync checkpoint: assemble (done / surfaced-from-data / asking) and deliver over the
    enabled remote channels (chat pro, claude pro, anthropic api, openai api). Three a day. This is
    Mongoose checking in with the owner's Claude and chat, a sync, not an action; it sends/changes
    nothing operationally."""
    try:
        from .sync_checkpoint import run_checkpoint
        r = await run_checkpoint()
        cp = r.get("checkpoint", {})
        return {"ok": True, "checkpoint_id": cp.get("id"), "summary": cp.get("summary"),
                "channels": list(r.get("delivery", {}).get("channels", {}).keys())}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _is_due(spec: tuple, name: str, now: datetime | None = None) -> bool:
    kind = spec[0]
    if kind == "interval":
        return _interval_due(name, int(spec[1]), now=now)
    if kind == "daily":
        return _daily_due(name, int(spec[1]), int(spec[2]), now=now)
    if kind == "weekly":
        return _weekly_due(name, int(spec[1]), int(spec[2]), int(spec[3]), now=now)
    if kind == "window":
        return _window_due(name, int(spec[1]), int(spec[2]), now=now)
    return False


# Time-critical / continuity jobs that must be dispatched before heavy model/build jobs on a busy
# tick. Kept small and by name so it's a pure ordering hint, not a semantics change.
_PRIORITY_JOBS = frozenset({"fire_alerts", "resource_guard"})


async def run_due_jobs(now: datetime | None = None) -> dict[str, Any]:
    """Run every job that's currently due, once. Returns what ran.

    Hardening: each job is isolated behind a per-job timeout and run in a worker thread, so a
    provider/model/build loop cannot wedge the main FastAPI event loop or stop every later scheduler
    job. Under OS memory pressure, deferrable/model-heavy jobs are skipped rather than making the box
    worse; safety/continuity jobs still run.
    """
    _ensure_table()
    ran: dict[str, Any] = {}
    disabled = _load_disabled_jobs()
    pressure = _resource_pressure_snapshot()
    _write_heartbeat()
    # Dispatch order matters: due jobs run sequentially in this tick, so a co-firing batch of
    # heavy 240s-class jobs would otherwise delay time-critical short jobs (alarms/timers via
    # fire_alerts, and the resource guard) by minutes. Run the priority jobs FIRST. sorted() is
    # stable, so every other job keeps its registry order; only dispatch order changes, not
    # semantics. fire_alerts still runs exactly once per tick.
    _ordered = sorted(_build_registry().items(),
                      key=lambda kv: (0 if kv[0] in _PRIORITY_JOBS else 1))
    for name, (spec, fn) in _ordered:
        if name in disabled:
            continue
        if not _is_due(spec, name, now=now):
            continue
        if _skip_deferrable_under_pressure(name, pressure, spec=spec, now=now):
            detail = {"ok": True, "skipped": "resource_pressure",
                      "pressure_level": pressure.get("level"),
                      "warnings": pressure.get("warnings", [])[:4]}
            _record_run(name, "skipped", str(detail))
            ran[name] = {"status": "skipped", "result": detail}
            audit("scheduler", name, True, payload=detail)
            continue
        try:
            result = await _run_job_with_timeout(name, fn)
            # Treat nested {"ok": false, ...} as degraded, not "ok": job ran but reported failure
            nested_ok = result.get("ok", True) if isinstance(result, dict) else True
            job_status = "ok" if nested_ok else ("timeout" if isinstance(result, dict) and result.get("timeout") else "degraded")
            _record_run(name, job_status, str(result))
            ran[name] = {"status": job_status, "result": result}
            audit("scheduler", name, nested_ok, payload=result if isinstance(result, dict) else {})
        except Exception as exc:
            _record_run(name, "error", str(exc))
            ran[name] = {"status": "error", "error": str(exc)}
            audit("scheduler", name, False, payload={"error": str(exc)})
        _write_heartbeat()
    return ran


def _job_description(name: str) -> str:
    """A job's plain-language description with the site-profile placeholders (the RTM's name, the site
    domain, the owner's name) filled in."""
    from ..settings import render_profile_copy
    return render_profile_copy(_JOB_DESCRIPTIONS.get(name, ""))


_JOB_DESCRIPTIONS: dict[str, str] = {
    "self_reflection": "Notices if a capability that exists in code stopped being reachable from the single brain, and (bounded) re-wires read-only-tool registration, telling the v3 peers.",
    "v3_handshake": "When household coordination (v3) is on, greets the other Claude agents automatically the moment the Pi link opens.",
    "v3_reconcile": "When v3 is on, catches up on the shared bus + {{rtm.name}} voicemails, acks messages, and posts a heartbeat (no household action without the owner's approval).",
    "daily_brief": "Prepares your morning brief (calendar, email, weather) and pushes it to your phone.",
    "sync_email": "Checks your email so the brief and search stay current.",
    "sync_email_sent": "Indexes your Sent folder on its own cadence so it never delays the inbox sync.",
    "sync_email_archive": "Indexes your Archive folder (where the WordPress Bcc receipts and demands are filed) so every audit copy is searchable and provable.",
    "sync_tasks": "Syncs your Microsoft To Do tasks and Outlook calendar so they stay current.",
    "delight_topup": "Pre-generates owner and guest delight snippets off the request path so the app feels responsive.",
    "self_harden_scan": "Notices what's fragile: records failures as hardening observations (evidence only, never changes code).",
    "self_heal": "Detects a subsystem whose sync has been silently failing and re-runs it to recover, recording the incident.",
    "brain_snapshot": "Takes a daily safe snapshot of your brain (conversation + memory) as a local restore point.",
    "backup": "Backs up your brain + config daily, and uploads to Google Drive (7 daily / 4 weekly / 12 monthly) if connected.",
    "benchmark": "Benchmarks models in the quiet early-morning window (5am-8am) so it never competes with daytime use.",
    "benchmark_reveal": "Lets Mongoose volunteer benchmark findings as unprompted chat messages (quiet, de-duped).",
    "wellness_checkins": "Gently reminds the owner and trusted guests to do brief private structured mindfulness/wellness self-journal check-ins.",
    "sync_imap": "Checks your IMAP email so the brief and search stay current.",
    "reindex_files": "Re-scans your indexed folders so file search stays up to date.",
    "tuning_check": "Looks at how you actually use Mongoose and benchmarks models in the background.",
    "readiness_refresh": "Refreshes the 'what can I use right now' readiness picture.",
    "warm_status_caches": "Keeps the cockpit status panels pre-computed so they open instantly.",
    "resource_guard": "Checks OS-level memory, process, event-loop and scheduler pressure so heavy work is skipped before the Mac starts thrashing.",
    "ddns_update": "Keeps your dynamic-DNS hostname pointed at your current home IP (so you stay reachable when it changes).",
    "fire_alerts": "Fires any timers/reminders/alerts that have come due.",
    "diagnostics_notify": "Surfaces any new diagnostics that need your attention.",
    "rtm_refresh": "Recomputes RTM (Right-To-Manage) arrears and demand status from the data.",
    "rtm_verify": "Checks {{site.domain}}'s live RTM build against what Mongoose deployed, so the sync strip shows a real 'running this exact build' verdict and a genuine last-heartbeat time.",
    "metro_watch": "Imports any Metro CSV you drop in the watch folder and archives it (no bank connection).",
    "rtm_digest": "Sends the daily RTM digest of what changed and what needs you.",
    "rtm_documents_fresh": "Notices when the letter wording or layout changes and republishes the documents, so a download is never a previous version.",
    "rtm_packs": "Rebuilds each flat's postable demand and chaser PDFs overnight so the download buttons never serve a letter that contradicts today's balance.",
    "rtm_build_daily": "Advances the {{rtm.name}} management build plan (approval-gated).",
    "rtm_docs_recorrelate": "Refreshes recent RTM document correlations so new mail/voicemail link in.",
    "rtm_docs_analyse": "Gives each RTM document a thumbnail, image labels, and a short description.",
    "rtm_build": "Weekly RTM build-plan advance (approval-gated).",
    "song_exchange_weekly": "Weekly, opt-in friend song exchange request.",
    "song_exchange_reminders": "Gentle reminders/feedback pass for pending song exchanges.",
    "sync_checkpoint_1": "Morning check-in: assembles a 'here's where we are' summary (sent only if you opted in).",
    "sync_checkpoint_2": "Midday check-in summary (sent only if you opted in).",
    "sync_checkpoint_3": "Evening check-in summary (sent only if you opted in).",
}


def _scheduler_running() -> bool:
    """Is the scheduler loop actually running? Primary signal is a fresh per-tick HEARTBEAT written by
    the loop itself, so liveness does not depend on a job having run recently (the old check reported a
    freshly-started loop, or one with only infrequent jobs, as 'not running'). Falls back to a recent
    job run, which also covers an externally-managed scheduler that doesn't write our heartbeat."""
    from datetime import datetime, timezone, timedelta

    def _recent(ts: str | None, hours: float) -> bool:
        if not ts:
            return False
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - t) < timedelta(hours=hours)
        except Exception:
            return False

    # 1) heartbeat: the loop writes it on start and every tick (60s), so a few minutes' tolerance
    # cleanly distinguishes a live loop from a dead one.
    try:
        from ..db import get_app_state
        if _recent(get_app_state("scheduler_heartbeat"), hours=0.1):  # ~6 min
            return True
    except Exception:
        pass
    # 2) fallback: did any job run in the last ~2h (covers an external scheduler)?
    try:
        from ..db import connect
        with connect() as con:
            row = con.execute(
                "SELECT MAX(last_run) AS m FROM scheduler_jobs WHERE last_run IS NOT NULL").fetchone()
        return _recent(row["m"] if row else None, hours=2)
    except Exception:
        return False


def job_status() -> list[dict[str, Any]]:
    """Each job's schedule, last run, and last status, for doctor/CLI."""
    _ensure_table()
    reg = _build_registry()
    disabled = _load_disabled_jobs()
    with connect() as con:
        rows = {r["name"]: dict(r) for r in con.execute(
            "SELECT name, last_run, last_status, last_detail FROM scheduler_jobs").fetchall()}
    out = []
    active = {a["job"]: a for a in active_job_status()}
    for name, (spec, _) in reg.items():
        row = rows.get(name, {})
        if spec[0] == "interval":
            sched = f"every {spec[1]}m"
        elif spec[0] == "daily":
            sched = f"daily at {spec[1]:02d}:{spec[2]:02d}"
        elif spec[0] == "weekly":
            days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
            day_name = days[spec[1]] if isinstance(spec[1], int) and 0 <= spec[1] < 7 else str(spec[1])
            sched = f"weekly {day_name} at {spec[2]:02d}:{spec[3]:02d}" if len(spec) > 3 else f"weekly {day_name}"
        elif spec[0] == "window":
            sched = f"window {spec[1]:02d}:00-{spec[2]:02d}:00"
        else:
            sched = str(spec)
        off = disabled.get(name, {})
        a = active.get(name, {})
        out.append({"job": name, "name": name, "id": name, "schedule": sched,
                    "description": _job_description(name),
                    "enabled": name not in disabled,
                    "disabled_reason": off.get("reason"),
                    "disabled_at": off.get("disabled_at"),
                    "disabled_by": off.get("disabled_by"),
                    "disabled_temporary": bool(off.get("temporary")),
                    "auto_resume_at": off.get("auto_resume_at"),
                    "active": bool(a), "active_age_seconds": a.get("age_seconds"),
                    "active_over_timeout": a.get("over_timeout"),
                    "last_run": row.get("last_run"), "last": row.get("last_run"),
                    "last_status": row.get("last_status")})
    return out


async def run_scheduler(tick_seconds: int = 60) -> None:
    """The long-running loop. Wakes every tick, runs due jobs. Never returns
    (until cancelled). This is what `mongoose scheduler` runs."""
    _ensure_table()
    audit("scheduler", "start", True, payload={"tick": tick_seconds})
    _write_heartbeat()  # record liveness immediately, so status is honest before any job fires
    while True:
        try:
            await run_due_jobs()
        except Exception as exc:
            audit("scheduler", "loop_error", False, payload={"error": str(exc)})
        _write_heartbeat()
        await asyncio.sleep(tick_seconds)


def _write_heartbeat() -> None:
    """Record that the scheduler loop is alive RIGHT NOW. Liveness must not depend on a job having
    run recently (a freshly-started loop, or one with only infrequent daily jobs, would otherwise be
    misreported as 'not running')."""
    try:
        from ..db import set_app_state
        from datetime import datetime, timezone
        set_app_state("scheduler_heartbeat", datetime.now(timezone.utc).isoformat())
    except Exception:
        pass

async def _job_song_exchange_weekly() -> dict[str, Any]:
    """Weekly: pick two users and ask one to recommend a song to the other. 🎵"""
    try:
        from .song_exchange import create_exchange
        result = await create_exchange()
        if result:
            return {"ok": True, "exchange": result}
        return {"ok": True, "skipped": "fewer than 2 pokeable users"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _job_song_exchange_reminders() -> dict[str, Any]:
    """Daily: nudge pending exchanges gently (one reminder max), request feedback at 24-48h."""
    try:
        from .song_exchange import run_reminder_pass
        return await run_reminder_pass()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
