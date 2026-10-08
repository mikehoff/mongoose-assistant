"""Reconciles two more submissions against this session's own boundaries.

(1) Cross-origin PWA install: a REAL, currently-live bug in the shipped code this session
produced earlier. pair()'s JS stores the device token via localStorage on whatever origin
/pair loaded from, then hands off to launchpad_url, which is correctly DDNS when configured
(the earlier fix), but localStorage is origin-scoped: a token stored on the LAN origin is
invisible to JS running on the DDNS origin. The phone would land on the DDNS launchpad with
no way to know it's already paired. Fixed with a URL-fragment handoff (never sent to the
server or a request log, unlike a query string) that the destination origin imports and
immediately strips via history.replaceState.

(2) DDNS "configured" vs "proven": already correctly gated in this tree (remote_away's ok
already requires reachability_diagnosis to say ok, not just DDNS+cert existing) -- this pins
that plus the genuinely new piece: a self-test run from the Mac can never fully prove a
genuinely external client can reach it, so a paired device (the owner's own phone, on
cellular) can report back that it reached the public URL from outside, which is now recorded
and reflected as a stronger, explicitly-labelled confidence level.

(3) /device/remote/repair refreshes DDNS and re-diagnoses on demand, owner-devices only, and
deliberately does NOT attempt any UPnP/NAT-PMP/port-forward automation: that would silently
open a mapping from the ENTIRE PUBLIC INTERNET to this Mac, the one category of action this
whole reconciliation already agreed deserves a real, visible, owner-performed step, not
automation, unlike LAN reachability/HTTPS/phone pairing which stay bounded to the home
network and were correctly defaulted on elsewhere this session."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path, **extra):
    env = {"MONGOOSE_DB_PATH": str(tmp_path / "d.sqlite3"), "MONGOOSE_DATA_DIR": str(tmp_path / "d")}
    env.update(extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A


def test_pair_page_carries_the_token_across_origins_via_fragment():
    from mongoose.cockpit import pair_html
    h = pair_html()
    assert "handoffUrl" in h
    assert "mongoose_device_token=" in h
    assert "dest.origin===location.origin" in h


def test_launchpad_and_phone_cockpit_import_and_strip_the_fragment():
    from mongoose.cockpit import device_launchpad_html, phone_cockpit_html
    for name, html in (("launchpad", device_launchpad_html()), ("phone_cockpit", phone_cockpit_html())):
        assert "importPairingHandoff" in html, name
        assert "history.replaceState" in html, name


def test_remote_away_requires_proof_not_just_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    import unittest.mock as mock
    from mongoose import ddns, acme_tls
    with mock.patch.object(ddns, "reachability_diagnosis",
                          return_value={"ok": False, "reason": "hairpin_or_no_portforward"}), \
         mock.patch.object(acme_tls, "certificate_status", return_value={"present": True}):
        from mongoose.setup_state import setup_state
        s = setup_state(only=["remote_away"])["surfaces"]["remote_away"]
        assert s["ok"] is False


def test_external_device_proof_upgrades_remote_away_when_self_test_cannot(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    from mongoose.db import init_db
    init_db()
    import unittest.mock as mock
    from mongoose import ddns, acme_tls
    with mock.patch.object(ddns, "reachability_diagnosis",
                          return_value={"ok": False, "reason": "hairpin_or_no_portforward"}), \
         mock.patch.object(acme_tls, "certificate_status", return_value={"present": True}):
        from mongoose.db import set_app_state, utc_now_iso
        set_app_state("remote_external_proof_at", utc_now_iso())
        from mongoose.setup_state import setup_state
        s = setup_state(only=["remote_away"])["surfaces"]["remote_away"]
        assert s["ok"] is True
        assert s["facts"]["externally_proven"] is True


def test_device_remote_proof_route_requires_pairing(monkeypatch, tmp_path):
    """403, not a bespoke 401: _device_from_request already raises 403 for any invalid
    token, the same convention every other /device/* route uses; this route should not
    invent a different status code for the same fact."""
    c, A = _setup(monkeypatch, tmp_path)
    r = c.post("/device/remote/proof", json={"url": "https://yourname.duckdns.org/health"})
    assert r.status_code == 403


def test_device_remote_proof_route_records_for_a_paired_device(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                 DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    from mongoose.devices import create_pairing_code, claim_pairing_code
    from mongoose.ddns import public_url
    from mongoose.setup_wizard import effective_env
    real_url = public_url(effective_env())
    code = create_pairing_code("primary_phone")["code"]
    cl = claim_pairing_code(code, "phone", "android")
    r = c.post("/device/remote/proof", json={"url": real_url + "/health"},
              headers={"X-Mongoose-Device-Token": cl["device_token"]})
    assert r.status_code == 200 and r.json()["ok"]
    from mongoose.db import get_app_state
    assert get_app_state("remote_external_proof_at")


def test_device_remote_proof_rejects_a_url_that_does_not_match_the_real_configured_address(monkeypatch, tmp_path):
    """The actual vulnerability here was never 'which profile called this'; it was that the
    route would blindly record whatever URL string ANY caller sent. A guest happening to be
    away from home and genuinely reaching the real public URL is legitimate proof; a
    fabricated or mismatched URL, from ANY caller including an owner device by mistake, must
    never be recorded."""
    c, A = _setup(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                 DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    from mongoose.devices import create_pairing_code, claim_pairing_code
    code = create_pairing_code("trusted_guest")["code"]
    cl = claim_pairing_code(code, "guest", "android")
    r = c.post("/device/remote/proof", json={"url": "https://totally-fake-url.com/health"},
              headers={"X-Mongoose-Device-Token": cl["device_token"]})
    assert r.json()["ok"] is False and r.json()["error"] == "url_mismatch"
    from mongoose.db import get_app_state
    assert not get_app_state("remote_external_proof_at")


def test_remote_repair_is_owner_device_only(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    from mongoose.devices import create_pairing_code, claim_pairing_code
    gcode = create_pairing_code("trusted_guest")["code"]
    gcl = claim_pairing_code(gcode, "guest", "android")
    r_guest = c.post("/device/remote/repair", headers={"X-Mongoose-Device-Token": gcl["device_token"]})
    assert r_guest.status_code == 403

    ocode = create_pairing_code("primary_phone")["code"]
    ocl = claim_pairing_code(ocode, "phone", "android")
    r_owner = c.post("/device/remote/repair", headers={"X-Mongoose-Device-Token": ocl["device_token"]})
    assert r_owner.status_code == 200


def test_remote_repair_never_attempts_router_automation():
    import inspect
    import mongoose.app as A
    src = inspect.getsource(A.device_remote_repair)
    for forbidden in ("import upnp", "import miniupnpc", "nat_pmp_map",
                      "request_port_forward", "subprocess"):
        assert forbidden not in src, forbidden
