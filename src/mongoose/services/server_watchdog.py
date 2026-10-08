"""External server watchdog, recovers a WEDGED Mongoose server from OUTSIDE the process.

Why this exists, separate from runtime_watchdog.py: the in-process watchdog restarts dead *background
tasks* from inside the event loop. But the failure we actually hit, staging ran blocking work
(compileall/pyflakes) on the event-loop thread and froze the WHOLE loop, is invisible to anything
running inside that loop, because the loop is exactly what's stuck. /health and even /health/live stop
answering though the process is still alive. The in-process watchdog can't fire because it can't run.

So this watchdog runs as its OWN process (managed by launchd with KeepAlive). It polls /health/live,
which is async, so it only answers when the loop is actually turning, and if the server fails N
consecutive probes, it kills the owned server pid and starts a fresh one. A frozen loop can't stop its
own rescue because the rescuer is a different process.

Design choices for safety:
  - It only ever kills a pid it can PROVE is a Mongoose server (pidfile pid whose command matches the
    same ownership test `mongoose stop` uses). It never kills a stranger that happens to hold the port.
  - It is conservative: a single slow probe doesn't trigger a restart; it takes `fail_threshold`
    consecutive failures (default 3) so a transient hiccup or a legitimately long-but-progressing
    request doesn't cause a needless bounce.
  - It backs off after restarting (cooldown) so it can't get into a tight kill/respawn loop.
  - It also checks the server process RSS. If the Python server itself is exhausting memory beyond
    MONGOOSE_WATCHDOG_MAX_RSS_MB for repeated ticks, it restarts the owned server even if /health/live
    still answers. This catches memory leaks/thrashing that liveness probes alone miss.
Everything here is best-effort and defensive; a probe error is treated as a failed probe, never a crash.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_PROBE_TIMEOUT = 5.0
DEFAULT_COOLDOWN_SECONDS = 60.0
# Circuit breaker: after this many restarts that DON'T bring the server back, stop hammering and back
# off to a long interval (the server is broken in a way a restart won't fix; respawning in a tight loop
# just burns CPU and floods logs). The breaker resets once a probe succeeds.
DEFAULT_MAX_FAILED_RESTARTS = 5
DEFAULT_BREAKER_BACKOFF_SECONDS = 600.0
# Spawn throttle: when the server decides "no external watchdog heartbeat is visible" several UI
# reloads or startup hooks can all try to spawn one before the first child has had time to write its
# heartbeat. This marker prevents a supervisor-spawn storm without weakening recovery.
DEFAULT_SPAWN_THROTTLE_SECONDS = 20.0


def _markers_dir() -> Path:
    d = _data_dir() / "watchdog"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _pause_marker() -> Path:
    """Present => the watchdog must NOT restart the server. Written during a deliberate stop or a deploy
    so the watchdog doesn't fight an intentional shutdown or a self-update's own restart."""
    return _data_dir() / "watchdog" / "paused"


def pause(reason: str = "") -> None:
    """Tell the watchdog to stand down (intentional stop / deploy in progress). Best-effort."""
    try:
        _markers_dir()
        _pause_marker().write_text(f"{_now_iso()} {reason}".strip())
    except Exception:
        pass


def resume() -> None:
    """Lift a pause, the watchdog may supervise again."""
    try:
        _pause_marker().unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def is_paused() -> bool:
    try:
        return _pause_marker().exists()
    except Exception:
        return False


# ── Liveness: is a supervisor actually RUNNING? ───────────────────────────────────────────────
# Distinct from is_paused (which is about intent). The watchdog stamps a heartbeat every cycle (and
# immediately on start); the rest of the system reads its freshness to know whether anything is
# really supervising the server. This is what lets the cockpit warn "no supervisor" and what lets
# `serve` decide whether it needs to spawn one.
def _heartbeat_marker() -> Path:
    return _data_dir() / "watchdog" / "heartbeat"


def _spawn_marker() -> Path:
    return _data_dir() / "watchdog" / "spawn_attempt"


def _recent_spawn_age() -> float | None:
    try:
        ts = float(_spawn_marker().read_text().strip().split()[0])
        return max(0.0, time.time() - ts)
    except Exception:
        return None


def _write_spawn_marker(pid: int) -> None:
    try:
        _markers_dir()
        _spawn_marker().write_text(f"{time.time()} {pid}")
    except Exception:
        pass


def write_heartbeat() -> None:
    """Record that the watchdog is alive right now: '<epoch> <pid>'. Best-effort, never raises."""
    try:
        _markers_dir()
        _heartbeat_marker().write_text(f"{int(time.time())} {os.getpid()}")
    except Exception:
        pass


def seconds_since_heartbeat() -> float | None:
    """Age of the watchdog heartbeat in seconds, or None if there has never been one."""
    try:
        ts = float(_heartbeat_marker().read_text().strip().split()[0])
        return max(0.0, time.time() - ts)
    except Exception:
        return None


def is_supervised(max_age_seconds: float | None = None) -> bool:
    """True if a watchdog heartbeat exists, is fresh, and (if it names a pid) that pid is alive. The
    default staleness budget is a few probe intervals so one slow loop doesn't read as 'down'."""
    if max_age_seconds is None:
        max_age_seconds = max(DEFAULT_INTERVAL_SECONDS * 3.0, 45.0)
    age = seconds_since_heartbeat()
    if age is None or age > max_age_seconds:
        return False
    try:
        parts = _heartbeat_marker().read_text().strip().split()
        if len(parts) > 1 and not _pid_is_running(int(parts[1])):
            return False   # heartbeat recent but the process is gone (hard-killed between writes)
    except Exception:
        pass
    return True


