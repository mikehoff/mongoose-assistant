from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# Install progress, the human-facing layer over bootstrap_macos.sh.
#
# The bootstrap does real work (Homebrew, Python, Ollama, venv, models, Docker services) but
# historically streamed raw logs to a Terminal and ended at a health URL. That's an engineer's
# finish line, not a person's. This module lets the bootstrap emit a small, ordered set of
# STEPS with status, which the served /install page polls and renders as a clean progress
# screen that ends by handing the user to their phone (the pairing QR).
#
# Design: a single JSON file in data/. The shell bootstrap appends step transitions via the
# `mongoose _install-step` CLI (so the shell doesn't need to know the file format). The page
# reads install_progress(). Dead simple, no daemon, survives the bootstrap being a subprocess.

# The canonical ordered steps. Keep stable: the page renders them in this order and shows
# anything not-yet-started as pending. Labels are what the human sees.
STEPS: list[tuple[str, str]] = [
    ("prerequisites", "Installing prerequisites (Homebrew, Python, Ollama)"),
    ("environment", "Setting up Mongoose's environment"),
    ("database", "Preparing the local database"),
    # The browser opens as soon as the server is healthy, then optional services and models continue.
    # Keep the rendered order aligned with bootstrap_macos.sh so the page never shows a later completed
    # step underneath an earlier pending one.
    ("server", "Starting Mongoose"),
    ("services", "Starting local services (push, web search)"),
    ("models", "Downloading the local brain (this is the long one)"),
    # Smoke test is the readiness proof: once it passes, Mongoose is usable and the install can NEVER be
    # reported as "failed before ready" again, whatever happens after. The watchdog is a best-effort
    # protective extra (auto-restart on crash); its failure is non-fatal by design.
    ("smoke", "Checking everything works"),
    ("watchdog", "Installing the auto-restart watchdog"),
    ("ready", "Ready"),
]

_STEP_KEYS = [k for k, _ in STEPS]
_LABELS = dict(STEPS)

# Human truth beats equal fractions here. The browser opens once the web server is up, but the
# slowest and most user-visible remaining task is usually model download. Equal-weight progress made
# the page look "almost done" the moment it opened, then sit there. Weighted progress keeps the early
# web-ready handoff honest without hiding the pairing QR.
_STEP_WEIGHTS: dict[str, float] = {
    "prerequisites": 8.0,
    "environment": 8.0,
    "database": 8.0,
    "services": 8.0,
    "server": 8.0,
    "models": 60.0,
    "smoke": 4.0,
    "watchdog": 2.0,
}


def _progress_path() -> Path:
    from .settings import get_settings
    return get_settings().data_dir / "install_progress.json"


def _read() -> dict[str, Any]:
    p = _progress_path()
    if not p.exists():
        return {"steps": {}, "started_at": None, "current": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"steps": {}, "started_at": None, "current": None}


def _write(state: dict[str, Any]) -> None:
    p = _progress_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, p)


def begin_install() -> None:
    """Reset progress at the start of a bootstrap run."""
    _write({"steps": {}, "started_at": time.time(), "current": None})


def set_step(key: str, status: str, detail: str = "") -> None:
    """Record a step transition. status in {running, done, failed, skipped}. Unknown keys are
    accepted (forward-compatible) but only canonical ones render in order."""
    state = _read()
    if state.get("started_at") is None:
        state["started_at"] = time.time()
    steps = state.setdefault("steps", {})
    steps[key] = {"status": status, "detail": detail, "at": time.time()}
    if status == "running":
        state["current"] = key
    if key == "ready" and status == "done":
        state["current"] = "ready"
        state["finished_at"] = time.time()
    _write(state)


def install_progress() -> dict[str, Any]:
    """The render-ready view for the /install page: every canonical step with its status,
    plus overall percent and whether we're done. A step with no record yet is 'pending'."""
    state = _read()
    recorded = state.get("steps", {})
    steps_out = []
    done_count = 0
    failed = False
    for key in _STEP_KEYS:
        rec = recorded.get(key)
        status = rec["status"] if rec else "pending"
        if status == "done":
            done_count += 1
        if status == "failed":
            failed = True
        steps_out.append({
            "key": key,
            "label": _LABELS[key],
            "status": status,
            "detail": (rec or {}).get("detail", ""),
        })
    # 'ready' is the terminal marker, not a work step, so percent is over the weighted work steps.
    # A running step earns half its own weight so the long model download visibly moves the bar, while
    # the browser opening at "server done" no longer falsely implies the install is nearly complete.
    work_steps = steps_out[:-1]
    total_weight = sum(_STEP_WEIGHTS.get(s["key"], 1.0) for s in work_steps)
    earned = 0.0
    for s in work_steps:
        weight = _STEP_WEIGHTS.get(s["key"], 1.0)
        if s["status"] in ("done", "skipped"):
            earned += weight
        elif s["status"] == "running":
            earned += 0.5 * weight
    pct = int(100 * earned / total_weight) if total_weight else 0
    ready = recorded.get("ready", {}).get("status") == "done"
    if ready:
        pct = 100
    # Time-remaining estimate: from elapsed time and how much of the weighted work is done, project the
    # remainder. Only meaningful once a little progress exists (avoids a wild number at 1%). The model
    # download dominates, so this is rough, deliberately shown as "about N minutes", never a countdown.
    eta_seconds = None
    started_at = state.get("started_at")
    if started_at and 3 <= pct < 100:
        elapsed = max(0.0, time.time() - float(started_at))
        if elapsed > 2:
            projected_total = elapsed / (pct / 100.0)
            eta_seconds = int(max(0, projected_total - elapsed))
    return {
        "started": state.get("started_at") is not None,
        "steps": steps_out,
        "progress_pct": pct,
        "eta_seconds": eta_seconds,
        "current": state.get("current"),
        "ready": ready,
        "failed": failed,
    }


