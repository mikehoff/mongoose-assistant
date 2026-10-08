"""Tests for the reliability spine + setup/UX polish pass (v0.52.27):
- install-old-server: launch-agent teardown + health-gated kill (not the brittle ps-pattern)
- UI liveness: /health/live async probe + /cockpit/restart
- mongoose status CLI + the Start/Stop/Status .command scripts
- /cockpit/setup-ntfy (native push from the browser, no terminal)
- Rosetta/arch-aware Docker steering in the bootstrap
- 'do something cool' partial path now ACTS (kicks a sync, returns actions)
- collapsible setup stages markup
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def _repo() -> Path:
    return Path(__file__).resolve().parent.parent


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


# ---- install-old-server: the real fix ----

def test_bootstrap_tears_down_launch_agent_before_kill():
    src = (_repo() / "scripts/bootstrap_macos.sh").read_text()
    assert "bootout_mongoose_agents" in src
    assert "launchctl bootout" in src
    # the helper that frees the port is health-gated
    assert "force_free_port_of_mongoose" in src
    assert "mongoose_is_on_port" in src


def test_install_command_kill_is_health_gated_not_ps_pattern():
    src = (_repo() / "Install Mongoose.command").read_text()
    # the brittle ps-pattern KILL gate that let old servers survive is gone
    assert "bin/mongoose\\ serve" not in src
    # kills are gated on /health confirming mongoose
    assert '"name":"mongoose"' in src
    assert "launchctl bootout" in src
    # 'mongoose.app:app' may appear, but only as a POSITIVE ownership proof (identifying OUR wedged
    # server that can't answer /health, mirroring `mongoose stop`), never as a kill-anything pattern.
    if "mongoose.app:app" in src:
        assert "_cmd_is_mongoose_server" in src


def test_bootstrap_retries_teardown_on_version_mismatch():
    """On a version mismatch the installer must actually re-run the teardown + relaunch, not just
    print manual instructions."""
    src = (_repo() / "scripts/bootstrap_macos.sh").read_text()
    # after detecting a version mismatch it calls the teardown again and relaunches
    assert src.count("force_free_port_of_mongoose") >= 2
    assert "Now serving" in src or "Still serving" in src


def test_install_scripts_valid_bash():
    for rel in ["scripts/bootstrap_macos.sh", "Install Mongoose.command",
                "Start Mongoose.command", "Stop Mongoose.command", "Mongoose Status.command"]:
        p = _repo() / rel
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"{rel} bash syntax error: {r.stderr}"


# ---- control scripts exist + executable ----

def test_control_scripts_exist_and_executable():
    for rel in ["Start Mongoose.command", "Stop Mongoose.command", "Mongoose Status.command"]:
        p = _repo() / rel
        assert p.exists(), f"missing {rel}"
        assert os.access(p, os.X_OK), f"{rel} not executable"


def test_stop_script_is_identity_safe():
    """The Stop script must only kill a confirmed-Mongoose process (health-gated), never a bare port
    holder."""
    src = (_repo() / "Stop Mongoose.command").read_text()
    assert '"name":"mongoose"' in src
    # prefers the CLI (pidfile-aware) path
    assert "mongoose stop" in src


# ---- UI liveness ----

def test_health_live_endpoint():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    r = c.get("/health/live")
    assert r.status_code == 200
    assert r.json().get("loop") == "responsive"


def test_restart_endpoint_requires_nonce():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    r = c.post("/cockpit/restart")
    assert r.status_code == 403


def test_cockpit_has_watchdog_loop():
    """The cockpit must ship a liveness watchdog that pings /health/live and can recover the UI."""
    from mongoose.cockpit import COCKPIT_HTML
    assert "/health/live" in COCKPIT_HTML
    assert "watchdogTick" in COCKPIT_HTML
    assert "Restart the assistant" in COCKPIT_HTML


# ---- status CLI ----

def test_status_cli_reports_not_running():
    _fresh()
    import sys
    from mongoose.cli import main
    old = sys.argv
    try:
        sys.argv = ["mongoose", "status"]
        rc = main()
    finally:
        sys.argv = old
    assert rc == 1  # nothing running in the test


# ---- native ntfy from browser ----

def test_setup_ntfy_endpoint_requires_nonce():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    r = c.post("/cockpit/setup-ntfy")
    assert r.status_code == 403


def test_cockpit_setup_ntfy_button_not_terminal_hint():
    """The push card should offer a one-click native-push button, not tell the user to run a terminal
    command."""
    from mongoose.cockpit import COCKPIT_HTML
    assert "setupNtfy" in COCKPIT_HTML
    # the old terminal instruction is gone
    assert "run in a terminal: <code>mongoose setup-ntfy</code>" not in COCKPIT_HTML


# ---- Rosetta/arch-aware Docker ----

def test_bootstrap_is_rosetta_aware():
    src = (_repo() / "scripts/bootstrap_macos.sh").read_text()
    assert "uname -m" in src
    assert "arm64" in src
    assert "Rosetta" in src or "emulation" in src


# ---- do something cool ACTS on partial config ----

def test_do_something_cool_partial_returns_actions(monkeypatch):
    _fresh()
    os.environ["IMAP_HOST"] = "imap.gmail.com"
    os.environ["IMAP_USERNAME"] = "m@example.com"
    os.environ["IMAP_PASSWORD"] = "pw"
    os.environ["HA_URL"] = "http://192.168.1.20:8123"
    os.environ["HA_TOKEN"] = "tok"
    import asyncio
    from mongoose.services.tutorial import do_something_cool
    out = asyncio.run(do_something_cool(allow_private_data=True))
    # it should recognise the partial state and offer concrete actions (not just explain)
    if out.get("kind") == "partial":
        assert "actions" in out and out["actions"]
        labels = [a.get("label", "") for a in out["actions"]]
        assert any("Sync mail" in l for l in labels) or any("calendar" in l.lower() for l in labels)
    for k in ["IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "HA_URL", "HA_TOKEN"]:
        os.environ.pop(k, None)


# ---- collapsible setup stages ----

def test_cockpit_has_collapsible_stages():
    from mongoose.cockpit import COCKPIT_HTML
    assert "toggleStage" in COCKPIT_HTML
    assert "stage-body" in COCKPIT_HTML
    assert "stage-chev" in COCKPIT_HTML


# ---- null-under-load hardening ----

def test_j_helper_retries_and_rejects_null():
    """The cockpit's fetch helper must retry and reject null/empty bodies so tabs don't blank under
    load."""
    from mongoose.cockpit import COCKPIT_HTML
    assert "null response" in COCKPIT_HTML or "empty response" in COCKPIT_HTML
    assert "retryCard" in COCKPIT_HTML
