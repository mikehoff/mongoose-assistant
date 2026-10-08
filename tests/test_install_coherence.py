"""Tests for the install-coherence checker and release manifest (benchmark
post-mortem product fixes): detecting stale/mixed source trees and verifying a
release by content rather than filename."""
from pathlib import Path

from mongoose.services.install_check import EXPECTED_COMMANDS


def test_coherence_ok_on_current_tree():
    from mongoose.services.install_check import check_install_coherence
    r = check_install_coherence()
    assert r["ok"], r["problems"]
    assert r["running_version"] == r["pyproject_version"]
    assert not r["missing_commands"]


def test_coherence_detects_version_mismatch(monkeypatch):
    from mongoose.services import install_check as ic
    monkeypatch.setattr(ic, "_pyproject_version", lambda root: "0.5.4")
    monkeypatch.setattr(ic, "_running_version", lambda: "0.14.2")
    r = ic.check_install_coherence()
    assert r["ok"] is False
    assert any("0.5.4" in p and "0.14.2" in p for p in r["problems"])
    assert r["hint"]


def test_coherence_detects_missing_commands(monkeypatch):
    from mongoose.services import install_check as ic
    monkeypatch.setattr(ic, "_cli_commands", lambda: {"doctor", "chat"})
    r = ic.check_install_coherence()
    assert r["ok"] is False
    # a critical command that is in EXPECTED_COMMANDS but absent from the (mocked) CLI must
    # be flagged. "alpha-check" is in the expected set; "doctor"/"chat" are the only present.
    assert any("alpha-check" in p for p in r["problems"])


def test_expected_commands_present_in_real_cli():
    """The current CLI must actually expose every command the checker expects, else
    the checker would false-positive on a healthy tree."""
    from mongoose.services.install_check import _cli_commands
    cmds = _cli_commands()
    missing = [c for c in EXPECTED_COMMANDS if c not in cmds]
    assert not missing, f"real CLI missing {missing}"


# --- release manifest --------------------------------------------------------
def test_build_manifest_matches_tree():
    from mongoose.services.release_manifest import build_manifest
    root = Path(__file__).resolve().parent.parent
    m = build_manifest(root)
    assert m["package"] == "mongoose"
    assert m["version"] != "unknown"
    assert "alpha-check" in m["expected_commands"]
    assert "doctor" in m["expected_commands"]


def test_verify_against_manifest_ok(tmp_path):
    from mongoose.services.release_manifest import write_manifest, verify_against_manifest
    # build a minimal coherent tree
    root = tmp_path / "tree"
    (root / "src" / "mongoose").mkdir(parents=True)
    (root / "pyproject.toml").write_text('version = "9.9.9"\n')
    cli = "import argparse\n" + "".join(
        f'# add_parser("{c}")\nx="{c}"\n' for c in EXPECTED_COMMANDS)
    # write real add_parser calls so the regex finds them
    cli = "import argparse\n" + "".join(
        f'sub.add_parser("{c}")\n' for c in EXPECTED_COMMANDS)
    (root / "src" / "mongoose" / "cli.py").write_text(cli)
    write_manifest(root)
    r = verify_against_manifest(root)
    assert r["ok"], r.get("problems")


def test_verify_against_manifest_detects_stale(tmp_path):
    from mongoose.services.release_manifest import verify_against_manifest
    import json
    root = tmp_path / "tree"
    (root / "src" / "mongoose").mkdir(parents=True)
    (root / "pyproject.toml").write_text('version = "0.5.4"\n')
    (root / "src" / "mongoose" / "cli.py").write_text('sub.add_parser("doctor")\n')
    # manifest claims a newer version + commands the source doesn't have
    (root / "release.json").write_text(json.dumps({
        "package": "mongoose", "version": "0.14.2",
        "expected_commands": ["benchmark", "brief"], "schema": 1}))
    r = verify_against_manifest(root)
    assert r["ok"] is False
    assert any("version" in p for p in r["problems"])
    assert any("benchmark" in p for p in r["problems"])


def test_missing_manifest_tolerated(tmp_path):
    from mongoose.services.release_manifest import verify_against_manifest
    root = tmp_path / "tree"
    root.mkdir()
    r = verify_against_manifest(root)
    assert r["ok"] is True and r["manifest"] is None
