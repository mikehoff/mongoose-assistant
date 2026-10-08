"""Fixes from the external 1.40.1 audit, pinned. A1 self-patch confirm dispatch, A2 poke
per-device targeting + honest delivery, A4 embedding opt-out diagnostics, A5 notification
checks agree, A6 Polish action-phrased routing, B1 RTM demand preflight, B2 IMAP graceful
failure, B3 no diet nudge, B4 guest-facing 403."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "a.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()


def test_a1_self_patch_confirm_dispatch_runs(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
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
    r = confirm_sensitive_action(p["confirm_token"])
    assert r["ok"] and r.get("job_id")


def test_a2_poke_targets_only_the_recipient_device(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "t")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://example.duckdns.org")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTERS", "off")
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    from mongoose.services.web_push import save_subscription, list_subscriptions

    def guest(name, label):
        inv = create_guest_invite(owner_label=label)
        tok = inv["invite_url"].rstrip("/").split("/")[-1]
        cl = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": "x"})
        return cl["device_id"], {"X-Mongoose-Device-Token": cl["device_token"]}
    alex_dev, H1 = guest("Alex", "a")
    sam_dev, _ = guest("Samster", "g")
    save_subscription({"endpoint": "https://p.test/a", "keys": {"p256dh": "AA", "auth": "BB"}},
                      device_id=alex_dev, device_profile="trusted_guest", device_label="Alex phone")
    save_subscription({"endpoint": "https://p.test/g", "keys": {"p256dh": "CC", "auth": "DD"}},
                      device_id=sam_dev, device_profile="trusted_guest", device_label="Sam phone")
    import mongoose.services.notifications as N
    hit = {}
    async def fake(title, body="", **kw):
        subs = list_subscriptions(device_ids=kw.get("target_device_ids"),
                                  user_id=kw.get("target_user_id"))
        hit["labels"] = [s["device_label"] for s in subs]
        return {"sent": len(subs)}
    monkeypatch.setattr(N, "send_to_phone", fake)
    r = c.post("/device/irc/send", headers=H1, json={"text": "poke sam"}).json()
    assert hit["labels"] == ["Sam phone"], hit
    assert "on its way" in r["reply"]


def test_a2_poke_unsubscribed_does_not_promise_delivery(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "t")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://example.duckdns.org")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTERS", "off")
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    def guest(name, label):
        inv = create_guest_invite(owner_label=label)
        tok = inv["invite_url"].rstrip("/").split("/")[-1]
        cl = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": "x"})
        return {"X-Mongoose-Device-Token": cl["device_token"]}
    H1 = guest("Alex", "a")
    guest("Zoe", "z")
    r = c.post("/device/irc/send", headers=H1, json={"text": "poke zoe"}).json()
    assert "on its way" not in r["reply"] and "notifications yet" in r["reply"]


def test_a4_embedding_optout_reflected_in_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_AUTO_EMBEDDINGS", "0")
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.services.embeddings import available
    a = available()
    assert a["auto_download"] is False
    assert "disabled by MONGOOSE_AUTO_EMBEDDINGS" in a["hint"]


def test_a5_notification_checks_agree(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "n.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.doctor import run_doctor
    ch = run_doctor()["checks"]
    assert ch["push_to_phone"]["ok"] == ch["notifications"]["ok"] is False


def test_a6_polish_action_request_ranks_plugin_tools_first(monkeypatch, tmp_path):
    from mongoose.services.capability_index import search
    ids = [c["id"] for c in search("jak naprawić wtyczkę wordpress", top_k=5)]
    assert "tool:rtm_build_source" not in ids[:2]
    assert any("wp_plugin" in i for i in ids[:3])


def test_b1_placeholder_flat_demand_never_reaches_the_leaseholder_portal(monkeypatch, tmp_path):
    """create_demand deliberately stays unconditional (it is this subsystem's own test
    suite's established fixture pattern; a hard gate there broke ~26 legitimate call sites).
    The real safety property, a leaseholder never SEES a demand against a nameless
    placeholder flat, is enforced at the portal snapshot instead (external audit B1)."""
    monkeypatch.setenv("MONGOOSE_RTM_PORTAL_BANK_ACCOUNT", "x")
    monkeypatch.setenv("MONGOOSE_RTM_PORTAL_SORT_CODE", "x")
    _db(monkeypatch, tmp_path)
    from mongoose.services import rtm_management as RM
    from mongoose.services import rtm_portal as RP
    RM.seed_flats()
    r = RM.create_demand(document_number="D1", flat="1ECF1",
                         period="1 Jul 2026 to 31 Dec 2026", amount="123.45",
                         issue_date="2026-07-04", due_date="2026-08-01")
    assert r["ok"] and r["status"] == "Issued"  # unconditional creation, unchanged contract
    rows = RP._demand_rows_for_snapshot()
    assert not any(d["document_number"] == "D1" for d in rows), (
        "a demand against an unnamed placeholder flat reached the leaseholder-facing portal")
    # once the flat is genuinely named, the SAME demand becomes visible
    RM.upsert_flat(block="1EC", flat_number="1", leaseholder_name="A Leaseholder")
    rows2 = RP._demand_rows_for_snapshot()
    assert any(d["document_number"] == "D1" for d in rows2)


def test_b2_imap_dead_host_returns_status_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "i.sqlite3"))
    monkeypatch.setenv("IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("IMAP_PORT", "9")
    monkeypatch.setenv("IMAP_USERNAME", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.setenv("IMAP_USE_SSL", "false")
    from mongoose.db import init_db
    init_db()
    from mongoose.services.email_index import sync_imap, sync_imap_incremental
    for f in (sync_imap, sync_imap_incremental):
        r = f()
        assert r["ok"] is False and r["reason"] == "imap_unreachable"


def test_b3_puffiness_copy_has_no_diet_nudge(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services.body_state import log_body_state
    from mongoose.services.body_voice import puffiness_today
    log_body_state(puffiness=4, tension=2, notes="audit")
    s = puffiness_today()["speech"]
    assert "lighter food" not in s and "eat less" not in s


def test_b4_guest_gets_plain_owner_only_403(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "t")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://example.duckdns.org")
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app, client=("203.0.113.9", 5000))
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label="g")
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": "Lee", "public_name": "R", "fun_fact": "x"})
    r = c.get("/cockpit/devices", headers={"X-Mongoose-Device-Token": cl["device_token"]})
    assert r.status_code == 403
    assert "MONGOOSE_ALLOW_REMOTE" not in r.text and "owner" in r.text.lower()


def test_hardening_device_scoped_send_never_falls_back_to_shared_ntfy(monkeypatch, tmp_path):
    """Found during hardening review after A2: target_device_ids was threaded into the
    subscription count/send calls but the FALLBACK branch never checked it, so a poke aimed
    at one unsubscribed device silently broadcast to the shared ntfy topic instead of just
    reporting no delivery. Same class of leak as A2, second channel."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh")
    monkeypatch.setenv("NTFY_TOPIC", "mongoose-private-topic")
    from mongoose.db import init_db
    init_db()
    from mongoose.services.notifications import send_to_phone
    import mongoose.services.web_push as W
    monkeypatch.setattr(W, "send_web_push", lambda *a, **k: {"sent": 0})
    called = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            called["url"] = url

            class R:
                status_code = 200
            return R()
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    import asyncio
    r = asyncio.run(send_to_phone("poke", "someone poked you",
                                  target_device_ids=("nonexistent-device",)))
    assert "url" not in called, "device-scoped send leaked to the shared ntfy topic"
    assert r["sent"] is False and r["reason"] == "no_device_subscription"


def test_hardening_rtm_draft_ledger_timing_no_longer_applies(monkeypatch, tmp_path):
    """The Draft/ledger-timing mechanism was removed when the B1 fix was relocated to the
    portal layer; this pins the CURRENT contract (unconditional creation, exclusion at
    publish time) so a future change cannot silently reintroduce the old broken gate without
    a test noticing."""
    _db(monkeypatch, tmp_path)
    from mongoose.services import rtm_management as RM
    RM.seed_flats()
    r = RM.create_demand(document_number="D2", flat="1ECF2", period="p", amount="10.00",
                         issue_date="2026-07-04", due_date="2026-08-01")
    assert r["ok"] and r["status"] == "Issued"
    with RM.connect() as con:
        debit = con.execute(
            "SELECT COALESCE(SUM(debit_pence),0) FROM rtm_mgmt_ledger_entries WHERE flat_id='1ECF2'"
        ).fetchone()[0]
    assert debit == 1000  # the debit posts immediately again, matching the original contract