def _launchagent_supervising(label: str = "com.mongoose.server-watchdog") -> bool:
    """True if the KeepAlive LaunchAgent that runs the server is loaded. In the LaunchAgent-as-sole-
    supervisor model (the current model: the plist runs `mongoose serve` with RunAtLoad + KeepAlive)
    THIS is the supervisor, and there is no runtime-watchdog heartbeat by design. So a stale heartbeat
    must NOT be reported as 'no supervisor': that false alarm also tempts a click of 'Start
    supervisor', which would spawn a SECOND supervisor and cause the dual-supervisor flapping."""
    try:
        r = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=4)
        return r.returncode == 0
    except Exception:
        return False


def supervisor_status() -> dict[str, Any]:
    """Small snapshot for the cockpit: is a supervisor running, how stale, ever seen, paused. A
    supervisor is present if EITHER the runtime watchdog is heartbeating OR the KeepAlive LaunchAgent
    is loaded (the current supervision model)."""
    age = seconds_since_heartbeat()
    hb = is_supervised()
    la = _launchagent_supervising()
    return {
        "running": bool(hb or la),
        "source": ("watchdog" if hb else ("launchagent" if la else None)),
        "age_seconds": (round(age, 1) if age is not None else None),
        "ever_seen": age is not None,
        "paused": is_paused(),
    }


