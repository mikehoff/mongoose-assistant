"""In-process runtime watchdog.

Every ~7 seconds this loop wakes up and checks that the parts of Mongoose that are supposed to be
alive actually are, and repairs the ones it can. It is deliberately HONEST about its limits: an
in-process watchdog cannot rescue a fully wedged Python process, because it cannot run while the event
loop is frozen. (For that case the BROWSER-side watchdog in the cockpit detects the freeze via the
async /health/live probe and offers a restart, the two watchdogs are complementary.) What this one
DOES catch and fix is the important middle case: a background task (the scheduler) silently dies while
the server is otherwise alive, so recurring jobs quietly stop. It also records event-loop lag and a
heartbeat so liveness is observable.

What it records into app_state.runtime_watchdog (JSON):
  - last_beat:            ISO time of the most recent watchdog tick
  - loop_lag_ms:          how much later than scheduled this tick woke (a proxy for loop pressure)
  - scheduler_alive:      whether the scheduler task is currently running
  - scheduler_restarts:   how many times this watchdog has restarted a dead scheduler
  - beats:                total ticks since start
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..db import set_app_state

WATCHDOG_KEY = "runtime_watchdog"
DEFAULT_INTERVAL_SECONDS = 7.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeWatchdog:
    """Owns the watchdog loop and the (re)start of the tasks it supervises. The supervised tasks are
    passed as (name, factory) pairs so the watchdog can recreate one if it dies. The factory must be a
    zero-arg callable returning a coroutine (i.e. `lambda: run_scheduler()`)."""

    def __init__(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS):
        self.interval = max(1.0, float(interval_seconds))
        self._task: Optional[asyncio.Task] = None
        self._supervised: dict[str, dict[str, Any]] = {}
        self._beats = 0
        self._restarts = 0
        self._stop = False
        from .resource_sentinel import ResourceSentinel
        self._sentinel = ResourceSentinel()

    def supervise(self, name: str, factory: Callable[[], Any], task: Optional[asyncio.Task]) -> None:
        """Register a background task to keep alive. `factory()` recreates it if it dies."""
        self._supervised[name] = {"factory": factory, "task": task}

    def _restart_if_dead(self) -> dict[str, bool]:
        status: dict[str, bool] = {}
        for name, rec in self._supervised.items():
            task: Optional[asyncio.Task] = rec.get("task")
            alive = bool(task) and not task.done()
            if not alive:
                # The task finished/crashed. Recreate it from its factory so the capability resumes.
                try:
                    new_task = asyncio.ensure_future(rec["factory"]())
                    rec["task"] = new_task
                    self._restarts += 1
                    alive = True
                except Exception:
                    alive = False
            status[name] = alive
        return status

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time() + self.interval
        # record an immediate heartbeat so status is honest before the first full interval elapses
        self._write({"loop_lag_ms": 0.0, "scheduler_alive": None})
        while not self._stop:
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            now = loop.time()
            # how much later than scheduled did we actually wake? (event-loop lag proxy)
            lag_ms = max(0.0, (now - expected) * 1000.0)
            expected = now + self.interval
            self._beats += 1
            status = self._restart_if_dead()
            # Proactive OS-level resource check, two complementary layers:
            #  (1) resource_guard.assess_runtime_pressure() gives an immediate green/amber/red picture
            #      (process RSS, system memory via psutil//proc/vm_stat, Ollama RSS, loop lag, scheduler
            #      heartbeat). Recorded here, and used by the scheduler to gate deferrable jobs at
            #      dispatch under amber/red.
            #  (2) the sentinel adds durable, rate-limited backoff: under SUSTAINED pressure it triggers
            #      the same safe memory-relief the owner can ask for by hand, with hysteresis so a brief
            #      spike does not flap the scheduler.
            # Neither may raise into this loop; a guard/sentinel failure must not take down the watchdog.
            resource_level = None
            resource_warnings: list[str] = []
            try:
                from .resource_guard import assess_runtime_pressure
                rg = assess_runtime_pressure(include_process_table=False)
                resource_level = rg.get("level")
                resource_warnings = list(rg.get("warnings", []))[:4]
            except Exception:
                pass
            try:
                self._sentinel.tick(lag_ms)
            except Exception:
                pass
            self._write({
                "loop_lag_ms": round(lag_ms, 1),
                "scheduler_alive": status.get("scheduler"),
                "resource_level": resource_level,
                "resource_warnings": resource_warnings,
            })

    def _write(self, extra: dict[str, Any]) -> None:
        payload: dict[str, Any] = {
            "last_beat": _now_iso(),
            "beats": self._beats,
            "scheduler_restarts": self._restarts,
        }
        payload.update(extra)
        try:
            set_app_state(WATCHDOG_KEY, json.dumps(payload))
        except Exception:
            pass

    def start(self) -> asyncio.Task:
        self._stop = False
        self._task = asyncio.ensure_future(self._run())
        return self._task

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=2.0)
            except BaseException:
                pass


def current_status() -> Optional[dict[str, Any]]:
    """Read the last watchdog status (for liveness()/the cockpit). None if it hasn't beaten yet."""
    from ..db import get_app_state
    raw = get_app_state(WATCHDOG_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None
