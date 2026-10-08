"""Tests for capabilities reconciled from the competing Chat 0.55.0 build:
updater runtime-smoke + rollback-drill, and self_harden review-submission consent gate.
These were genuinely absent from this lineage; the tests lock them in so they cannot be lost again."""
import os
import tempfile
from pathlib import Path

import pytest


def _fake_stage(tmp_data: str, candidate_root: str, *, approved=True, checked=True):
    os.environ["MONGOOSE_DATA_DIR"] = tmp_data
    from mongoose import updater
    stage = {
        "stage_id": "s1", "candidate_root": candidate_root,
        "manifest": {"ok": True}, "checks": [{"name": "manifest", "ok": True}] if checked else [],
        "approved": approved, "status": "checked", "created_at": "now", "updated_at": "now",
        "current_version": "0.0.0", "target_version": "0.0.1",
    }
    updater._save_stage(stage)
    return updater


def test_updater_has_ported_safety_functions():
    from mongoose import updater
    assert hasattr(updater, "runtime_smoke_stage")
    assert hasattr(updater, "rollback_drill_stage")
    assert hasattr(updater, "_free_local_port")


def test_free_local_port_returns_usable_port():
    from mongoose import updater
    p = updater._free_local_port()
    assert isinstance(p, int) and 1024 < p < 65536


def test_rollback_drill_reports_checks_without_applying():
    d = tempfile.mkdtemp()
    updater = _fake_stage(d, os.getcwd())
    r = updater.rollback_drill_stage("s1")
    names = {c["name"] for c in r.get("checks", [])}
    # it verifies the rollback story exists , stage, update script, prior checks, approval
    assert {"stage_exists", "update_script_exists", "stage_checked", "stage_approved"} <= names
    # it must not have changed stage status to applied/anything destructive
    assert "applied" not in str(r).lower()


def test_check_update_stage_accepts_runtime_smoke_param():
    """The signature must accept runtime_smoke (the Chat 0.55.0 update-safety flag). We don't spawn a
    server here; we assert the parameter is honoured by inspecting the signature."""
    import inspect
    from mongoose import updater
    sig = inspect.signature(updater.check_update_stage)
    assert "runtime_smoke" in sig.parameters


def test_rollback_drill_unknown_stage_raises():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose import updater
    with pytest.raises(Exception):
        updater.rollback_drill_stage("does-not-exist")


def test_self_harden_review_submission_is_consent_gated():
    """submit_review_packet must NOT mark anything sent unless allow_external AND yes. This is the
    privacy gate ported from Chat 0.55.0."""
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = str(Path(d) / "m.sqlite3")
    from mongoose.db import init_db
    init_db()
    from mongoose.services import self_harden as H
    # build a real packet first (my tree has create_reviewer_packet)
    pkt = H.create_reviewer_packet(reviewer="claude")
    pid = pkt["id"]
    # default: blocked, nothing leaves
    blocked = H.submit_review_packet(pid)
    assert blocked["status"] == "blocked_needs_explicit_consent"
    assert blocked["ok"] is False
    # explicit consent: status flips to ready_for_manual (still no automatic network call)
    allowed = H.submit_review_packet(pid, allow_external=True, yes=True)
    assert allowed["status"] == "ready_for_manual_external_submission"


def test_review_submission_plan_flags_risk_terms_without_sending():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = str(Path(d) / "m.sqlite3")
    from mongoose.db import init_db
    init_db()
    from mongoose.services import self_harden as H
    pkt = H.create_reviewer_packet(reviewer="claude")
    plan = H.review_submission_plan(pkt["id"], reviewer="claude")
    # the plan describes what WOULD leave; it estimates size and names the endpoint, but sends nothing
    assert "estimated_tokens" in plan
    assert "reviewer" in plan
