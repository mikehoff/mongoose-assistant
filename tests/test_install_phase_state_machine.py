"""The install-phase state machine and the wrapper's outcome class.

The post-mortem: an install that started the API, served HTTPS, and passed 14/14 smoke tests was
reported as "Install failed before Mongoose became ready" because a best-effort post-readiness step
(the auto-restart watchdog) exited non-zero and the wrapper branched on the raw exit code. The
technical state (ready) and the human message (failed) disagreed.

These tests pin the fix: readiness is proven at the smoke step; once that passes the install is a
success whatever the watchdog does, and only a genuine pre-readiness failure is reported as failed.
"""
from __future__ import annotations

import tempfile

import pytest

from mongoose import install_progress as P


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MONGOOSE_DATA_DIR", d)
    # install_progress resolves the path via settings.data_dir; point it at the temp dir
    import mongoose.settings as S
    S.get_settings.cache_clear() if hasattr(S.get_settings, "cache_clear") else None
    yield


def _advance_to_smoke_passed():
    P.begin_install()
    for k in ("prerequisites", "environment", "database", "server", "services", "models"):
        P.set_step(k, "done")
    P.set_step("smoke", "done")


def test_smoke_passed_then_watchdog_failed_is_ready_proven():
    """THE log scenario: smoke passed, watchdog failed. Must be ready_proven (a success)."""
    _advance_to_smoke_passed()
    P.set_step("watchdog", "running")
    P.set_step("watchdog", "failed", "launchctl unavailable")
    ph = P.install_phase()
    assert ph["ready_proven"] is True
    assert ph["phase"] == P.PHASE_WATCHDOG_FAILED_NON_FATAL
    assert ph["watchdog_ok"] is False
    assert "launchctl" in ph["watchdog_detail"]


def test_ready_done_after_watchdog_failed_still_reports_non_fatal_watchdog_phase():
    """Real bootstrap order when the watchdog fails is: smoke done, watchdog failed, ready done. The
    wrapper still exits successfully (ready_proven=True), but the named phase must not lie and call this
    INSTALL_COMPLETE while watchdog_ok=False. (Credit: parallel auditor's 1.1.11 phase-honesty finding.)"""
    _advance_to_smoke_passed()
    P.set_step("watchdog", "failed", "launchctl unavailable")
    P.set_step("ready", "done")
    ph = P.install_phase()
    assert ph["ready_proven"] is True
    assert ph["phase"] == P.PHASE_WATCHDOG_FAILED_NON_FATAL
    assert ph["watchdog_ok"] is False
    assert ph["complete"] is True  # ready marker exists; the named phase carries the warning


def test_phase_complete_never_contradicts_watchdog_ok():
    """Invariant (the durable gate, not just one scenario): for EVERY watchdog state with the ready
    marker written, the named phase must never be INSTALL_COMPLETE while watchdog_ok is False. Reporting
    'complete' and 'watchdog not ok' simultaneously is the contradictory state that confuses diagnostics
    and UI. This sweeps all watchdog statuses to lock the invariant in permanently."""
    for wd in ("failed", "done", "skipped", "running", None):
        _advance_to_smoke_passed()
        if wd is not None:
            P.set_step("watchdog", wd, "launchctl unavailable" if wd == "failed" else "")
        P.set_step("ready", "done")
        ph = P.install_phase()
        if ph["phase"] == P.PHASE_INSTALL_COMPLETE:
            assert ph["watchdog_ok"] is True, (
                f"phase=INSTALL_COMPLETE but watchdog_ok=False for watchdog={wd!r} , "
                f"contradictory state (phase claims done while watchdog verdict says not ok)")


def test_smoke_passed_then_watchdog_ok_is_complete():
    _advance_to_smoke_passed()
    P.set_step("watchdog", "done")
    P.set_step("ready", "done")
    ph = P.install_phase()
    assert ph["phase"] == P.PHASE_INSTALL_COMPLETE
    assert ph["ready_proven"] is True and ph["watchdog_ok"] is True


def test_smoke_failed_is_a_genuine_failure():
    """Contrast: if smoke itself fails, readiness was NOT proven and it's a real failure."""
    P.begin_install()
    for k in ("prerequisites", "environment", "database", "server"):
        P.set_step(k, "done")
    P.set_step("smoke", "failed", "health check did not pass")
    ph = P.install_phase()
    assert ph["ready_proven"] is False
    assert ph["phase"] == P.PHASE_SERVER_FAILED


def test_server_failure_before_smoke_is_a_genuine_failure():
    P.begin_install()
    P.set_step("prerequisites", "done")
    P.set_step("server", "failed", "port held by another process")
    ph = P.install_phase()
    assert ph["ready_proven"] is False
    assert ph["phase"] == P.PHASE_SERVER_FAILED


def test_phase_progression_is_monotonic_through_a_clean_install():
    seen = []
    P.begin_install()
    seen.append(P.install_phase()["phase"])
    for k in ("prerequisites", "environment", "database", "server"):
        P.set_step(k, "done")
    seen.append(P.install_phase()["phase"])      # SERVER_READY
    P.set_step("smoke", "done")
    seen.append(P.install_phase()["phase"])      # SMOKE_TEST_PASSED
    P.set_step("watchdog", "done")
    P.set_step("ready", "done")
    seen.append(P.install_phase()["phase"])      # INSTALL_COMPLETE
    assert seen == [P.PHASE_BOOTSTRAP_STARTED, P.PHASE_SERVER_READY,
                    P.PHASE_SMOKE_TEST_PASSED, P.PHASE_INSTALL_COMPLETE]


