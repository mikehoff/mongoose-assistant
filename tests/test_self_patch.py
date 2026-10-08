"""Chat-driven self-hardening: goal in, background build + gates + peer review, candidate
staged in the normal approvals pipeline. The live tree is NEVER touched by this loop; the
owner's approval of the staged candidate is the only application path."""
from __future__ import annotations

import asyncio
import warnings

warnings.filterwarnings("ignore")


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "sp.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "data"))
    from mongoose.db import init_db
    init_db()


def test_owner_only_and_goal_bounds(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services import self_patch as SP
    assert not SP.request_self_patch("harden x", requested_by="guest:p:kasia")["ok"]
    assert not SP.request_self_patch("", requested_by="owner")["ok"]
    assert not SP.request_self_patch("x" * 501, requested_by="owner")["ok"]
    assert SP.request_self_patch("harden the QR code logic", requested_by="owner")["ok"]


def test_happy_path_stages_candidate_and_notifies_never_touching_live_tree(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services import self_patch as SP
    res = SP.request_self_patch("harden the QR code logic", requested_by="owner")
    job_id = res["job_id"]

    def fake_codegen(tree, goal):
        (tree / "src" / "marker.py").write_text("# patched\n")
        return {"ok": True, "error": None}
    monkeypatch.setattr(SP, "_invoke_codegen", fake_codegen)
    monkeypatch.setattr(SP, "_run_gates", lambda tree: {"ok": True})
    monkeypatch.setattr(SP, "_peer_review",
                        lambda tree, goal, i: {"approve": True, "reviewer": i, "reason": "APPROVE fine"})
    staged = {}
    def fake_stage(zip_path):
        staged["path"] = str(zip_path)
        return {"ok": True, "stage_id": "stage-xyz"}
    import mongoose.updater as U
    monkeypatch.setattr(U, "stage_update_candidate", fake_stage)
    notified = {}
    from mongoose.services import mailbox as mb
    monkeypatch.setattr(mb, "send_message",
                        lambda sender, recipient, body: notified.update(body=body) or {"ok": True})

    out = asyncio.run(SP.run_job(job_id))
    assert out["ok"] and out["stage_id"] == "stage-xyz"
    job = SP.get_job(job_id)
    assert job["status"] == "awaiting_owner_approval"
    assert [s["name"] for s in job["steps"]][:2] == ["workspace", "codegen"]
    assert len(job["reviews"]) == SP.REQUIRED_APPROVALS
    assert "Approvals" in notified["body"] and "stage-xyz" in notified["body"]
    # the workspace is under the data dir, not the live tree
    assert str(tmp_path / "data") in staged["path"]


def test_peer_rejection_blocks_staging(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services import self_patch as SP
    job_id = SP.request_self_patch("harden the QR code logic", requested_by="owner")["job_id"]
    monkeypatch.setattr(SP, "_invoke_codegen", lambda tree, goal: {"ok": True, "error": None})
    monkeypatch.setattr(SP, "_run_gates", lambda tree: {"ok": True})
    verdicts = iter([{"approve": True, "reviewer": 1, "reason": "APPROVE"},
                     {"approve": False, "reviewer": 2, "reason": "REJECT deletes a test"}])
    monkeypatch.setattr(SP, "_peer_review", lambda tree, goal, i: next(verdicts))
    import mongoose.updater as U
    def never(*a, **k):
        raise AssertionError("staging must not happen on peer rejection")
    monkeypatch.setattr(U, "stage_update_candidate", never)
    out = asyncio.run(SP.run_job(job_id))
    assert not out["ok"]
    assert SP.get_job(job_id)["status"] == "rejected_by_peers"


def test_gate_failure_blocks_review_and_staging(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services import self_patch as SP
    job_id = SP.request_self_patch("harden the QR code logic", requested_by="owner")["job_id"]
    monkeypatch.setattr(SP, "_invoke_codegen", lambda tree, goal: {"ok": True, "error": None})
    monkeypatch.setattr(SP, "_run_gates",
                        lambda tree: {"ok": False, "gate": "pyflakes", "detail": "unused import"})
    def never(*a, **k):
        raise AssertionError("no review after failed gates")
    monkeypatch.setattr(SP, "_peer_review", never)
    out = asyncio.run(SP.run_job(job_id))
    assert not out["ok"]
    assert SP.get_job(job_id)["status"] == "failed"


def test_tool_registered_sensitive_with_status_verb(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.tools import TOOLS, SENSITIVE_TOOLS
    assert "self_patch" in SENSITIVE_TOOLS
    assert TOOLS["self_patch"]["description"].startswith("Sensitive")
    out = asyncio.run(TOOLS["self_patch"]["executor"]({"action": "status"}))
    assert out["ok"] and isinstance(out["jobs"], list)


def test_guest_utterance_never_reaches_self_patch(monkeypatch, tmp_path):
    """A guest saying 'mongoose, try to harden your QR code logic' must ride the NORMAL
    guest chat path: no tool call, no job, no owner-only refusal copy leaking into their
    reply. The structural guarantee is llm.py's guest_mode gate (guests get no tools ever);
    this pins the observable behaviour end to end through the real chat route."""
    import json as _j
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "g.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "t")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://example.duckdns.org")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTERS", "off")
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label="jordan")
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": "Kasia", "public_name": "K",
                                  "fun_fact": "p"})
    r = c.post("/device/chat", headers={"X-Mongoose-Device-Token": cl["device_token"]},
               json={"message": "mongoose, try to harden your QR code logic"})
    body = _j.dumps(r.json())
    assert r.status_code == 200
    assert "owner-only" not in body, "refusal copy leaked into a guest reply"
    from mongoose.services import self_patch as SP
    assert SP.list_jobs() == [], "a guest utterance created a self-patch job"
