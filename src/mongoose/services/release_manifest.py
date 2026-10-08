from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Release manifest (release.json).
#
# The post-mortem showed that filenames lie (a browser-renamed mongoose-2.zip
# was indistinguishable from a stale zip) and that an update can apply an incoherent
# tree. A manifest shipped INSIDE the zip lets the updater verify what it's applying
# by content, not by filename: the declared version and the command surface that
# version must expose.

from .install_check import EXPECTED_COMMANDS


def build_manifest(root: Path) -> dict[str, Any]:
    """Build a release manifest for the source tree at `root`."""
    from . import install_check
    version = install_check._pyproject_version(root) or "unknown"
    return {
        "package": "mongoose",
        "version": version,
        "expected_commands": list(EXPECTED_COMMANDS),
        "schema": 1,
    }


def write_manifest(root: Path) -> Path:
    manifest = build_manifest(root)
    path = root / "release.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def verify_against_manifest(root: Path) -> dict[str, Any]:
    """Check an unpacked tree against its own release.json: version agreement and
    that the source actually defines the commands the manifest promises. Returns
    {ok, problems}. A missing manifest is tolerated (older zips) but noted."""
    mpath = root / "release.json"
    if not mpath.exists():
        return {"ok": True, "manifest": None, "note": "no release.json (older release)"}
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "problems": [f"release.json unreadable: {exc}"]}

    from . import install_check
    problems = []
    declared = manifest.get("version")
    actual = install_check._pyproject_version(root)
    if declared and actual and declared != actual:
        problems.append(f"release.json version {declared} != pyproject {actual}")

    cli = root / "src" / "mongoose" / "cli.py"
    src = cli.read_text(encoding="utf-8") if cli.exists() else ""
    import re
    present = set(re.findall(r'add_parser\(\s*"([^"]+)"', src))
    missing = [c for c in manifest.get("expected_commands", []) if c not in present]
    if missing:
        problems.append(f"source missing manifest commands: {', '.join(missing)}")

    return {"ok": not problems, "manifest": manifest, "problems": problems}
