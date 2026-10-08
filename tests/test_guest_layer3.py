"""Layer-3 polish: owner can revoke pending/claimed guest invites, revoked guests drop out of the
directory, the PWA plumbing (manifest, service worker, share target) is publicly fetchable and the
service-worker cache is versioned so old JS is evicted after an update.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MONGOOSE_ALLOW_REMOTE", raising=False)
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def _make_invite(min_version="1.1", label="Jo"):
    from mongoose.guest_invites import create_guest_invite
    res = create_guest_invite(owner_label=label, min_version=min_version,
                              base_url="https://yourname.duckdns.org:8765")
    return res["invite_url"].rsplit("/", 1)[-1]


def _payload(**extra):
    base = {"mongoose_name": "Jo", "public_name": "Jo from Flat 4", "fun_fact": "I test things."}
    base.update(extra)
    return base


def test_revoke_unclaimed_invite_blocks_claim(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import claim_guest_invite, list_invites, revoke_invite
    token = _make_invite()
    iid = list_invites()[0]["invite_id"]
    assert revoke_invite(iid)["ok"] is True
    res = claim_guest_invite(token, _payload())
    assert res.get("ok") is False and res.get("error") == "revoked_invite"


def test_revoke_claimed_invite_also_revokes_the_guest_device(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import claim_guest_invite, list_invites, revoke_invite
    token = _make_invite()
    assert claim_guest_invite(token, _payload()).get("ok") is True
    iid = list_invites()[0]["invite_id"]
    r = revoke_invite(iid)
    assert r["ok"] is True and r["device_revoked"] is True
    from mongoose.db import connect
    with connect() as con:
        live = con.execute("SELECT COUNT(*) FROM paired_devices WHERE profile='trusted_guest' AND revoked=0").fetchone()[0]
    assert live == 0


def test_list_invites_reports_state(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import claim_guest_invite, list_invites
    token = _make_invite()
    assert list_invites()[0]["state"] == "pending"
    claim_guest_invite(token, _payload())
    assert list_invites()[0]["state"] == "claimed"


def test_directory_excludes_revoked_guest(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import (claim_guest_invite, list_invites, revoke_invite,
                                        guest_public_directory)
    token = _make_invite()
    claim_guest_invite(token, _payload(announce_join=True, share_fun_fact=True))
    assert any(d["public_name"] == "Jo from Flat 4" for d in guest_public_directory())
    revoke_invite(list_invites()[0]["invite_id"])
    assert not any(d["public_name"] == "Jo from Flat 4" for d in guest_public_directory())


def test_cockpit_invite_endpoints_require_nonce(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/cockpit/guests/invites").status_code == 403
    assert c.post("/cockpit/guests/invites/whatever/revoke").status_code == 403
    nonce = c.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = c.get("/cockpit/guests/invites", headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200 and "invites" in r.json() and "readiness" in r.json()


def test_guest_invites_route_has_one_nonce_gated_handler():
    """Regression: a duplicate no-nonce handler for the same path is a security footgun, even if
    FastAPI currently resolves the first route. There must be exactly one owner invite-list route."""
    from mongoose.app import app
    matches = [r for r in app.routes if getattr(r, "path", None) == "/cockpit/guests/invites" and "GET" in getattr(r, "methods", set())]
    assert len(matches) == 1


def test_pwa_plumbing_is_public_and_sw_is_versioned(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    import mongoose.app as A
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", {"127.0.0.1", "::1", "localhost"})  # simulate remote
    c = TestClient(A.app, raise_server_exceptions=False)
    for p in ["/manifest.webmanifest", "/device/manifest.webmanifest", "/sw.js", "/share-target?text=hi"]:
        assert c.get(p).status_code == 200, f"{p} must be publicly fetchable for PWA to work"
    sw = c.get("/sw.js").text
    from mongoose import __version__
    assert f"mongoose-shell-{__version__}" in sw and "__VER__" not in sw


def test_launchpad_hides_push_card_for_guests():
    """The notifications/push card is gated on owner devices in the launchpad markup."""
    import mongoose.cockpit as C
    html = C.device_launchpad_html()
    # the push card render is wrapped so only owners get it
    assert "isOwner?`<div class=\"card\" id=\"notif-card\"" in html
    assert "if(isOwner) updateNotifCard()" in html
