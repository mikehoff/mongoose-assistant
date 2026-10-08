"""Fixes from the 'additional findings' audit (10 items) plus two bugs caught by
re-scrutinising those very fixes: (a) the push_unsubscribe privileged check initially reused
_caller_is_authenticated, whose contract also covers any logged-in account session, not just
the owner; (b) the new document-number-embedding receipt_number broke PDF rendering when a
real document number contains '/', since it was used raw as a filesystem path component."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_finding1_remote_first_run_pair_code_requires_owner_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "o.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.install_progress import begin_install, set_step
    begin_install()
    for k in ["prerequisites", "environment", "database", "server", "services",
             "models", "smoke", "watchdog", "ready"]:
        set_step(k, "done", "test")
    from mongoose.app import app
    from fastapi.testclient import TestClient
    remote = TestClient(app, client=("192.168.1.55", 52344))
    r = remote.post("/install/pair-code")
    assert r.status_code == 401 and r.json()["detail"] == "owner_secret_required"
    loopback = TestClient(app)
    r2 = loopback.post("/install/pair-code")
    assert r2.status_code == 200


def test_finding2_receipt_amount_must_match_the_billed_demand(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services import rtm_management as M
    from mongoose.services import rtm_trust as T
    M.seed_flats()
    M.upsert_flat(block="1EC", flat_number=1, leaseholder_name="Alex",
                 correspondence_address="Flat 1", payment_reference="ALEX6F1")
    M.create_demand(document_number="D1", flat="1ECF1", period="p", amount="50.00",
                    issue_date="2026-07-04", due_date="2026-08-01")
    csv_path = tmp_path / "metro.csv"
    csv_path.write_text("Date,Description,Reference,Credit\n2026-07-04,Pay,ALEX6F1,50.00\n")
    M.import_metro_csv(str(csv_path))
    bad = T.build_receipt("1ECF1", "D1", "ALEX6F1", 1, "2026-07-04")
    assert not bad["ok"] and bad["reason"] == "amount_mismatch"
    good = T.build_receipt("1ECF1", "D1", "ALEX6F1", 5000, "1999-01-01")
    assert good["ok"] and good["amount"] == "£50.00" and good["received_date"] == "2026-07-04"


def test_finding3_receipts_for_the_same_flat_never_collide(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "r2.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d2"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services import rtm_management as M
    from mongoose.services import rtm_trust as T
    M.seed_flats()
    M.upsert_flat(block="1EC", flat_number=1, leaseholder_name="Alex",
                 correspondence_address="Flat 1", payment_reference="ALEX6F1")
    M.create_demand(document_number="D-A", flat="1ECF1", period="First", amount="50.00",
                    issue_date="2026-07-04", due_date="2026-08-01")
    M.create_demand(document_number="D-B", flat="1ECF1", period="Second", amount="60.00",
                    issue_date="2026-08-04", due_date="2026-09-01")
    csv_path = tmp_path / "metro.csv"
    csv_path.write_text("Date,Description,Reference,Credit\n"
                       "2026-07-04,Pay 1,ALEX6F1,50.00\n2026-08-04,Pay 2,ALEX6F1,60.00\n")
    M.import_metro_csv(str(csv_path))
    T.build_receipt("1ECF1", "D-A", "ALEX6F1", 5000, "2026-07-04")
    T.build_receipt("1ECF1", "D-B", "ALEX6F1", 6000, "2026-08-04")
    receipts = T.list_receipts("1ECF1")
    assert len(receipts) == 2


def test_finding5_crisis_wording_gets_a_deterministic_safety_response(monkeypatch, tmp_path):
    import asyncio
    from mongoose.services import guest_tasks as GT
    r = asyncio.run(GT.reflect("guest:abc", "I want to kill myself"))
    assert r.get("crisis") is True and "999" in r["text"]
    r2 = asyncio.run(GT.reflect("guest:abc", "I'm having a rough week at work"))
    assert not r2.get("crisis")


def test_finding4_polish_crisis_content_suppresses_lounge_reflection(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "l.sqlite3"))
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    from mongoose.services import lounge_surpriser as LS
    LS.store_human_message(device_id="d1", sender="Ania", text="mam depresję i myśli samobójcze",
                           line="Ania: mam depresję i myśli samobójcze")
    assert LS.lounge_snapshot()["safe_to_reflect"] is False


def test_finding4_ordinary_polish_chat_still_reflectable(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "l2.sqlite3"))
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    from mongoose.services import lounge_surpriser as LS
    LS.store_human_message(device_id="d3", sender="Kasia", text="super pogoda, idziemy na spacer?",
                           line="Kasia: super pogoda, idziemy na spacer?")
    assert LS.lounge_snapshot()["safe_to_reflect"] is True


def test_finding4_emoji_does_not_trip_the_script_backstop(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "l3.sqlite3"))
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    from mongoose.services import lounge_surpriser as LS
    LS.store_human_message(device_id="d4", sender="Kasia", text="pizza night 🍕🎉 who's in?",
                           line="Kasia: pizza night 🍕🎉 who's in?")
    assert LS.lounge_snapshot()["safe_to_reflect"] is True


def test_finding6_high_sensitivity_health_context_always_needs_review(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "k.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.knowledge_import.compilers import _health_sensitive
    from mongoose.knowledge_import.schemas import Claim, Sensitivity, SourceBasis
    claim = Claim(claim_id="c1", predicate="has_health_context", object="unusual marker",
                 summary="a summary with no matching keyword at all",
                 source_record_key="r1", source_basis=SourceBasis.user_stated,
                 confidence=0.9, sensitivity=Sensitivity.high)
    assert _health_sensitive(claim) is True


def test_finding7_guest_can_unsubscribe_own_but_not_anothers(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "p.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.devices import create_pairing_code, claim_pairing_code
    from mongoose.app import app
    from fastapi.testclient import TestClient
    client = TestClient(app, client=("192.168.1.56", 50000))
    code1 = create_pairing_code("trusted_guest")["code"]
    c1 = claim_pairing_code(code1, "Guest 1", "guest_web")
    code2 = create_pairing_code("trusted_guest")["code"]
    c2 = claim_pairing_code(code2, "Guest 2", "guest_web")
    client.post("/push/subscribe", json={"subscription": {"endpoint": "https://p/a",
               "keys": {"p256dh": "a", "auth": "b"}}}, headers={"X-Mongoose-Device-Token": c1["device_token"]})
    r_own = client.post("/push/unsubscribe", json={"endpoint": "https://p/a"},
                        headers={"X-Mongoose-Device-Token": c1["device_token"]})
    assert r_own.status_code == 200
    client.post("/push/subscribe", json={"subscription": {"endpoint": "https://p/b",
               "keys": {"p256dh": "c", "auth": "d"}}}, headers={"X-Mongoose-Device-Token": c2["device_token"]})
    r_other = client.post("/push/unsubscribe", json={"endpoint": "https://p/b"},
                          headers={"X-Mongoose-Device-Token": c1["device_token"]})
    assert r_other.status_code == 403


def test_finding7_ordinary_account_session_is_not_privileged(monkeypatch, tmp_path):
    """Caught by re-scrutinising the fix itself: reusing _caller_is_authenticated (any
    logged-in account) instead of an owner-specific check would have let one ordinary
    account remove ANY other account's or guest's subscription."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "p2.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.devices import create_pairing_code, claim_pairing_code
    from mongoose.app import app
    from fastapi.testclient import TestClient
    from mongoose.services import auth_accounts as AA
    import mongoose.session_auth as SA
    client = TestClient(app, client=("192.168.1.56", 50000))
    code = create_pairing_code("trusted_guest")["code"]
    victim = claim_pairing_code(code, "Victim", "guest_web")
    client.post("/push/subscribe", json={"subscription": {"endpoint": "https://p/victim",
               "keys": {"p256dh": "a", "auth": "b"}}}, headers={"X-Mongoose-Device-Token": victim["device_token"]})
    AA.ensure_table()
    acct = AA.create_account("someuser", "pw123456")
    client.cookies.set(SA.SESSION_COOKIE, SA.issue_session(user_id=acct["user_id"]))
    r = client.post("/push/unsubscribe", json={"endpoint": "https://p/victim"})
    assert r.status_code == 403, "an ordinary logged-in account was treated as privileged"


def test_finding9_cached_weather_discloses_age(monkeypatch, tmp_path):
    import asyncio
    import json
    from datetime import datetime, timezone, timedelta
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "w.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    payload = {"updated_at": old, "sources": ["open_meteo"],
              "current": {"temperature_c": 20.0, "feels_like_c": 20.0, "condition": "clear"},
              "rain": {"next_expected": None, "confidence": "low", "minute_precision": False},
              "uv": {"peak": 5.0, "peak_time": "2026-07-04T14:00"}, "confidence": "medium",
              "voice": "20 degrees, clear."}
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO weather_consensus(location_key,payload_json,"
                   "updated_at,expires_at) VALUES(?,?,?,?)", ("home", json.dumps(payload), old, future))
    from mongoose.services.weather import weather_summary
    res = asyncio.run(weather_summary(force=False))
    assert res.get("served_from_cache") is True
    assert "minutes ago" in res["voice"]


def test_finding10_malformed_json_is_400_not_503(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "j.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.app import app
    from fastapi.testclient import TestClient
    client = TestClient(app, raise_server_exceptions=False, client=("192.168.1.57", 50000))
    r = client.post("/push/subscribe", data="{", headers={"content-type": "application/json"})
    assert r.status_code == 400


