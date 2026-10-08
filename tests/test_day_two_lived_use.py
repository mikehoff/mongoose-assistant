"""Day-two lived-use regressions.

These are the bugs users tend to discover only after real use: an update runs against an old lazy
schema, a phone says it accepted data but search never sees it, yesterday's file still appears after
it was deleted, or a reminder disappears even though no notification was delivered.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def _sub(endpoint: str) -> dict:
    return {"endpoint": endpoint, "keys": {"p256dh": "abc", "auth": "def"}}


def test_web_push_migrates_old_subscription_schema_and_filters_profiles():
    from mongoose.db import connect
    from mongoose.services.web_push import save_subscription, list_subscriptions, subscription_count

    # Simulate a DB created before account/profile-scoped subscriptions existed. This is the exact
    # update path a day-two user hits; a fresh test DB would not catch it.
    with connect() as con:
        con.execute(
            "CREATE TABLE push_subscriptions ("
            "sub_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL UNIQUE, p256dh TEXT NOT NULL, "
            "auth TEXT NOT NULL, created_at TEXT NOT NULL)"
        )

    assert save_subscription(
        _sub("https://push.example/owner"),
        user_id="owner-account",
        device_id="dev-owner",
        device_profile="primary_phone",
        device_label="Alex phone",
    )["ok"]
    assert save_subscription(
        _sub("https://push.example/guest"),
        device_id="dev-guest",
        device_profile="trusted_guest",
        device_label="Guest phone",
    )["ok"]

    assert subscription_count(user_id="owner-account") == 1
    owner_scoped = list_subscriptions(device_profiles=("primary_phone", "home_tablet"), include_unscoped=False)
    assert [s["endpoint"] for s in owner_scoped] == ["https://push.example/owner"]
    assert subscription_count(device_profiles=("primary_phone",), include_unscoped=False) == 1


def test_device_notification_relay_reaches_search_index(monkeypatch):
    from mongoose import devices as D
    from mongoose.app import app
    from mongoose.services.notifications import search_notifications

    monkeypatch.setattr(D, "device_for_token", lambda _t: {
        "device_id": "phone1", "name": "Alex iPhone", "profile": "primary_phone"
    })
    c = TestClient(app, raise_server_exceptions=False)

    r = c.post(
        "/device/notifications/ingest",
        json={"title": "WhatsApp", "text": "New message from Jo about Thursday", "app": "WhatsApp"},
        headers={"X-Mongoose-Device-Token": "tok"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["notification_inbox"]["inserted"] is True

    hits = search_notifications("Jo Thursday")
    assert hits and hits[0]["title"] == "WhatsApp"
    assert hits[0]["device"] == "Alex iPhone"


def test_file_reindex_prunes_deleted_local_files(tmp_path):
    from mongoose.services.file_index import index_files, search_files

    root = tmp_path / "indexed"
    root.mkdir()
    f = root / "day-two-note.txt"
    f.write_text("unique stale file phrase", encoding="utf-8")
    first = index_files(str(root), limit=100)
    assert first["indexed"] == 1
    assert search_files("unique stale")

    f.unlink()
    second = index_files(str(root), limit=100)
    assert second["completed"] is True
    assert second["pruned"] == 1
    assert search_files("unique stale") == []


def test_device_file_delete_tombstone_removes_stale_search_result():
    from mongoose.services.file_index import ingest_device_files, search_files

    ingest_device_files([
        {"path": "/sdcard/Download/old.txt", "filename": "old.txt", "text": "phone stale tombstone phrase", "device_id": "phoneA"}
    ], device="phoneA")
    assert search_files("tombstone phrase")

    res = ingest_device_files([
        {"path": "/sdcard/Download/old.txt", "device_id": "phoneA", "deleted": True}
    ], device="phoneA")
    assert res["pruned"] == 1
    assert search_files("tombstone phrase") == []


def test_alert_not_marked_fired_when_no_delivery(monkeypatch):
    from mongoose.services import notifications as N
    from mongoose.services import time_alerts as TA
    from mongoose.services.time_alerts_fire import fire_due_alerts

    seen = {}

    async def fake_send_to_phone(*args, **kwargs):
        seen["kwargs"] = kwargs
        return {"sent": False, "reason": "no_profile_subscription"}

    monkeypatch.setattr(N, "send_to_phone", fake_send_to_phone)
    alert = TA.create_alert(
        kind="reminder",
        fire_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        label="drink water",
    )

    fired = asyncio.run(fire_due_alerts())
    assert fired["fired_count"] == 1
    assert fired["fired"][0]["status"] == "failed"
    assert seen["kwargs"]["target_device_profiles"] == ("primary_phone", "home_tablet")
    row = TA.get_alert(alert["id"])
    assert row["status"] == TA.FAILED
    assert "no_profile_subscription" in (row.get("detail") or "")


def test_free_memory_pause_auto_resumes_when_window_expires():
    from mongoose.db import get_app_state, set_app_state
    from mongoose.services import scheduler as S

    res = S.scheduler_control("free_memory", reason="owner asked yesterday")
    assert res["ok"] is True
    disabled = json.loads(get_app_state("scheduler_disabled_jobs_v1", "{}") or "{}")
    assert disabled["benchmark"].get("temporary") is True
    assert disabled["benchmark"].get("auto_resume_at")

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    for meta in disabled.values():
        meta["auto_resume_at"] = past
    set_app_state("scheduler_disabled_jobs_v1", json.dumps(disabled))

    assert S.disabled_jobs() == {}
    rows = {r["job"]: r for r in S.job_status()}
    assert rows["benchmark"]["enabled"] is True
