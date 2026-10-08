"""The owner must never face a poke sign-in wall. A paired primary_phone device gets an
idempotent poke identity bound to the device (not to a client-held password), so the owner
always appears in others' poke dropdowns and can be poked. Guests are refused this route."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "o.sqlite3"),
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


def test_get_or_create_owner_account_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "a.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services import auth_accounts as AA
    a = AA.get_or_create_owner_account("dev-123", "Alex")
    b = AA.get_or_create_owner_account("dev-123", "Alex")
    assert a["ok"] and b["ok"]
    assert a["user_id"] == b["user_id"]           # same identity, not a duplicate
    assert a["username"] == "alex" and a["display_name"] == "Alex"
    # the owner is pokeable and shows up for others
    assert any(u["username"] == "alex" and u["allow_poke"] for u in AA.list_users())


def test_owner_endpoint_mints_and_logs_in_owner_but_refuses_guest(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    # Pair an owner device (primary_phone) via the real pairing flow.
    from mongoose import devices
    code = devices.create_pairing_code(profile="primary_phone")["code"]
    claimed = devices.claim_pairing_code(code, "Alex", "phone", display_name="Alex")
    otok = claimed["device_token"]
    r = c.post("/account/owner-identity", headers={"X-Mongoose-Device-Token": otok})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["username"] == "alex"
    # session cookie was set -> /account/me now sees the owner as signed in
    me = c.get("/account/me").json()
    assert me.get("authenticated") and me.get("account") is True and me.get("username") == "alex"
    # a guest device is refused
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label="g")
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": "Jordan", "public_name": "Jordan", "fun_fact": "x"})
    rg = c.post("/account/owner-identity", headers={"X-Mongoose-Device-Token": cl["device_token"]})
    assert rg.status_code == 403