def spawn_detached(reason: str = "") -> dict[str, Any]:
    """Start the external watchdog as a DETACHED process (start_new_session) so it outlives whatever
    launched it, including a server that is about to restart or die. No-op if a fresh heartbeat says
    one is already supervising. If a spawn was attempted moments ago, also no-op: the child may not
    have written its first heartbeat yet, and repeated browser reloads/startup hooks must not create a
    stack of supervisors before the first one gets a chance to announce itself."""
    if is_supervised():
        return {"ok": True, "already_running": True}
    age = _recent_spawn_age()
    if age is not None and age < DEFAULT_SPAWN_THROTTLE_SECONDS:
        return {"ok": True, "spawn_pending": True, "last_spawn_age_seconds": round(age, 1),
                "note": "A watchdog spawn was attempted moments ago; waiting for its heartbeat."}
    try:
        import sys as _sys
        root = Path.cwd()
        logs = _data_dir() / "logs"
        try:
            logs.mkdir(parents=True, exist_ok=True)
            out = open(logs / "server-watchdog.out.log", "ab")
        except Exception:
            out = subprocess.DEVNULL
        proc = subprocess.Popen([_sys.executable, "-m", "mongoose.cli", "server-watchdog"],
                                stdout=out, stderr=out, start_new_session=True, cwd=str(root))
        _write_spawn_marker(proc.pid)
        _log_action("watchdog_spawned", {"pid": proc.pid, "reason": reason})
        return {"ok": True, "spawned_pid": proc.pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _log_path() -> Path:
    return _data_dir() / "watchdog" / "actions.log"


def _log_action(event: str, detail: dict[str, Any]) -> None:
    """Append a one-line JSON record of what the watchdog did, so there's an audit trail of every
    restart and why. Best-effort; never raises."""
    try:
        import json as _json
        _markers_dir()
        rec = {"ts": _now_iso(), "event": event, **detail}
        with open(_log_path(), "a") as f:
            f.write(_json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists but owned by someone else
    except Exception:
        return False


def _data_dir() -> Path:
    try:
        from ..settings import get_settings
        s = get_settings()
        d = getattr(s, "data_dir", None)
        if d:
            return Path(d)
    except Exception:
        pass
    # Absolute fallback, never the relative "data" (which resolved against the process cwd , the same
    # trap that spawned a phantom brain under ~/.mongoose/app/data).
    return Path(os.environ.get("MONGOOSE_DATA_DIR") or "~/.mongoose/data").expanduser()


def _pidfile() -> Path:
    return _data_dir() / "mongoose-server.pid"


def read_pidfile() -> tuple[int | None, int | None]:
    """Return (pid, port) from the server pidfile, or (None, None). The pidfile is two lines: pid, port."""
    try:
        txt = _pidfile().read_text().strip().splitlines()
        pid = int(txt[0]) if txt and txt[0].strip() else None
        port = int(txt[1]) if len(txt) > 1 and txt[1].strip() else None
        return pid, port
    except Exception:
        return None, None


def _pid_is_mongoose_server(pid: int) -> bool:
    """Prove pid is OUR server before we ever kill it, the same ownership test `mongoose stop` uses,
    so we never kill a stranger that merely holds the port."""
    try:
        from ..cli import _pid_looks_like_mongoose_server
        return bool(_pid_looks_like_mongoose_server(pid))
    except Exception:
        # Fallback: check the command line directly.
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=5)
            low = (out.stdout or "").lower()
            return ("mongoose" in low and " serve" in low) or ("uvicorn" in low and "mongoose.app:app" in low)
        except Exception:
            return False


def probe_live(port: int, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
    """Hit /health/live. Because that endpoint is ASYNC, a prompt 200 means the event loop is turning.
    Any timeout / connection error / non-200 counts as NOT live (the thing we recover from).

    HTTPS is the default in 1.1 and first-run certs are self-signed, so a loopback watchdog probe must
    try https with verification disabled before falling back to plain http for MONGOOSE_TLS=off installs.
    """
    try:
        from ..local_probe import local_base_candidates
        bases = local_base_candidates(port)
    except Exception:
        bases = [f"https://127.0.0.1:{port}", f"http://127.0.0.1:{port}"]
    for base in bases:
        try:
            r = httpx.get(f"{base}/health/live", timeout=timeout, verify=False)
            if r.status_code == 200:
                return True
        except Exception:
            continue
    return False


def _kill_pid(pid: int, *, grace_seconds: float = 5.0) -> bool:
    """SIGTERM, wait briefly, then SIGKILL if still alive. Returns True if the process is gone after."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)        # still alive?
        except ProcessLookupError:
            return True
        except Exception:
            break
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    time.sleep(0.3)
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except Exception:
        return False


def _server_binary() -> list[str]:
    """How to start the server. Prefer the installed `mongoose` console script in this venv; fall back
    to `python -m mongoose.cli`."""
    import sys
    venv_bin = Path(sys.executable).parent / "mongoose"
    if venv_bin.exists():
        return [str(venv_bin), "serve"]
    return [sys.executable, "-m", "mongoose.cli", "serve"]


def restart_server(port: int, *, repo_root: Path | None = None) -> dict[str, Any]:
    """Kill the owned wedged server (if any) and spawn a fresh one on the same port. Returns a report.
    Refuses to kill a pid that isn't provably our server, and re-verifies ownership immediately before
    the kill to avoid a TOCTOU race where the pid was recycled between the probe and the kill."""
    pid, pidport = read_pidfile()
    target_port = port or pidport or 8765
    killed = False
    if pid is not None:
        if not _pid_is_running(pid):
            # Stale pidfile: the server already exited (e.g. crashed). Nothing to kill; just respawn.
            _log_action("stale_pid", {"pid": pid, "port": target_port})
        elif _pid_is_mongoose_server(pid):
            # Re-verify ownership RIGHT before the kill , the pid could have been reused since the probe.
            if _pid_is_mongoose_server(pid):
                killed = _kill_pid(pid)
            else:
                return {"ok": False, "action": "none",
                        "detail": f"pid {pid} changed identity before kill; refusing.", "port": target_port}
        else:
            _log_action("refused_foreign_pid", {"pid": pid, "port": target_port})
            return {"ok": False, "action": "none",
                    "detail": f"pidfile pid {pid} is not provably a Mongoose server; refusing to kill it.",
                    "port": target_port}
    # Start a fresh server, detached, logging where the installer does. Use a context manager so the log
    # file handles are not leaked (Popen keeps the fds it needs; we can close our references after).
    root = repo_root or Path.cwd()
    logs = _data_dir() / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    argv = _server_binary() + ["--port", str(target_port)]
    try:
        with open(logs / "mongoose.out.log", "ab") as out_log, \
             open(logs / "mongoose.err.log", "ab") as err_log:
            proc = subprocess.Popen(argv, cwd=str(root), stdout=out_log, stderr=err_log,
                                    start_new_session=True)
        _log_action("restarted", {"killed": killed, "new_pid": proc.pid, "port": target_port})
        return {"ok": True, "action": "restarted", "killed": killed, "new_pid": proc.pid,
                "port": target_port}
    except Exception as exc:
        _log_action("restart_failed", {"killed": killed, "error": str(exc), "port": target_port})
        return {"ok": False, "action": "restart_failed", "killed": killed,
                "error": str(exc), "port": target_port}


def watch_once(state: dict[str, Any], *, fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
               cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
               probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
               max_failed_restarts: int = DEFAULT_MAX_FAILED_RESTARTS,
               breaker_backoff_seconds: float = DEFAULT_BREAKER_BACKOFF_SECONDS,
               repo_root: Path | None = None) -> dict[str, Any]:
    """One watchdog tick. `state` carries consecutive_failures, last_restart, failed_restarts and
    breaker_until between calls (so this is pure and testable). Returns the updated state plus what
    happened this tick.

    Hardened logic:
      - If a PAUSE marker is set (deliberate stop or in-progress deploy), stand down, never restart.
        This is what makes `mongoose stop` and self-update actually work instead of being fought.
      - If the CIRCUIT BREAKER is open (too many restarts that didn't bring the server back), back off
        until breaker_until, a broken-in-a-way-restart-won't-fix server shouldn't be respawned in a
        tight loop. A successful probe closes the breaker.
      - Otherwise: probe /health/live. Success resets the failure counter AND the breaker. Failure
        increments; once it reaches fail_threshold and we're past the cooldown, restart. If the restart
        does not result in a live server (checked on subsequent ticks), failed_restarts climbs; at
        max_failed_restarts the breaker opens."""
    now = time.time()

    # 1) Deliberate pause wins over everything.
    if is_paused():
        state["consecutive_failures"] = 0
        return {**state, "event": "paused"}

    # 2) Circuit breaker open => back off.
    breaker_until = state.get("breaker_until", 0.0)
    if breaker_until and now < breaker_until:
        # Still probe so a recovered server closes the breaker, but do not restart.
        pid, port = read_pidfile()
        if port and probe_live(port, timeout=probe_timeout):
            state["consecutive_failures"] = 0
            state["failed_restarts"] = 0
            state["breaker_until"] = 0.0
            return {**state, "event": "recovered_breaker_closed", "port": port}
        return {**state, "event": "breaker_open", "breaker_until": breaker_until}

    pid, port = read_pidfile()
    if not port:
        state["consecutive_failures"] = 0
        return {**state, "event": "no_pidfile"}

    live = probe_live(port, timeout=probe_timeout)
    if live:
        # A live loop can still be a sick process: check the Python server's RSS as an OS-level guard
        # against memory leaks / pathological indexing / runaway in-process model helpers. The default
        # threshold is deliberately high and configurable; Ollama's own model memory is NOT counted here.
        mem_report = {"exceeded": False}
        try:
            if pid is not None:
                from .resource_guard import server_rss_exceeds_restart_threshold
                mem_report = server_rss_exceeds_restart_threshold(pid)
        except Exception:
            mem_report = {"exceeded": False}
        if mem_report.get("exceeded"):
            mem_fails = state.get("memory_failures", 0) + 1
            state["memory_failures"] = mem_fails
            last_restart = state.get("last_restart", 0.0)
            if mem_fails >= fail_threshold and (now - last_restart) >= cooldown_seconds:
                report = restart_server(port, repo_root=repo_root)
                state["memory_failures"] = 0
                state["last_restart"] = now
                state["restarts"] = state.get("restarts", 0) + 1
                state["awaiting_recovery"] = True
                return {**state, "event": "memory_restarted", "report": report, "memory": mem_report, "port": port}
            return {**state, "event": "memory_pressure", "memory": mem_report, "port": port}
        # Healthy , reset everything, including any restart-failure tally and breaker.
        state["consecutive_failures"] = 0
        state["memory_failures"] = 0
        state["failed_restarts"] = 0
        state["breaker_until"] = 0.0
        return {**state, "event": "live", "port": port, "memory": mem_report}

    fails = state.get("consecutive_failures", 0) + 1
    state["consecutive_failures"] = fails
    last_restart = state.get("last_restart", 0.0)

    # If we restarted on a previous tick and the server is STILL not live now, that restart didn't work.
    # Count it toward the breaker. (Independent of cooldown so the breaker works even with cooldown=0.)
    if state.get("awaiting_recovery"):
        state["failed_restarts"] = state.get("failed_restarts", 0) + 1
        state["awaiting_recovery"] = False
        if state["failed_restarts"] >= max_failed_restarts:
            state["breaker_until"] = now + breaker_backoff_seconds
            _log_action("breaker_open",
                        {"failed_restarts": state["failed_restarts"], "backoff_s": breaker_backoff_seconds})
            return {**state, "event": "breaker_opened", "failed_restarts": state["failed_restarts"]}

    if fails >= fail_threshold and (now - last_restart) >= cooldown_seconds:
        report = restart_server(port, repo_root=repo_root)
        state["consecutive_failures"] = 0
        state["last_restart"] = now
        state["restarts"] = state.get("restarts", 0) + 1
        state["awaiting_recovery"] = True   # next tick checks whether this restart actually helped
        return {**state, "event": "restarted", "report": report, "port": port}
    return {**state, "event": "probe_failed", "consecutive_failures": fails, "port": port}


def run_forever(*, interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
                cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                probe_timeout: float = DEFAULT_PROBE_TIMEOUT) -> int:
    """The watchdog main loop (run as its own launchd-managed process). Polls forever; never raises out."""
    state: dict[str, Any] = {"consecutive_failures": 0, "last_restart": 0.0, "restarts": 0}
    root = Path.cwd()
    write_heartbeat()   # immediately, so liveness is detectable within ~1s of spawn
    while True:
        try:
            write_heartbeat()   # before probing, so a slow probe still reads as 'alive'
            result = watch_once(state, fail_threshold=fail_threshold,
                                 cooldown_seconds=cooldown_seconds, probe_timeout=probe_timeout,
                                 repo_root=root)
            if result.get("event") == "restarted":
                # Give the new server time to bind before the next probe so we don't immediately
                # re-fail it during startup.
                time.sleep(max(interval_seconds, 10.0))
        except Exception:
            pass
        time.sleep(interval_seconds)
