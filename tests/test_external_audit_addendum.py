"""The ten additional findings from the second external audit round, each verified against
this tree with the audit's own exact reproduction before being fixed, and pinned here with
that same reproduction (or the closest FastAPI-TestClient equivalent)."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_finding1_remote_caller_cannot_mint_owner_slot_without_secret(monkeypatch, tmp_path):
    """First-ever remote /install/pair-code call, on a genuinely fresh install (no owner
    secret has ever been created), must be refused -- not just the second attempt onward."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.install_progress import begin_install, set_step
    begin_install()
    for k in ["prerequisites", "environment", "database", "server", "services",
             "models", "smoke", "watchdog", "ready"]:
        set_step(k, "done", "audit")
    from mongoose.app import app
    from fastapi.testclient import TestClient
    remote = TestClient(app, client=("192.168.1.55", 52344))
    r = remote.post("/install/pair-code")
    assert r.status_code == 401 and r.json().get("detail") == "owner_secret_required"
    # loopback (the real owner) is unaffected
    loopback = TestClient(app)
    r2 = loopback.post("/install/pair-code")
    assert r2.status_code == 200 and r2.json().get("profile") == "primary_phone"


def test_finding2_receipt_amount_must_match_the_actual_payment(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.services import rtm_management as M
    from mongoose.services import rtm_trust as T
    M.seed_flats()
    M.upsert_flat(block="1EC", flat_number=1, leaseholder_name="Alex Leaseholder",
                 correspondence_address="Flat 1, 1 Example Court", payment_reference="ALEX6F1")
    M.create_demand(document_number="AUD-REC-001", flat="1ECF1", period="Test",
                    amount="50.00", issue_date="2026-07-04", due_date="2026-08-01")
    csv_path = tmp_path / "metro.csv"
    csv_path.write_text("Date,Description,Reference,Credit\n2026-07-04,Payment,ALEX6F1,50.00\n")
    M.import_metro_csv(str(csv_path))
    bad = T.build_receipt("1ECF1", "AUD-REC-001", "ALEX6F1", 1, "2026-07-04")
    assert bad["ok"] is False and bad["reason"] == "amount_mismatch"
    good = T.build_receipt("1ECF1", "AUD-REC-001", "ALEX6F1", 5000, "2026-07-04")
    assert good["ok"] and good["amount"] == "£50.00"


def test_finding3_receipts_for_the_same_flat_do_not_overwrite_each_other(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.services import rtm_management as M
    from mongoose.services import rtm_trust as T
    M.seed_flats()
    M.upsert_flat(block="1EC", flat_number=1, leaseholder_name="Alex Leaseholder",
                 correspondence_address="Flat 1, 1 Example Court", payment_reference="ALEX6F1")
    M.create_demand(document_number="AUD-REC-A", flat="1ECF1", period="First",
                    amount="50.00", issue_date="2026-07-04", due_date="2026-08-01")
    M.create_demand(document_number="AUD-REC-B", flat="1ECF1", period="Second",
                    amount="60.00", issue_date="2026-08-04", due_date="2026-09-01")
    csv_path = tmp_path / "metro.csv"
    csv_path.write_text("Date,Description,Reference,Credit\n"
                        "2026-07-04,Payment 1,ALEX6F1,50.00\n2026-08-04,Payment 2,ALEX6F1,60.00\n")
    M.import_metro_csv(str(csv_path))
    r1 = T.build_receipt("1ECF1", "AUD-REC-A", "ALEX6F1", 5000, "2026-07-04")
    r2 = T.build_receipt("1ECF1", "AUD-REC-B", "ALEX6F1", 6000, "2026-08-04")
    assert r1["receipt_number"] != r2["receipt_number"]
    assert len(T.list_receipts("1ECF1")) == 2


def test_finding4_polish_crisis_content_suppresses_lounge_reflection(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    from mongoose.services import lounge_surpriser as LS
    LS.store_human_message(device_id="d1", sender="Ania", text="mam depresję i myśli samobójcze",
                           line="Ania: mam depresję i myśli samobójcze")
    LS.store_human_message(device_id="d2", sender="Bartek",
                           text="sąd i prawnik, dług i eksmisja mnie stresują",
                           line="Bartek: sąd i prawnik, dług i eksmisja mnie stresują")
    snap = LS.lounge_snapshot()
    assert snap["safe_to_reflect"] is False


def test_finding4_ordinary_polish_chat_is_not_over_suppressed(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    from mongoose.services import lounge_surpriser as LS
    LS.store_human_message(device_id="d3", sender="Kasia",
                           text="ale dzisiaj piękna pogoda, idziemy na spacer?",
                           line="Kasia: ale dzisiaj piękna pogoda, idziemy na spacer?")
    snap = LS.lounge_snapshot()
    assert snap["safe_to_reflect"] is True


def test_finding5_explicit_crisis_text_gets_a_deterministic_safety_response(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    from mongoose.services import guest_tasks as GT
    for msg in ("I want to kill myself", "I am having a panic attack and feel unsafe"):
        r = asyncio.run(GT.reflect("guest:abc", msg))
        assert r.get("crisis") is True
    for msg in ("I'm having a rough week at work", "this panic room escape game was fun"):
        r = asyncio.run(GT.reflect("guest:abc", msg))
        assert not r.get("crisis")


def test_finding6_high_sensitivity_health_context_always_needs_review(monkeypatch, tmp_path):
    import json as _json
    import zipfile
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "metadata.yaml").write_text(
        'dump_version: "audit"\nthread_title: "Health audit import"\n'
        'thread_date_range: "2026"\nextracted_at: "2026-07-04"\n'
        'primary_domains: ["health"]\nsensitivity_level: "high"\n'
        'overall_confidence: "high"\nsource_quality_notes: ["synthetic audit package"]\n')
    rec = {"record_type": "health_context", "topic": "elevated WBC",
           "summary": "User has had a mildly raised blood marker over several years.",
           "details": {"marker": "WBC", "values": [1, 2, 3]}, "source_basis": "user_stated",
           "confidence": 0.95, "sensitivity": "high", "time_context": "2023-2026",
           "use_for_mongoose": "Keep as sensitive health context only.",
           "do_not_overgeneralise": True, "retrieval_tags": ["health", "blood tests", "WBC"],
           "memory_importance": "high", "acceptance_state": "user_stated"}
    (pkg / "structured_records.jsonl").write_text(_json.dumps(rec) + "\n")
    zpath = tmp_path / "health_dump.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(pkg / "metadata.yaml", "metadata.yaml")
        z.write(pkg / "structured_records.jsonl", "structured_records.jsonl")
    from mongoose.knowledge_import.service import import_bulk_dump
    from mongoose.db import connect
    import_bulk_dump(str(zpath))
    with connect() as con:
        reviews = con.execute("SELECT * FROM knowledge_review_queue").fetchall()
    assert len(reviews) > 0


def test_finding7_guest_can_unsubscribe_own_but_not_others(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.devices import create_pairing_code, claim_pairing_code
    from mongoose.app import app
    from fastapi.testclient import TestClient
    code = create_pairing_code("trusted_guest")["code"]
    claim = claim_pairing_code(code, "Guest Phone", "guest_web")
    tok = claim["device_token"]
    client = TestClient(app, client=("192.168.1.56", 50000))
    body = {"subscription": {"endpoint": "https://push.example/sub/a",
                             "keys": {"p256dh": "abc", "auth": "def"}}, "device_label": "guest phone"}
    client.post("/push/subscribe", json=body, headers={"X-Mongoose-Device-Token": tok})
    r = client.post("/push/unsubscribe", json={"endpoint": "https://push.example/sub/a"},
                    headers={"X-Mongoose-Device-Token": tok})
    assert r.status_code == 200 and r.json()["ok"]

    code2 = create_pairing_code("trusted_guest")["code"]
    claim2 = claim_pairing_code(code2, "Second Guest Phone", "guest_web")
    tok2 = claim2["device_token"]
    client.post("/push/subscribe",
               json={"subscription": {"endpoint": "https://push.example/sub/b",
                                      "keys": {"p256dh": "x", "auth": "y"}}},
               headers={"X-Mongoose-Device-Token": tok2})
    r2 = client.post("/push/unsubscribe", json={"endpoint": "https://push.example/sub/b"},
                     headers={"X-Mongoose-Device-Token": tok})
    assert r2.status_code == 403


def test_finding8_whatsapp_unconfigured_gives_clean_message_everywhere(monkeypatch, tmp_path):
    monkeypatch.delenv("WAHA_URL", raising=False)
    monkeypatch.delenv("WHATSAPP_WAHA_URL", raising=False)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.services.whatsapp_journey import login_qr
    from mongoose.services.whatsapp import draft_message, approve_and_send
    qr = login_qr()
    assert "Connection refused" not in qr["error"] and "mongoose whatsapp setup" not in qr["error"]
    d = draft_message(contact="Sam", body="hello audit", chat_id="12345@c.us")
    approve = approve_and_send(d["id"])
    assert "Connection refused" not in approve["error"] and "mongoose whatsapp setup" not in approve["error"]


def test_finding9_cached_weather_discloses_its_age(monkeypatch, tmp_path):
    import asyncio
    import json as _json
    from datetime import datetime, timezone, timedelta
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    from mongoose.db import init_db, connect
    init_db()
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    payload = {"updated_at": old, "sources": ["open_meteo"],
              "current": {"temperature_c": 20.4}, "voice": "20 degrees, probably dry."}
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO weather_consensus(location_key,payload_json,updated_at,expires_at) "
                   "VALUES(?,?,?,?)", ("home", _json.dumps(payload), old, future))
    from mongoose.services.weather import weather_summary
    res = asyncio.run(weather_summary(force=False))
    assert res["served_from_cache"] is True and res["cache_age_minutes"] == 45
    assert "about 45 minutes ago" in res["voice"]


def test_finding10_malformed_json_is_400_not_503(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    from mongoose.app import app
    from fastapi.testclient import TestClient
    client = TestClient(app, raise_server_exceptions=False, client=("192.168.1.57", 50000))
    for path in ("/push/subscribe", "/push/unsubscribe"):
        r = client.post(path, data="{", headers={"content-type": "application/json"})
        assert r.status_code == 400 and r.json()["error"] == "invalid_json"
