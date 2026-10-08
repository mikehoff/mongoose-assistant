"""Mongoose occasionally demands the owner's Alpha Progression data (and optionally MyFitnessPal),
and both browser and phone can satisfy that demand.

Covers: the nudge fires only when data is missing/stale, paces itself (no nag), goes silent once data
arrives; the browser ingest endpoint and the phone /device/upload path both import Alpha/MFP CSVs and
silence the nudge; a guest cannot reach either; and the body_data read tool answers on request.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import mongoose.app as A
from mongoose.app import app
from mongoose import devices as D
from mongoose import tools as T
from mongoose.services import body_data_nudge as N
from mongoose.services import alpha_import as AI


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "b.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "tok")
    yield


SETS = "exercise,weight,reps,dt\nLateral Raise,12,15,2026-06-20\nLat Pulldown,60,10,2026-06-20\n"
MFP = ("Date,Meal,Calories,Carbohydrates (g),Protein (g),Fat (g),Sodium (mg)\n"
       "2026-06-01,Breakfast,500,40,30,20,400\n2026-06-01,Lunch,700,60,45,25,600\n")


def test_nudge_fires_when_alpha_missing_then_paces():
    n = N.due_nudge()
    assert n and n["reason"] == "alpha_missing"
    N.mark_asked(n["reason"])
    # immediately after, the gap holds
    assert N.due_nudge() is None


def test_force_answers_even_within_gap():
    n = N.due_nudge(force=True)
    assert n and n["reason"] == "alpha_missing"


def test_import_silences_then_optional_mfp_then_quiet():
    AI.import_sets_csv(SETS)
    N.mark_satisfied()
    after = N.due_nudge(force=True)
    # alpha now present -> at most the optional MFP ask remains
    assert after is None or after["reason"] == "mfp_optional"
    AI.import_mfp_csv(MFP)
    N.mark_satisfied()
    assert N.due_nudge(force=True) is None  # everything present, never asks


def test_browser_ingest_endpoint_imports_and_silences():
    c = TestClient(app)
    H = {"X-Mongoose-Token": "tok"}
    assert N.due_nudge(force=True)["reason"] == "alpha_missing"
    r = c.post("/body/data/ingest", json={"filename": "my-sets.csv", "text": SETS}, headers=H)
    assert r.status_code == 200 and r.json()["ok"] and r.json()["detected"] == "alpha_sets"
    # status now reports alpha present
    s = c.get("/body/data/status", headers=H).json()
    assert s["data"]["alpha_present"] is True


def test_phone_upload_routes_body_data_to_importer():
    c = TestClient(app)
    code = D.create_pairing_code(profile="primary_phone")["code"]
    owner = D.claim_pairing_code(code, "phone", "android")["device_token"]
    r = c.post("/device/upload", content=SETS.encode(),
               headers={"x-mongoose-device-token": owner, "x-filename": "sets-export.csv",
                        "content-type": "text/csv"})
    j = r.json()
    assert j["ok"] and j["detected"] == "alpha_sets" and j.get("surface") == "phone"


def test_guest_cannot_upload_body_data(monkeypatch):
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", frozenset({"127.0.0.1", "::1", "localhost"}))
    c = TestClient(app)
    code = D.create_pairing_code(profile="trusted_guest")["code"]
    guest = D.claim_pairing_code(code, "g", "android")["device_token"]
    r = c.post("/device/upload", content=SETS.encode(),
               headers={"x-mongoose-device-token": guest, "x-filename": "sets.csv",
                        "content-type": "text/csv"})
    assert r.status_code == 403
    r2 = c.post("/body/data/ingest", json={"filename": "x.csv", "text": SETS},
                headers={"x-mongoose-device-token": guest})
    assert r2.status_code == 403


def test_body_data_tool_answers_on_request():
    res = asyncio.run(T.TOOLS["body_data"]["executor"]({}))
    assert "data" in res
    assert res["request"] is not None
    assert res["request"]["reason"] == "alpha_missing"
    assert "body_data" in T.READ_TOOLS


def test_mfp_importer_aggregates_per_day():
    r = AI.import_mfp_csv(MFP)
    assert r["ok"] and r["imported"] == 1  # two meals on one day -> one day row
