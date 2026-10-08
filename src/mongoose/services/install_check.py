from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Install-coherence checker.
#
# The benchmark post-mortem showed a failure class that a passing `doctor` missed
# entirely: the editable install and the running source tree had drifted apart (the
# console script pointed at an old folder, pip showed 0.5.4 while the tree claimed
# 0.14.x, and the CLI was missing `benchmark`). Runtime integration checks all looked
# green because the PROBLEM WAS THE INSTALL, not the runtime.
#
# This module answers: is the thing I'm running actually the thing I think I'm
# running? It compares the imported module path, the package version, the
# pyproject version, and the command surface, and fails loudly when they disagree.

# Commands every current (v0.14.x) build must expose. If the running CLI is missing
# any of these, the source tree is stale even if it imports and doctor passes.
EXPECTED_COMMANDS = (
    # Core daily-use surface , these are what a user actually runs; if any silently
    # disappears from a build, install-coherence should catch it. (QA-audit: the old list
    # was 8 stale entries incl. legacy 'crawler'/'ritual' and missed every modern command.)
    "doctor", "release-check", "alpha-check", "serve", "service", "setup",
    "daily", "brief", "what-changed", "prepare", "suggest",
    "system-map", "liveness", "receipts", "devices", "verify-data",
    "chat", "find", "search-files", "search-attachments", "weather", "mail", "whatsapp",
    "simulate-surpriser", "authorize", "scheduler", "version", "tune", "readiness", "alarm", "timer", "reminder", "alerts", "intro", "rehearse", "diagnostics", "body", "unstuck", "rtm", "build-plan-{{rtm.slug}}-intake", "build-plan-{{rtm.slug}}-governance", "build-plan-{{rtm.slug}}-management",
)
# The RTM build-plan commands are named after the install's RTM slug. They are listed (and written in
# cli.py's add_parser calls) as the "{{rtm.slug}}" template, so a source scan and the release manifest
# match on every install; a live parser exposes the rendered name, which _has_command also accepts.


def _imported_cli_path() -> Path | None:
    try:
        import mongoose.cli as cli
        return Path(cli.__file__).resolve()
    except Exception:
        return None


def _running_version() -> str:
    try:
        import mongoose
        return getattr(mongoose, "__version__", "unknown")
    except Exception:
        return "unknown"


def _project_root_from_module(cli_path: Path | None) -> Path | None:
    # src/mongoose/cli.py -> project root is three parents up.
    if cli_path is None:
        return None
    try:
        return cli_path.parent.parent.parent
    except Exception:
        return None


def _pyproject_version(root: Path | None) -> str | None:
    if root is None:
        return None
    pp = root / "pyproject.toml"
    if not pp.exists():
        return None
    for line in pp.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("version") and "=" in s:
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _cli_commands() -> set[str]:
    """The actual subcommands the running CLI exposes, read from the parser."""
    try:
        import argparse
        import mongoose.cli as cli
        # build_parser may or may not exist; fall back to scanning source.
        if hasattr(cli, "build_parser"):
            parser = cli.build_parser()
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    return set(action.choices.keys())
    except Exception:
        pass
    # Fallback: scan the source for add_parser("name").
    cli_path = _imported_cli_path()
    if cli_path and cli_path.exists():
        import re
        names = re.findall(r'add_parser\(\s*"([^"]+)"', cli_path.read_text(encoding="utf-8"))
        return set(names)
    return set()


def _has_command(expected: str, commands: set[str]) -> bool:
    if expected in commands:
        return True
    if "{{" not in expected:
        return False
    try:
        from ..settings import render_profile_copy
        return render_profile_copy(expected) in commands
    except Exception:
        return False


def check_install_coherence() -> dict[str, Any]:
    """Return a structured coherence report. ok=False means the install is stale or
    mixed and should be fixed by a fresh restore, not trusted."""
    cli_path = _imported_cli_path()
    root = _project_root_from_module(cli_path)
    running = _running_version()
    pyproj = _pyproject_version(root)
    commands = _cli_commands()
    missing = [c for c in EXPECTED_COMMANDS if not _has_command(c, commands)]

    problems = []
    # version metadata must agree
    if pyproj is not None and running != "unknown" and pyproj != running:
        problems.append(f"pyproject version {pyproj} != running version {running}")
    # the running CLI must expose the current command surface
    if missing:
        problems.append(f"CLI missing expected commands: {', '.join(missing)}")
    # the imported module should live under the same root pip would install from
    if cli_path is None:
        problems.append("could not import mongoose.cli")

    ok = not problems
    return {
        "ok": ok,
        "running_version": running,
        "pyproject_version": pyproj,
        "project_root": str(root) if root else None,
        "cli_module": str(cli_path) if cli_path else None,
        "python_executable": sys.executable,
        "command_count": len(commands),
        "missing_commands": missing,
        "problems": problems,
        "hint": None if ok else
                "install is stale or mixed: restore from a verified current zip into a "
                "fresh folder and reinstall from there (scripts/fresh_restore.sh), rather "
                "than updating from this install.",
    }