# --- the wrapper's message contract (the auditor's explicit acceptance criteria) -------------------

def _wrapper_message(ready_proven: bool, watchdog_ok: bool, watchdog_detail: str = "") -> tuple[str, int]:
    """Pure reimplementation of the wrapper's decision, kept in lockstep with Install Mongoose.command,
    so the message/exit contract is unit-testable without spawning a Terminal."""
    if ready_proven:
        if watchdog_ok:
            return ("Mongoose is installed, running, and protected by the auto-restart watchdog.", 0)
        msg = ("Mongoose is installed and running. You can use it now. "
               "The auto-restart watchdog could not be installed, so Mongoose may not restart itself after a crash.")
        if watchdog_detail:
            msg += f" Reason: {watchdog_detail}"
        return (msg, 0)
    return ("Install failed before Mongoose became ready. See data/logs/install-command.log.", 1)


def test_wrapper_message_for_log_scenario_is_non_fatal_and_correct():
    """given smoke_test_passed and watchdog_install_failed:
       then exit is non-fatal, message must NOT contain 'before Mongoose became ready',
       and message MUST contain 'installed and running'."""
    msg, code = _wrapper_message(ready_proven=True, watchdog_ok=False, watchdog_detail="launchctl unavailable")
    assert code == 0
    assert "before Mongoose became ready" not in msg
    assert "installed and running" in msg
    assert "launchctl unavailable" in msg


def test_wrapper_message_for_genuine_failure_is_fatal():
    msg, code = _wrapper_message(ready_proven=False, watchdog_ok=False)
    assert code == 1
    assert "before Mongoose became ready" in msg


def test_wrapper_message_for_full_success():
    msg, code = _wrapper_message(ready_proven=True, watchdog_ok=True)
    assert code == 0
    assert "before Mongoose became ready" not in msg
    assert "protected by the auto-restart watchdog" in msg


# --- pin the ACTUAL shell artifacts, so the Python contract above can't drift from what ships --------

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "Install Mongoose.command"
BOOTSTRAP = REPO / "scripts" / "bootstrap_macos.sh"


def test_wrapper_consults_install_phase_not_just_exit_code():
    """The real wrapper must branch on the install-phase state machine (ready_proven), and must contain
    the non-fatal success message. This guards against a regression back to blind exit-code branching."""
    text = WRAPPER.read_text()
    assert "_install-phase" in text, "wrapper no longer queries the install-phase state machine"
    assert "ready_proven" in text
    assert "installed and running" in text, "wrapper lost the non-fatal watchdog success message"
    # the fatal message must still exist, but only on the not-ready branch
    assert "before Mongoose became ready" in text


def test_wrapper_does_not_fail_install_when_ready_proven():
    """Structural check: the fatal 'failed before ready' PRINT must be reachable only on the not-ready
    branch. The phrase also appears in an explanatory comment, so we locate the actual printf line and
    assert it comes after the readiness check."""
    lines = WRAPPER.read_text().splitlines()
    ready_line = next((i for i, l in enumerate(lines) if "READY_PROVEN" in l and "_install-phase" in l), -1)
    fatal_print_line = next((i for i, l in enumerate(lines)
                             if "printf" in l and "Install failed before Mongoose became ready" in l), -1)
    assert ready_line != -1, "wrapper doesn't compute READY_PROVEN from _install-phase"
    assert fatal_print_line != -1, "wrapper lost the genuine-failure message"
    assert fatal_print_line > ready_line, \
        "the fatal print must be in the not-ready branch, after the readiness check"
    # and the success branch must print the non-fatal message before that fatal line
    success_line = next((i for i, l in enumerate(lines)
                         if "printf" in l and "installed and running" in l), -1)
    assert success_line != -1 and ready_line < success_line < fatal_print_line, \
        "the non-fatal success message must sit in the ready_proven branch, before the fatal path"


def test_bootstrap_marks_smoke_then_watchdog_phases():
    """The bootstrap must record the smoke and watchdog phases the state machine reads, and the watchdog
    step must be non-fatal under set -e: its exit is captured with `|| WD_RC=$?` (not allowed to abort),
    and its real stderr is surfaced rather than discarded."""
    text = BOOTSTRAP.read_text()
    assert "_install-step smoke done" in text, "bootstrap doesn't mark smoke as passed"
    assert "_install-step watchdog" in text, "bootstrap doesn't record the watchdog phase"
    assert "server-watchdog --install" in text
    # the watchdog command must be non-fatal under set -e -o pipefail: capture the code, don't abort
    wd_line = next((l for l in text.splitlines()
                    if "server-watchdog --install" in l and "WD_RC=$?" in l), "")
    assert wd_line, "watchdog install command is not guarded with `|| WD_RC=$?` against set -e"
    # and the failure reason must be surfaced (Class D: don't hide the only useful error)
    section = text.split("Installing the server watchdog", 1)[1].split("_install-step ready done", 1)[0]
    assert "$WD_ERR" in section and "tail -20" in section, "watchdog stderr is not surfaced on failure"
