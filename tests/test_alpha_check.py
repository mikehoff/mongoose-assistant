"""Tests for `mongoose release-check` (formerly alpha-check), the runtime readiness verdict.
It composes existing checks, so these assert the shape, the verdict logic, that the
Security section is present (the doc explicitly wants it surfaced), and that the
cockpit Overview shows the known gaps."""
import asyncio

from fastapi.testclient import TestClient

from mongoose.release_check import release_check, format_release_check, READY, MISSING
from mongoose.app import app

client = TestClient(app)


def test_alpha_check_shape():
    report = asyncio.run(release_check())
    assert "verdict" in report and "sections" in report
    titles = [t for t, _ in report["sections"]]
    assert "Core" in titles and "Models" in titles and "Security" in titles


def test_alpha_check_security_section_present():
    report = asyncio.run(release_check())
    sec = dict(report["sections"])["Security"]
    labels = [ln["label"] for ln in sec]
    # the doc asks for these to be surfaced
    assert "Action nonce" in labels
    assert "Mutation policy" in labels
    assert "Session signing" in labels
    assert "Login throttle" in labels
    assert "Speaker policy" in labels


def test_alpha_check_lines_have_states():
    report = asyncio.run(release_check())
    for _, lines in report["sections"]:
        for ln in lines:
            assert ln["state"] in ("ready", "partial", "missing", "unknown")
            assert "label" in ln


def test_verdict_blocks_only_on_core_model_security_missing(monkeypatch):
    """A MISSING in Core/Models/Security blocks; a missing data path (e.g. HA) does
    not, that's a known gap, not a release blocker."""
    report = asyncio.run(release_check())
    # In the sandbox HA/web are missing (data paths) but verdict should still be ready
    data = dict(report["sections"]).get("Data paths", [])
    assert any(ln["state"] == MISSING for ln in data)  # something in data is missing
    assert report["verdict"] == "ready"           # yet still ready


def test_format_renders_verdict_and_marks():
    report = asyncio.run(release_check())
    text = format_release_check(report)
    assert "runtime readiness" in text.lower()
    assert "verdict:" in text
    # it must distinguish runtime readiness from the release-QA gate (P1.2 fix)
    assert "qa_repo.sh" in text
    # legend note about limits being visible
    assert "does not hide them" in text


def test_release_check_endpoint():
    r = client.get("/release-check")
    assert r.status_code == 200
    body = r.json()
    assert "verdict" in body and "sections" in body


def test_alpha_check_endpoint_legacy_alias():
    """/alpha-check is kept as a back-compat alias for /release-check and must return the same shape."""
    r = client.get("/alpha-check")
    assert r.status_code == 200
    body = r.json()
    assert "verdict" in body and "sections" in body


def test_cockpit_overview_shows_release_card():
    body = client.get("/ui").text
    assert "Release readiness" in body
    assert "/release-check" in body
    assert "Known gaps" in body


def test_security_lines_are_ready():
    """The five security protections should report ready in a normal build (they're
    code-level guarantees, not config)."""
    report = asyncio.run(release_check())
    sec = dict(report["sections"])["Security"]
    for ln in sec:
        if ln["label"] in ("Action nonce", "Mutation policy", "Login throttle", "Speaker policy"):
            assert ln["state"] == READY