# The install-phase STATE MACHINE. The wrapper must not decide success/failure from the bootstrap's
# raw exit code: that conflated "Mongoose never came up" with "Mongoose is up and serving but a
# best-effort extra (the watchdog) failed". Readiness is PROVEN by the smoke test. Once smoke passes,
# the install can never be reported as "failed before ready" again, whatever happens afterwards.
#
# Phases (ordered):
#   BOOTSTRAP_STARTED        - install began, server not yet confirmed
#   SERVER_READY             - the API came up and the /install page rendered
#   SMOKE_TEST_PASSED        - readiness proven: Mongoose is usable from here on
#   WATCHDOG_INSTALLING      - attempting to install the auto-restart agent (transient)
#   WATCHDOG_FAILED_NON_FATAL- watchdog couldn't be installed; Mongoose still fully usable
#   INSTALL_COMPLETE         - ready marked done; everything including the watchdog is in place
#   SERVER_FAILED            - a pre-readiness step failed; this is the only genuine install failure
PHASE_BOOTSTRAP_STARTED = "BOOTSTRAP_STARTED"
PHASE_SERVER_READY = "SERVER_READY"
PHASE_SMOKE_TEST_PASSED = "SMOKE_TEST_PASSED"
PHASE_WATCHDOG_INSTALLING = "WATCHDOG_INSTALLING"
PHASE_WATCHDOG_FAILED_NON_FATAL = "WATCHDOG_FAILED_NON_FATAL"
PHASE_INSTALL_COMPLETE = "INSTALL_COMPLETE"
PHASE_SERVER_FAILED = "SERVER_FAILED"


def install_phase() -> dict[str, Any]:
    """The named install phase plus the two booleans the wrapper actually branches on:
    `ready_proven` (smoke passed, so Mongoose is usable) and `complete` (ready marked done).

    This is deliberately derived from the recorded steps, not a second source of truth: the bootstrap
    only ever calls `_install-step`, and this reads the same file back."""
    state = _read()
    steps = state.get("steps", {})

    def st(key: str) -> str:
        rec = steps.get(key)
        return rec["status"] if rec else "pending"

    # A pre-readiness step failing is the only real install failure.
    pre_ready_failed = any(st(k) == "failed" for k in ("prerequisites", "environment", "database", "server", "smoke"))
    smoke_passed = st("smoke") == "done"
    ready_done = st("ready") == "done"
    watchdog_status = st("watchdog")

    # Watchdog outcome wins over the terminal ready marker for the NAMED phase. The bootstrap
    # deliberately marks `ready` even when the watchdog fails, because Mongoose is fully usable without
    # it (it is just the auto-restart-on-reboot convenience). But the named phase must still say
    # WATCHDOG_FAILED_NON_FATAL, not INSTALL_COMPLETE, or status UIs and diagnostics report a
    # contradictory "complete but watchdog not ok" state. `complete` below stays True (the ready marker
    # is real); the phase carries the warning.
    if smoke_passed and watchdog_status == "failed":
        phase = PHASE_WATCHDOG_FAILED_NON_FATAL
    elif ready_done and watchdog_status in ("done", "skipped"):
        phase = PHASE_INSTALL_COMPLETE
    elif smoke_passed and watchdog_status == "running":
        phase = PHASE_WATCHDOG_INSTALLING
    elif smoke_passed:
        # smoke passed but no terminal watchdog verdict yet (incl. ready marked before the watchdog
        # step resolved): report SMOKE_TEST_PASSED rather than INSTALL_COMPLETE, so the named phase
        # never claims completion while watchdog_ok (== watchdog in done/skipped) is still False.
        phase = PHASE_SMOKE_TEST_PASSED
    elif ready_done:
        # ready without smoke is not a real bootstrap order, but if it occurs, completion is the
        # truthful summary (watchdog_ok will reflect the watchdog state separately).
        phase = PHASE_INSTALL_COMPLETE
    elif pre_ready_failed:
        phase = PHASE_SERVER_FAILED
    elif st("server") == "done":
        phase = PHASE_SERVER_READY
    else:
        phase = PHASE_BOOTSTRAP_STARTED

    # ready_proven is the crucial gate: once smoke passes (or we're already complete), Mongoose is
    # usable and the wrapper must not print a "failed before ready" message.
    ready_proven = smoke_passed or ready_done
    watchdog_detail = (steps.get("watchdog") or {}).get("detail", "")
    return {
        "phase": phase,
        "ready_proven": ready_proven,
        "complete": ready_done,
        "watchdog_ok": watchdog_status in ("done", "skipped"),
        "watchdog_detail": watchdog_detail,
    }
