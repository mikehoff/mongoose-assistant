"""Fixes from a second reviewer's ('Pro') regression triage against 1.43.0, verified against
this tree before merging (their patch predated LAN-first QR work and would not apply
cleanly, but the underlying diagnoses were checked directly against current source).

(1) REAL, CONFIRMED BUG Pro caught that this session's own earlier QR fix missed entirely:
_lan_reachable_now() answered purely from config (host=0.0.0.0, allow_remote=true), never
from a live socket check, even though _lan_socket_reachable() already existed in the same
file for a different caller. A stale uvicorn process bound to loopback from before those
settings were saved would pass the config check and render a confident, warning-free QR that
led straight to ERR_CONNECTION_REFUSED. (2) the QR was rendered unconditionally BEFORE the
reachability check ran, so even the existing (weaker) warning never actually withheld the
QR itself, only added a note beside it. (3) obtain_certificate() (Let's Encrypt for guest
invites) existed and worked but nothing ever called it; guest-invite copy said
`mongoose tls obtain` in a terminal. (4) doctor's Brain Location check set ok=False whenever
a stale .env override was refused, even when the canonical brain was correctly active,
presenting a safety mechanism working exactly as intended as if it were a failure."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path, **extra_env):
    env = {"MONGOOSE_DB_PATH": str(tmp_path / "p.sqlite3"), "MONGOOSE_DATA_DIR": str(tmp_path / "d"),
           "MONGOOSE_HOST": "0.0.0.0", "MONGOOSE_ALLOW_REMOTE": "true"}
    env.update(extra_env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A


def test_lan_reachable_now_checks_the_live_socket_not_just_config(monkeypatch, tmp_path):
    """Config alone must never be sufficient; a stale bound-to-loopback process with
    LAN-enabling config must still read as not reachable."""
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "s.sqlite3"), "MONGOOSE_HOST": "0.0.0.0",
                 "MONGOOSE_ALLOW_REMOTE": "true"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from mongoose.app import _lan_reachable_now
    # In this test process nothing is actually listening on the LAN socket, matching the
    # exact "config says ready, socket says no" mismatch that caused the bug.
    assert _lan_reachable_now() is False


def test_qr_is_actually_withheld_not_just_warned_about(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    r = c.post("/install/pair-code")
    j = r.json()
    assert j.get("qr_blocked") is True
    assert j.get("qr_svg") == ""
    assert j.get("reachability_warning")


def test_second_pairing_endpoint_also_withholds_qr_for_owner_profiles(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/devices/pair-code", json={"profile": "primary_phone"}, headers=H)
    j = r.json()
    assert j.get("qr_blocked") is True and j.get("qr_svg") == ""


def test_trusted_guest_qr_is_not_gated_by_the_owners_lan_socket(monkeypatch, tmp_path):
    """Guests reach Mongoose via DDNS, a completely different reachability question from
    this Mac's own LAN socket; the owner's stale-loopback problem must never block guests."""
    c, A = _setup(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                 DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/devices/pair-code", json={"profile": "trusted_guest"}, headers=H)
    j = r.json()
    assert j.get("qr_blocked") is not True


def test_acme_obtain_route_exists_and_no_copy_mentions_a_terminal(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/tls/acme-obtain", headers=H)
    assert r.status_code == 200
    import pathlib
    src = pathlib.Path("src/mongoose/app.py").read_text()
    assert "mongoose tls obtain" not in src and "mongoose tls status" not in src


def test_brain_location_reports_protection_as_success_not_failure(monkeypatch, tmp_path):
    """REJECTED_BRAIN_OVERRIDES is populated once, at settings-module load time, by a real
    .env scrub; forcing that exact side effect inside an already-running pytest session
    fights module caching rather than testing the actual bug. Monkeypatching the module-level
    dict directly exercises the SAME doctor.py branch (this session already proved the real
    end-to-end mechanism separately, in an isolated process: ok=True, detail contains
    'protected against a stale .env override')."""
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    canon = tmp_path / "home" / ".mongoose" / "data" / "mongoose.sqlite3"
    canon.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(canon))
    from mongoose import settings as smod
    monkeypatch.setattr(smod, "REJECTED_BRAIN_OVERRIDES",
                        {"MONGOOSE_DB_PATH": "./data/mongoose.sqlite3"})
    from mongoose.doctor import run_doctor
    b = run_doctor()["checks"]["brain_location"]
    assert b["ok"] is True
    assert "protected" in b["detail"].lower()
