"""Tests for two real first-run fixes:
1. the installer must verify the NEWLY-started server is the one answering (not an old one squatting
   the port) and force-stop a stale old Mongoose, so 'install launches old mongoose' can't happen;
2. `mongoose setup-ntfy` must actually START ntfy (modern launchctl + a direct fallback), not just
   write a launch agent the legacy commands silently fail to load."""
from __future__ import annotations

from pathlib import Path

import mongoose


def _repo_root() -> Path:
    return Path(mongoose.__file__).resolve().parent.parent.parent


# ---- installer: no stale-old-server-mistaken-for-success ----

def test_bootstrap_captures_and_verifies_version():
    src = (_repo_root() / "scripts" / "bootstrap_macos.sh").read_text()
    assert "EXPECTED_VERSION=" in src                       # knows what it's installing
    assert "RUNNING_VERSION=" in src                        # checks what actually answered
    # fails if the running server's version differs from the one being installed
    assert 'RUNNING_VERSION" != "$EXPECTED_VERSION' in src


def test_bootstrap_force_stops_old_mongoose():
    src = (_repo_root() / "scripts" / "bootstrap_macos.sh").read_text()
    # A single transparent routine frees the port for the native server. It kills by PID with a hard -9
    # backstop, on the configurable $PORT (not a hardcoded 8765).
    assert "ensure_port_free_for_mongoose" in src
    assert "kill -9" in src
    assert "tcp:\"$PORT\"" in src or "tcp:$PORT" in src
    # the kill is gated on the command being a Mongoose server (proven pattern) OR /health identity ,
    # never a blind kill of a port holder.
    assert "_cmd_is_mongoose_server" in src
    assert '"name":"mongoose"' in src
    # tears down a respawning launch agent BEFORE killing, else KeepAlive re-spawns the old server
    assert "bootout_mongoose_agents" in src and "launchctl bootout" in src
    # hard-fails with an actionable message rather than starting a doomed server when the port stays held
    assert "kill ${_hp:-<pid>}" in src or "xargs kill" in src


def test_installer_command_has_force_stop_helper():
    src = (_repo_root() / "Install Mongoose.command").read_text()
    # the unified port-clearing routine exists and is invoked in BOTH paths (first-run + already-set-up)
    assert "ensure_port_free_for_mongoose" in src
    assert src.count("ensure_port_free_for_mongoose") >= 3     # def + 2 calls
    # it kills only listeners whose command is a Mongoose server (proven pattern), or that answer /health
    assert "_cmd_is_mongoose_server" in src
    assert '"name":"mongoose"' in src
    # and removes any respawning launch agent
    assert "launchctl bootout" in src


def test_identity_match_does_not_fire_on_mongoose_in_a_path():
    """A kill must never be triggered by the word 'mongoose' merely appearing in a path (e.g. an ntfy
    config path .../mongoose-ntfy/server.yml). The ownership test uses the project's proven server
    pattern: "mongoose" followed by " serve" (leading space) OR uvicorn + "mongoose.app:app". The
    leading space is what makes a path safe (".../mongoose-ntfy/serve.yml" is "mongoose-ntfy" then
    "/serve.yml" , no "mongoose" directly followed by " serve"). Assert the loose no-space glob is gone
    and the proven pattern is what guards the kill."""
    for rel in ["Install Mongoose.command", "scripts/bootstrap_macos.sh"]:
        src = (_repo_root() / rel).read_text()
        # the loose no-space glob that could match a path is gone
        assert "*mongoose*serve*" not in src, f"{rel} still has the over-loose mongoose*serve* glob"
        # the proven pattern (space-prefixed serve + uvicorn app target) guards the kill
        assert '*mongoose*" serve"*' in src, f"{rel} missing the space-prefixed serve pattern"
        assert "mongoose.app:app" in src
        assert "_cmd_is_mongoose_server" in src


def test_installer_scripts_have_valid_bash_syntax():
    import subprocess
    for rel in ["scripts/bootstrap_macos.sh", "Install Mongoose.command"]:
        p = _repo_root() / rel
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"{rel} has a bash syntax error: {r.stderr}"


def test_installer_command_still_executable():
    import os
    p = _repo_root() / "Install Mongoose.command"
    assert os.access(p, os.X_OK), "Install Mongoose.command lost its executable bit"


# ---- setup-ntfy autolaunch ----

def test_setup_ntfy_uses_modern_launchctl_and_kickstart():
    src = (_repo_root() / "src" / "mongoose" / "setup_ntfy.py").read_text()
    # modern domain-target API (legacy load/unload silently no-ops on modern macOS)
    assert "bootstrap" in src and "kickstart" in src
    # kickstart -k starts it NOW, not just on next login
    assert '"kickstart", "-k"' in src or "'kickstart', '-k'" in src
    # legacy load kept as a fallback for older macOS
    assert '"load"' in src or "'load'" in src


def test_setup_ntfy_has_direct_launch_fallback():
    src = (_repo_root() / "src" / "mongoose" / "setup_ntfy.py").read_text()
    # if launchd didn't bring it up, start ntfy directly so it's running this session
    assert "start_new_session=True" in src
    assert "Popen" in src


def test_setup_ntfy_plist_runs_at_load_and_keepalive():
    src = (_repo_root() / "src" / "mongoose" / "setup_ntfy.py").read_text()
    assert "RunAtLoad" in src and "KeepAlive" in src


# ---- the respawn case + diagnostics + tightened identity match (v0.52.24 follow-up) ----

def test_force_stop_boots_out_launch_agent_first():
    """If a Mongoose launch agent (KeepAlive) is loaded, the installer must bootout/unload it before
    killing the PID , otherwise the agent respawns the server and the new install can never bind."""
    src = (_repo_root() / "Install Mongoose.command").read_text()
    assert "bootout" in src
    assert "com.mongoose.scheduler" in src
    # bootout must appear before the kill within the helper
    helper = src.split("force_stop_mongoose_on_port()", 1)[1].split("\n}", 1)[0]
    assert helper.index("bootout") < helper.index("kill ")


def test_bootstrap_prints_diagnostic_on_failure():
    """On a failed/old-version start, the installer must show what is on the port and the err-log tail
    so the real cause is visible (the one thing a sandbox can't see is this err log)."""
    src = (_repo_root() / "scripts" / "bootstrap_macos.sh").read_text()
    assert "mongoose.err.log" in src and "tail -n" in src
    assert "launchctl list" in src  # points at the respawn cause
