"""Pairing single-use + stale-tab session epoch.

A3 (single-use, end to end): the code burns on first claim AND the cockpit can see the moment
it's consumed, so it stops showing a scannable QR instead of leaving a dead code on screen.
F5 (stale-tab guard): a session epoch advances every time a cockpit loads, so an older tab knows
it's been superseded and stops writing.
"""
from fastapi.testclient import TestClient


def _client():
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


# ---- A3: single-use + status visibility ----

def test_second_claim_of_same_code_is_rejected():
    from mongoose import devices as D
    code = D.create_pairing_code(profile="trusted_guest")["code"]
    assert D.claim_pairing_code(code, "Phone A", "phone")["ok"] is True
    r2 = D.claim_pairing_code(code, "Phone B", "phone")
    assert r2["ok"] is False
    assert "already used" in (r2.get("error") or "")


def test_pairing_code_status_flips_on_claim():
    from mongoose import devices as D
    code = D.create_pairing_code(profile="primary_phone")["code"]
    before = D.pairing_code_status(code)
    assert before["found"] is True and before["consumed"] is False
    D.claim_pairing_code(code, "Phone A", "phone")
    assert D.pairing_code_status(code)["consumed"] is True


def test_pair_status_endpoint_reports_consumption():
    from mongoose import devices as D
    c = _client()
    code = D.create_pairing_code(profile="primary_phone")["code"]
    assert c.get("/cockpit/devices/pair-status", params={"code": code}).json()["consumed"] is False
    D.claim_pairing_code(code, "Phone A", "phone")
    assert c.get("/cockpit/devices/pair-status", params={"code": code}).json()["consumed"] is True


def test_unknown_code_status_is_gone():
    from mongoose import devices as D
    s = D.pairing_code_status("definitely-not-a-real-code")
    assert s["found"] is False


# ---- F5: stale-tab session epoch ----

def test_session_epoch_advances_each_claim():
    c = _client()
    a = c.get("/cockpit/session/claim").json()
    b = c.get("/cockpit/session/claim").json()
    assert b["epoch"] > a["epoch"]
    assert a["boot_id"] == b["boot_id"]  # same process, stable boot id


def test_session_current_matches_latest_claim():
    c = _client()
    claimed = c.get("/cockpit/session/claim").json()
    cur = c.get("/cockpit/session/current").json()
    # current never goes backwards, and an older tab (lower epoch) can detect it's superseded
    assert cur["epoch"] >= claimed["epoch"]
    assert cur["boot_id"] == claimed["boot_id"]
