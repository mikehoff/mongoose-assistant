"""Third self-directed hardening pass, found by scrutinising the fixes from pass 1 and 2
rather than anything new. (1) CRITICAL: the poke resolver could pass target_device_ids=None
to send_to_phone if a resolved guest's device_id was ever falsy (empty string is allowed by
guest_profiles' PRIMARY KEY constraint, which only forbids NULL); list_subscriptions treats
an explicit None as 'no restriction at all' and returns EVERY subscription including the
owner's real phone, resurrecting the exact broadcast bug the A2 fix closed. (2) the sync
self_patch confirm dispatcher used asyncio.run() and would have blocked its calling thread
for up to 90 minutes; nothing live calls it today, but it's tested public API expected to be
symmetric with the async dispatcher. (3) the staleness ceiling's margin over the real
worst-case duration was only 30 minutes, tighter than intended for a value with zero cost to
widening. (4) list_jobs/get_job never reaped, so 'how is it going' could report a stale
'building' status for hours after a job actually died, until something else happened to
start a new request."""
from __future__ import annotations

import time
import warnings

warnings.filterwarnings("ignore")


def test_poke_never_broadcasts_even_with_a_corrupted_empty_device_id(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "g.sqlite3"),
                 "MONGOOSE_ALLOW_REMOTE": "true", "MONGOOSE_HOST": "0.0.0.0",
                 "MONGOOSE_API_TOKEN": "t", "MONGOOSE_LOUNGE_STARTERS": "off",
                 "MONGOOSE_PUBLIC_URL": "https://example.duckdns.org"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from mongoose.db import connect
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite

    def guest(name, label):
        inv = create_guest_invite(owner_label=label)
        tok = inv["invite_url"].rstrip("/").split("/")[-1]
        cl = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": "x"})
        return cl, {"X-Mongoose-Device-Token": cl["device_token"]}

    cl1, H1 = guest("Kasia", "k")
    cl2, _ = guest("Sammy", "g")
    with connect() as con:
        con.execute("UPDATE guest_profiles SET device_id=? WHERE device_id=?",
                   ("", cl2["device_id"]))
        con.commit()
    import mongoose.services.web_push as WP
    WP.save_subscription({"endpoint": "https://p.test/owner", "keys": {"p256dh": "AA", "auth": "BB"}},
                         device_id="owner-real-phone", device_profile="primary_phone",
                         device_label="OWNER REAL PHONE")
    hit = {"n": 0}
    import mongoose.services.notifications as N
    async def spy(title, body="", **kw):
        from mongoose.services.web_push import list_subscriptions as ls
        subs = ls(device_ids=kw.get("target_device_ids"))
        if any(s["device_label"] == "OWNER REAL PHONE" for s in subs):
            hit["n"] += 1
        return {"ok": True, "sent": len(subs)}
    monkeypatch.setattr(N, "send_to_phone", spy)
    r = c.post("/device/irc/send", headers=H1, json={"text": "poke sam"}).json()
    assert hit["n"] == 0, "owner's real phone was reachable via a corrupted device_id"
    assert "can't reach" in r["reply"]


def test_sync_self_patch_confirm_does_not_block_the_calling_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    import mongoose.services.self_patch as SP
    monkeypatch.setattr(SP, "_invoke_codegen", lambda tree, goal: {"ok": True, "error": None})
    monkeypatch.setattr(SP, "_run_gates", lambda tree: {"ok": True})
    monkeypatch.setattr(SP, "_peer_review",
                        lambda tree, goal, i: {"approve": True, "reviewer": i, "reason": "APPROVE"})
    import mongoose.updater as U
    monkeypatch.setattr(U, "stage_update_candidate", lambda z: {"ok": True, "stage_id": "s1"})
    import mongoose.services.mailbox as mb
    monkeypatch.setattr(mb, "send_message", lambda **k: {"ok": True})
    from mongoose.services.sensitive_tools import propose_sensitive_action, confirm_sensitive_action
    p = propose_sensitive_action("self_patch", {"goal": "harden QR logic"})
    t0 = time.time()
    r = confirm_sensitive_action(p["confirm_token"])
    assert time.time() - t0 < 1.0, "sync confirm blocked the calling thread"
    assert r["ok"] and r.get("job_id")
    for _ in range(20):
        time.sleep(0.25)
        job = SP.get_job(r["job_id"])
        if job["status"] not in SP._ACTIVE_STATUSES:
            break
    assert job["status"] == "awaiting_owner_approval"


def test_status_check_alone_reflects_staleness_without_a_new_request(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services import self_patch as SP
    r = SP.request_self_patch("harden QR logic", requested_by="owner")
    jobs = SP._load_jobs()
    jobs[r["job_id"]]["updated_at"] = time.time() - 20000
    SP._save_jobs(jobs)
    assert SP.list_jobs()[0]["status"] == "abandoned"
    assert SP.get_job(r["job_id"])["status"] == "abandoned"


def test_stale_ceiling_has_real_margin_over_worst_case_duration():
    """Worst case: codegen 1800s + gates up to 1800s (3 commands x 600s) + two peer reviews
    at 900s each 1800s = 5400s. The ceiling must clear this by a comfortable margin."""
    from mongoose.services.self_patch import _STALE_JOB_S
    worst_case = 1800 + 1800 + 1800
    assert _STALE_JOB_S - worst_case >= 3600, "less than an hour of margin over worst case"
