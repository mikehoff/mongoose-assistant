from urllib.parse import urlparse


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_INPROCESS_SCHEDULER", "false")
    monkeypatch.setenv("MONGOOSE_RUNTIME_WATCHDOG", "false")
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def _token(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def test_guest_invite_requires_https_ddns_by_default(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import create_guest_invite
    res = create_guest_invite(owner_label="Sam")
    assert res["ok"] is False
    assert res["error"] == "guest_remote_access_not_ready"
    codes = {p["code"] for p in res.get("problems", [])}
    assert "no_dynamic_dns" in codes or "no_https" in codes


def test_guest_invite_uses_https_ddns_and_salts_owner_label(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import create_guest_invite
    res = create_guest_invite(owner_label="Sam from book club", base_url="https://yourname.duckdns.org:8765")
    assert res["ok"] is True
    assert res["invite_url"].startswith("https://yourname.duckdns.org:8765/guest/invite/")
    assert "Sam" not in res["invite_url"]
    assert res["expires_in_days"] == 5
    assert res["min_version"] == "1.1"

    from mongoose.db import connect
    with connect() as con:
        row = con.execute("SELECT owner_label, owner_label_salt, owner_label_hash FROM guest_invites").fetchone()
    assert row["owner_label"] == "Sam from book club"
    assert row["owner_label_salt"]
    assert row["owner_label_hash"]
    assert row["owner_label_hash"] != row["owner_label"]


def test_guest_invite_page_contains_delight_and_questions(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import create_guest_invite
    res = create_guest_invite(owner_label="Anna", base_url="https://yourname.duckdns.org:8765")
    token = _token(res["invite_url"])
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get(f"/guest/invite/{token}")
    assert r.status_code == 200
    html = r.text
    assert "mongooseHero" in html
    assert "gooseHero" in html
    assert "loadbar 2s" in html
    assert "Rajdhani" in html
    assert "You\u2019re invited to join Mongoose" in html
    assert "How do you want Mongoose to refer to you?" in html
    assert "How do you want human guests to refer to you?" in html
    assert "A fun fact about yourself" in html
    assert "Can we tell other guests that you joined?" in html
    assert "Can we tell other guests your fun fact?" in html
    # After claiming, the guest is shown a savable personal login (the identity handoff), the
    # same courtesy owner pairing gives, instead of the old bare "thank you" bounce.
    assert "You are in!" in html
    assert "personal Mongoose login" in html
    assert "showGuestIdentity" in html


def test_guest_invite_claim_creates_trusted_guest_profile(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import create_guest_invite
    res = create_guest_invite(owner_label="Sam", base_url="https://yourname.duckdns.org:8765")
    token = _token(res["invite_url"])
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    claim = c.post(f"/guest/invite/{token}/claim", json={
        "mongoose_name": "Samvy",
        "public_name": "Sam",
        "fun_fact": "I once fixed a bike with a teaspoon.",
        "announce_join": True,
        "share_fun_fact": False,
        "client_version": "1.1",
    })
    assert claim.status_code == 200
    body = claim.json()
    assert body["ok"] is True
    assert body["profile"] == "trusted_guest"
    assert body["device_token"]

    from mongoose.db import connect
    with connect() as con:
        prof = con.execute("SELECT * FROM guest_profiles WHERE device_id=?", (body["device_id"],)).fetchone()
        inv = con.execute("SELECT claimed_device_id, claimed_at FROM guest_invites").fetchone()
    assert prof["owner_label"] == "Sam"
    assert prof["mongoose_name"] == "Samvy"
    assert prof["public_name"] == "Sam"
    assert prof["announce_join"] == 1
    assert prof["share_fun_fact"] == 0
    assert inv["claimed_device_id"] == body["device_id"]
    assert inv["claimed_at"]

    from mongoose.devices import device_for_token
    dev = device_for_token(body["device_token"])
    assert dev["profile"] == "trusted_guest"


def test_guest_invite_enforces_minimum_version(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import create_guest_invite
    res = create_guest_invite(owner_label="Lee", min_version="9.9", base_url="https://yourname.duckdns.org:8765")
    token = _token(res["invite_url"])
    from mongoose.guest_invites import claim_guest_invite
    claim = claim_guest_invite(token, {
        "mongoose_name": "Lee",
        "public_name": "Lee",
        "fun_fact": "I like rigorous specs.",
        "client_version": "1.1",
    })
    assert claim["ok"] is False
    # Gating is on the real running server version (1.1 < 9.9), not the browser-supplied
    # client_version, so a spoofed client_version cannot bypass it.
    assert claim["error"] == "server_version_too_old"
    assert claim["min_version"] == "9.9"


def test_cockpit_guest_invite_endpoint_uses_remote_https_prereqs(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    cert = tmp_path / "fullchain.pem"
    key = tmp_path / "privkey.pem"
    cert.write_text("dummy cert")
    key.write_text("dummy key")
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "sillygoose")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "token")
    monkeypatch.setenv("MONGOOSE_TLS_CERTFILE", str(cert))
    monkeypatch.setenv("MONGOOSE_TLS_KEYFILE", str(key))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "owner-token")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/cockpit/guests/invite", json={"owner_label": "Sam"}, headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["invite_url"].startswith("https://yourname.duckdns.org:8765/guest/invite/")


def test_readiness_endpoint_mirrors_invite_refusal(tmp_path, monkeypatch):
    # The setup-tab chase reads /cockpit/guests/readiness; it must report the SAME prerequisites
    # that create_guest_invite enforces, so the chase names exactly what invites demand.
    _fresh(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    r = c.get("/cockpit/guests/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    codes = {p["code"] for p in body.get("problems", [])}
    assert "no_dynamic_dns" in codes or "no_https" in codes
    # and it must NOT leak any invite tokens/list
    assert "invites" not in body and "token" not in body
