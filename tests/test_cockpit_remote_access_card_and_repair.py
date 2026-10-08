"""The Cockpit "Remote access" card called /cockpit/remote-access/router, but the frontend
checked for rt.ddns / rt.ports / rt.headline, which port_forward_card() never actually
produced -- a live-looking backend call feeding a card that could never render. When asked
to wire the DDNS diagnosis into the UI flow instead of a CLI command, this is the fix: the
route now returns the real setup_state remote_away diagnosis, the card renders it, and a
genuine "Repair now" button calls a new cockpit-nonce-gated repair action that runs the same
chain the paired-phone /device/remote/repair already used."""
from __future__ import annotations

import warnings
import unittest.mock as mock

warnings.filterwarnings("ignore")


def _client(monkeypatch, tmp_path, **extra_env):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "d.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A


def test_router_card_route_now_returns_the_real_diagnosis(monkeypatch, tmp_path):
    c, A = _client(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                   DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.get("/cockpit/remote-access/router", headers=H)
    j = r.json()
    assert r.status_code == 200
    assert "remote_away" in j
    assert "status" in j["remote_away"] and "ok" in j["remote_away"]


def test_cockpit_html_renders_from_the_real_diagnosis_not_the_dead_fields():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "rt.remote_away" in h
    assert "repairRemoteAccess" in h
    assert "raRepairBox" in h
    # The dead condition this replaces must be gone.
    assert "rt.ddns||rt.ports||rt.headline" not in h


def test_cockpit_repair_action_requires_the_nonce(monkeypatch, tmp_path):
    c, A = _client(monkeypatch, tmp_path)
    r = c.post("/cockpit/remote-access/repair")
    assert r.status_code in (401, 403)


def test_cockpit_repair_action_runs_the_same_chain_as_the_phone_route(monkeypatch, tmp_path):
    c, A = _client(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                   DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"ok": True, "listener_accepting": True}), \
         mock.patch("mongoose.ddns.update_now", return_value={"ok": True}), \
         mock.patch("mongoose.ddns.reachability_diagnosis",
                   return_value={"ok": True, "reason": "reachable"}):
        r = c.post("/cockpit/remote-access/repair", headers=H)
    j = r.json()
    assert r.status_code == 200
    assert j["ok"] is True and j["step"] == "reachable"


def test_cockpit_repair_never_needs_a_device_token(monkeypatch, tmp_path):
    """The whole point: someone sitting at the Mac's own Cockpit has no paired phone device
    token, only a cockpit nonce. This must work without one."""
    c, A = _client(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                   DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"ok": False, "listener_accepting": False,
                                "reason": "listener_loopback_only"}):
        r = c.post("/cockpit/remote-access/repair", headers=H)
    assert "X-Mongoose-Device-Token" not in H
    assert r.status_code == 200
    j = r.json()
    assert j["step"] == "listener"


def test_shared_repair_chain_produces_identical_results_for_both_callers(monkeypatch, tmp_path):
    """/device/remote/repair and /cockpit/remote-access/repair must call the exact same
    underlying logic, not two copies that can drift apart."""
    c, A = _client(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                   DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    from mongoose.devices import create_pairing_code, claim_pairing_code
    code = create_pairing_code("primary_phone")["code"]
    cl = claim_pairing_code(code, "phone", "android")
    H_cockpit = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    H_device = {"X-Mongoose-Device-Token": cl["device_token"]}
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"ok": True, "listener_accepting": True}), \
         mock.patch("mongoose.ddns.update_now", return_value={"ok": True}), \
         mock.patch("mongoose.ddns.reachability_diagnosis",
                   return_value={"ok": True, "reason": "reachable"}):
        r1 = c.post("/cockpit/remote-access/repair", headers=H_cockpit).json()
        r2 = c.post("/device/remote/repair", headers=H_device).json()
    assert r1["ok"] == r2["ok"] == True
    assert r1["step"] == r2["step"] == "reachable"
    assert r1["detail"] == r2["detail"]
