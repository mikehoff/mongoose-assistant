"""Two upload lanes: a 30MB chat attachment (browser + phone) and a separate 200MB staging area.

Covers: chat attach imports body-data CSVs and stores generic files with a summary; the 30MB cap is
enforced and points to staging; staging accepts larger files up to 200MB and lists them; both the
browser (cockpit nonce) and phone (owner device token) paths work; and a guest is denied on every
upload route.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import mongoose.app as A
from mongoose.app import app
from mongoose import devices as D
from mongoose.services.uploads import CHAT_ATTACHMENT_MAX, STAGING_MAX

SETS = b"exercise,weight,reps,dt\nSquat,60,5,2026-01-01\n"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "u.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "tok")
    yield


def _owner_token():
    code = D.create_pairing_code(profile="primary_phone")["code"]
    return D.claim_pairing_code(code, "p", "android")["device_token"]


def test_limits_are_30_and_200_mb():
    assert CHAT_ATTACHMENT_MAX == 30 * 1024 * 1024
    assert STAGING_MAX == 200 * 1024 * 1024


def test_cockpit_chat_attach_imports_body_data():
    c = TestClient(app)
    r = c.post("/cockpit/chat/attach", content=SETS,
               headers={"X-Mongoose-Token": "tok", "x-filename": "sets.csv", "content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["lane"] == "body_data" and r.json()["detected"] == "alpha_sets"


def test_cockpit_chat_attach_stores_generic_with_summary():
    c = TestClient(app)
    r = c.post("/cockpit/chat/attach", content=b"hello",
               headers={"X-Mongoose-Token": "tok", "x-filename": "n.txt"})
    j = r.json()
    assert j["ok"] and j["lane"] == "asset" and "summary" in j


def test_chat_attach_rejects_over_30mb_and_points_to_staging():
    c = TestClient(app)
    big = b"x" * (31 * 1024 * 1024)
    r = c.post("/cockpit/chat/attach", content=big,
               headers={"X-Mongoose-Token": "tok", "x-filename": "big.bin"})
    j = r.json()
    assert j["ok"] is False and j.get("too_large_for_chat") is True


def test_staging_accepts_larger_file_and_lists_it():
    c = TestClient(app)
    big = b"y" * (40 * 1024 * 1024)
    r = c.post("/files/stage", content=big,
               headers={"X-Mongoose-Token": "tok", "x-filename": "package.zip"})
    assert r.status_code == 200 and r.json()["lane"] == "staged"
    lst = c.get("/files/staged", headers={"X-Mongoose-Token": "tok"}).json()
    assert any(s["filename"] == "package.zip" for s in lst["staged"])


def test_phone_chat_attach_owner():
    c = TestClient(app)
    owner = _owner_token()
    r = c.post("/device/chat/attach", content=SETS,
               headers={"x-mongoose-device-token": owner, "x-filename": "sets.csv", "content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["detected"] == "alpha_sets"


def test_guest_denied_on_all_upload_routes(monkeypatch):
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", frozenset({"127.0.0.1", "::1", "localhost"}))
    c = TestClient(app)
    code = D.create_pairing_code(profile="trusted_guest")["code"]
    guest = D.claim_pairing_code(code, "g", "android")["device_token"]
    for path in ["/device/chat/attach", "/cockpit/chat/attach", "/files/stage"]:
        r = c.post(path, content=b"x", headers={"x-mongoose-device-token": guest, "x-filename": "x.txt"})
        assert r.status_code == 403, f"guest reached {path} ({r.status_code})"
    r = c.get("/files/staged", headers={"x-mongoose-device-token": guest})
    assert r.status_code == 403
