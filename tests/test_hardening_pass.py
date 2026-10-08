"""Self-directed hardening beyond the external audits: found by adversarially testing my own
A2 poke fix. (1) lounge poke must never resolve an account user, even if one matching the
fuzzy name is registered, because account poking already has its own consented, session-
gated route and an unauthenticated lounge guest must not reach the owner's real phone by
saying a name. (2) lounge poke had zero rate limiting; a guest could spam any target.
(3) same-lounge-name collision: excluding the sender AFTER resolving (not before) meant
'poke alex' with two Alexes always resolved to the sender and got wrongly blocked as a
self-poke. (4) self_patch had no concurrency guard against overlapping expensive jobs."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "h.sqlite3"),
                 "MONGOOSE_DATA_DIR": str(tmp_path / "d"),
                 "MONGOOSE_ALLOW_REMOTE": "true", "MONGOOSE_HOST": "0.0.0.0",
                 "MONGOOSE_API_TOKEN": "t", "MONGOOSE_LOUNGE_STARTERS": "off",
                 "MONGOOSE_PUBLIC_URL": "https://example.duckdns.org"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app)


def _guest(c, name, label):
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label=label)
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": "x"})
    return cl, {"X-Mongoose-Device-Token": cl["device_token"]}


def test_lounge_poke_reaches_account_only_with_consent(monkeypatch, tmp_path):
    # Design change (owner asked for it): a chat poke ("poke Alex") from the lounge or private
    # chat DOES reach the owner's account now, but gated by the recipient's allow_poke consent
    # (and rate limiting). Consent ON -> delivered to the account; consent OFF -> refused. This
    # replaces the old "lounge poke never reaches an account" rule with a consent-based one.
    c = _setup(monkeypatch, tmp_path)
    import mongoose.services.notifications as N
    calls = []
    async def fake(title, body="", **kw):
        calls.append(kw.get("target_user_id"))
        return {"ok": True, "sent": 1}
    monkeypatch.setattr(N, "send_to_phone", fake)
    from mongoose.services.auth_accounts import create_account, update_profile
    acct = create_account("yourname", "pw-abc-123", display_name="Alex")
    _, H1 = _guest(c, "Kasia", "k")
    r = c.post("/device/irc/send", headers=H1, json={"text": "poke Alex"}).json()
    assert "poked Alex" in r["reply"]
    assert acct["user_id"] in calls
    # consent OFF: a different guest's poke is refused and delivers nothing new
    update_profile(acct["user_id"], allow_poke=False)
    _, H2 = _guest(c, "Sam", "g")
    r2 = c.post("/device/irc/send", headers=H2, json={"text": "poke Alex"}).json()
    assert "pokes turned off" in r2["reply"]
    assert calls.count(acct["user_id"]) == 1


def test_lounge_poke_is_rate_limited(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    import mongoose.services.notifications as N
    sent = {"n": 0}
    async def counting(title, body="", **kw):
        sent["n"] += 1
        return {"ok": True, "sent": 1}
    monkeypatch.setattr(N, "send_to_phone", counting)
    _, H1 = _guest(c, "Kasia", "k")
    _guest(c, "Sammy", "g")
    replies = [c.post("/device/irc/send", headers=H1, json={"text": "poke sam"}).json()["reply"]
               for _ in range(6)]
    assert sent["n"] < 6
    assert any("give it" in r or "enough pokes" in r for r in replies)


def test_same_lounge_name_collision_resolves_to_the_other_person(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    import mongoose.services.notifications as N
    targeted = {}
    async def fake(title, body="", **kw):
        targeted["ids"] = kw.get("target_device_ids")
        return {"ok": True, "sent": 1}
    monkeypatch.setattr(N, "send_to_phone", fake)
    cl1, H1 = _guest(c, "Alex", "a1")
    cl2, _ = _guest(c, "Alex", "a2")
    r = c.post("/device/irc/send", headers=H1, json={"text": "poke alex"}).json()
    assert "cannot poke yourself" not in r["reply"] and "poked Alex" in r["reply"]
    assert targeted["ids"] == (cl2["device_id"],)


def test_self_patch_refuses_a_second_job_while_one_is_active(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services.self_patch import request_self_patch
    r1 = request_self_patch("harden QR logic", requested_by="owner")
    r2 = request_self_patch("also fix the lounge", requested_by="owner")
    assert r1["ok"] and not r2["ok"] and "already in progress" in r2["error"]
