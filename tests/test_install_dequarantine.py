"""Guard the macOS Gatekeeper friction fix.

Files in a downloaded zip get the com.apple.quarantine xattr, so each .command shows the "unidentified
developer" prompt and the user has to clear every one in Privacy & Security. We can't codesign/notarize
without a paid Apple Developer ID, but the install command strips the quarantine flag from the sibling
launchers + app so they open after the first install, and a standalone Trust Mongoose.command does the
same on demand. These guards keep both in place."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_install_command_strips_quarantine():
    cmd = (ROOT / "Install Mongoose.command").read_text()
    assert "com.apple.quarantine" in cmd, \
        "Install command must remove the quarantine xattr so the other .command files open"
    assert "xattr -dr com.apple.quarantine" in cmd


def test_trust_command_exists_and_dequarantines():
    trust = ROOT / "Trust Mongoose.command"
    assert trust.exists(), "Trust Mongoose.command should ship so users can clear Gatekeeper on demand"
    txt = trust.read_text()
    assert "xattr -dr com.apple.quarantine" in txt
    # must be executable
    import os
    assert os.access(trust, os.X_OK), "Trust Mongoose.command must be executable"


def test_bootstrap_installs_watchdog_mandatory():
    """The server watchdog must be auto-installed by bootstrap (mandatory, not opt-in), so a household
    assistant heals itself without the user having to run a separate command."""
    boot = (ROOT / "scripts" / "bootstrap_macos.sh").read_text()
    assert "server-watchdog --install" in boot, \
        "bootstrap must install the server watchdog so auto-restart is on by default"
    assert "com.mongoose.server-watchdog" in boot
    assert "launchctl bootstrap" in boot      # and actually load it
