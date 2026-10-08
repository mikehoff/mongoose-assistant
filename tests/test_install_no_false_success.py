"""Reconciled parallel audit: the install path must NOT claim 'ready' when the system isn't, and must
surface partial failures rather than hiding them behind a plain Ready banner."""
from __future__ import annotations

import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


def test_server_failure_is_not_ready():
    """If the server step failed (bootstrap exits before marking ready), the page must NOT show
    ready, it must show 'needs attention'."""
    _fresh()
    from mongoose.install_progress import set_step, install_progress
    for s in ["prerequisites", "environment", "database", "models"]:
        set_step(s, "done")
    set_step("server", "failed", detail="Server did not answer on port 8765")
    p = install_progress()
    assert p["ready"] is False
    assert p["failed"] is True


def test_partial_failure_is_ready_but_flagged():
    """If an optional step (services) failed but the server is up, the page shows ready WITH a
    warning listing the failed step, not a falsely-perfect Ready."""
    _fresh()
    from mongoose.install_progress import set_step, install_progress
    for s in ["prerequisites", "environment", "database", "models"]:
        set_step(s, "done")
    set_step("services", "failed", detail="ntfy/SearXNG did not start")
    set_step("server", "done")
    set_step("ready", "done")
    p = install_progress()
    assert p["ready"] is True
    assert p["failed"] is True
    failed = [s for s in p["steps"] if s["status"] == "failed"]
    assert any(s["key"] == "services" for s in failed)
    assert failed[0]["detail"]  # detail is carried through for the warning block


def test_clean_install_is_ready_no_warning():
    _fresh()
    from mongoose.install_progress import set_step, install_progress
    for s in ["prerequisites", "environment", "database", "models", "services", "server"]:
        set_step(s, "done")
    set_step("ready", "done")
    p = install_progress()
    assert p["ready"] is True
    assert p["failed"] is False
    assert not [s for s in p["steps"] if s["status"] == "failed"]


def test_install_page_shows_warning_block_logic():
    """The /install page must contain the warning-block path keyed off failed steps."""
    _fresh()
    from mongoose.cockpit import install_html
    h = install_html()
    assert "async function showDone(p)" in h          # takes progress
    assert 'filter(s=>s.status==="failed")' in h       # surfaces failed steps
    # the watchdog-only failure gets a reassuring, non-alarming message (the post-mortem fix)
    assert "installed and running" in h
    assert 'watchdogOnly' in h                          # distinguishes the non-fatal watchdog case


def test_bootstrap_fails_honestly_on_server_and_smoke():
    """The shipped bootstrap must record failures and exit non-zero, not pretend success."""
    import mongoose
    from pathlib import Path
    boot = Path(mongoose.__file__).resolve().parent.parent.parent / "scripts" / "bootstrap_macos.sh"
    src = boot.read_text()
    # honest server-start failure
    assert "_install-step server failed" in src
    assert 'started -ne 1' in src or '"$started" -ne 1' in src
    # smoke test failure is a real failure now (no bare '|| true' swallow): it marks the smoke step
    # failed AND the ready step failed, so the state machine reports a genuine pre-readiness failure.
    assert "_install-step smoke failed" in src
    assert "_install-step ready failed" in src
    # docker partial failure recorded
    assert "_install-step services failed" in src
    # port-ownership guard
    assert "non-Mongoose process" in src


def test_installer_command_refuses_occupied_port():
    import mongoose
    from pathlib import Path
    cmd = Path(mongoose.__file__).resolve().parent.parent.parent / "Install Mongoose.command"
    src = cmd.read_text()
    assert "non-Mongoose process" in src
    assert "lsof" in src
