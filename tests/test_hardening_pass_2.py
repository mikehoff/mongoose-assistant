"""Second self-directed hardening pass. Found by asking 'what else does the poke rate-limit
class of bug apply to, and what does my own concurrency guard from pass 1 break': (1) a guest
could flood the owner's mailbox with unlimited 'tell alex' messages, unthrottled, same class
as the poke spam from pass 1. (2) my own self_patch concurrency guard (pass 1) introduced a
real regression: a server crash/restart during an active job left it permanently stuck in an
'active' status with no supervisor to ever mark it failed, so every future self-patch request
was blocked forever. (3) self_patch never cleaned up its workspace (a full src+tests copy) on
any outcome: success, failure, or peer rejection all leaked a permanent copy on disk. (4) the
staleness reaper needed to clean up the orphaned WORKSPACE too, not just the job status, since
the crashed process's own finally block never got to run."""
from __future__ import annotations

import asyncio
import importlib
import time
import warnings

warnings.filterwarnings("ignore")


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    import mongoose.services.self_patch as SP
    importlib.reload(SP)
    return SP


def test_guest_mailbox_to_owner_is_rate_limited(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "g.sqlite3"),
                 "MONGOOSE_ALLOW_REMOTE": "true", "MONGOOSE_HOST": "0.0.0.0",
                 "MONGOOSE_API_TOKEN": "t", "MONGOOSE_LOUNGE_STARTERS": "off",
                 "MONGOOSE_PUBLIC_URL": "https://example.duckdns.org"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label="g")
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": "Kasia", "public_name": "K", "fun_fact": "x"})
    H = {"X-Mongoose-Device-Token": cl["device_token"]}
    replies = [c.post("/device/chat", headers=H,
                      json={"message": f"tell alex message {i}"}).json().get("reply", "")
               for i in range(5)]
    throttled = sum(1 for r in replies if "give it" in r or "flooded" in r)
    assert throttled > 0, "unlimited mailbox spam to the owner"


def test_stale_self_patch_job_does_not_permanently_lock_out_new_requests(monkeypatch, tmp_path):
    SP = _fresh(monkeypatch, tmp_path)
    r1 = SP.request_self_patch("harden QR logic", requested_by="owner")
    jobs = SP._load_jobs()
    jobs[r1["job_id"]]["updated_at"] = time.time() - 20000  # orphaned 2h ago
    SP._save_jobs(jobs)
    r2 = SP.request_self_patch("a genuinely new fix", requested_by="owner")
    assert r2["ok"], "a 2-hour-stale job permanently blocked every future self-patch"
    assert SP.get_job(r1["job_id"])["status"] == "abandoned"


def test_self_patch_workspace_is_cleaned_up_on_failure(monkeypatch, tmp_path):
    SP = _fresh(monkeypatch, tmp_path)
    SP._invoke_codegen = lambda tree, goal: {"ok": False, "error": "boom"}
    r = SP.request_self_patch("this will fail", requested_by="owner")
    asyncio.run(SP.run_job(r["job_id"]))
    assert not (SP.workspace_root() / r["job_id"]).exists()


def test_self_patch_workspace_is_cleaned_up_on_success(monkeypatch, tmp_path):
    SP = _fresh(monkeypatch, tmp_path)
    SP._invoke_codegen = lambda tree, goal: {"ok": True, "error": None}
    SP._run_gates = lambda tree: {"ok": True}
    SP._peer_review = lambda tree, goal, i: {"approve": True, "reviewer": i, "reason": "APPROVE"}
    import mongoose.updater as U
    monkeypatch.setattr(U, "stage_update_candidate", lambda z: {"ok": True, "stage_id": "s1"})
    import mongoose.services.mailbox as mb
    monkeypatch.setattr(mb, "send_message", lambda **k: {"ok": True})
    r = SP.request_self_patch("harden QR logic", requested_by="owner")
    out = asyncio.run(SP.run_job(r["job_id"]))
    assert out["ok"]
    assert not (SP.workspace_root() / r["job_id"]).exists(), "staged job left its workspace on disk"


def test_reaper_removes_the_orphaned_workspace_directory_too(monkeypatch, tmp_path):
    SP = _fresh(monkeypatch, tmp_path)
    r = SP.request_self_patch("orphaned by a crash", requested_by="owner")
    orphan_ws = SP.workspace_root() / r["job_id"]
    orphan_ws.mkdir(parents=True, exist_ok=True)
    (orphan_ws / "marker.txt").write_text("leftover from a dead process")
    jobs = SP._load_jobs()
    jobs[r["job_id"]]["updated_at"] = time.time() - 20000
    SP._save_jobs(jobs)
    SP._reap_stale_jobs()
    assert not orphan_ws.exists(), "reaper marked the job abandoned but left the disk copy"
