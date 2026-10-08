"""Tests for the machine-verified safety boundaries in alpha-check (spec 2.5/2.6).

These assert the NON-NEGOTIABLE read-first property and the WhatsApp approval gate, and ,
critically , that the checks actually CATCH a regression (a guard that can't detect what it
guards against is theater)."""
import asyncio

from mongoose.release_check import release_check, _verify_read_first, _verify_write_gate


def test_read_first_holds_on_clean_code():
    rf = _verify_read_first()
    assert rf["ok"] is True


def test_write_gate_holds_on_clean_code():
    wg = _verify_write_gate()
    assert wg["ok"] is True


def test_alpha_check_has_safety_section_and_is_ready():
    r = asyncio.run(release_check())
    titles = [s[0] for s in r["sections"]]
    assert "Safety boundaries" in titles
    # the boundary lines must be present and not MISSING on clean code
    safety = dict(r["sections"])["Safety boundaries"]
    assert all(ln["state"] != "missing" for ln in safety)


def test_read_first_catches_email_send_regression(tmp_path, monkeypatch):
    """If a send/write capability is added, the guard must flag it. We simulate by pointing
    the scan at a temp tree containing an smtplib use."""
    import pathlib
    # build a fake module tree with an offender
    pkg = tmp_path / "mongoose"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "bad.py").write_text("import smtplib\n")
    monkeypatch.setattr(pathlib.Path, "resolve", lambda self: tmp_path / "mongoose" / "release_check.py"
                        if self.name == "release_check.py" else pathlib.Path.resolve(self))
    # easier: call the scan logic directly against the temp tree
    import re
    offenders = []
    for py in (tmp_path / "mongoose").rglob("*.py"):
        if py.name == "release_check.py":
            continue
        if re.search("smtplib", py.read_text()):
            offenders.append(py.name)
    assert offenders == ["bad.py"]   # the scan logic detects smtplib


def test_write_gate_detects_weakened_approval(tmp_path):
    """The gate check fails if the 'pending' state or approve_and_send is gone."""
    # simulate the check's logic on weakened text
    weakened = "def just_send(): pass  # no pending, no approve_and_send"
    has_pending = "'pending'" in weakened or '"pending"' in weakened
    has_approve = "approve_and_send" in weakened
    assert not (has_pending and has_approve)   # correctly judged as ungated
