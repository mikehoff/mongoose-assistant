"""Tests for Time Alerts: alarms/timers/reminders with a Mongoose-owned ledger, honest
delivery (never claim it reached you when it didn't), and the safety rules (cancel-all
confirms). The whole point is avoiding the 'said alarm set but nothing happened' failure."""
import os
import tempfile
from datetime import datetime, timezone, timedelta


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


# --- parsing ----------------------------------------------------------------

def test_parse_clock_tomorrow():
    from mongoose.services.time_parse import parse_when
    now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    r = parse_when("wake me at 7 tomorrow", now=now)
    assert r["kind_hint"] == "alarm"
    assert r["fire_at"].hour == 7 and r["fire_at"].day == 20


def test_parse_duration_is_timer():
    from mongoose.services.time_parse import parse_when
    now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    r = parse_when("set a 20 minute timer", now=now)
    assert r["kind_hint"] == "timer"
    assert r["duration_seconds"] == 1200


def test_parse_weekday_recurrence():
    from mongoose.services.time_parse import parse_when
    now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    r = parse_when("alarm at 6:45 on weekdays", now=now)
    assert r["recurrence"] == "weekdays"


def test_parse_unparseable_returns_none():
    from mongoose.services.time_parse import parse_when
    assert parse_when("do something nice", now=datetime.now(timezone.utc)) is None


def test_past_time_rolls_to_tomorrow():
    from mongoose.services.time_parse import parse_when
    now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    r = parse_when("wake me at 7am", now=now)   # 7am already passed today
    assert r["fire_at"].day == 20


# --- ledger -----------------------------------------------------------------

def test_create_and_list_and_cancel():
    _fresh()
    from mongoose.services import time_alerts as TA
    now = datetime.now(timezone.utc)
    a = TA.create_alert(kind="alarm", fire_at=now + timedelta(hours=8), label="wake")
    assert len(TA.list_alerts()) == 1
    TA.cancel_alert(a["id"])
    assert len(TA.list_alerts()) == 0


def test_due_alerts_respects_time():
    _fresh()
    from mongoose.services import time_alerts as TA
    now = datetime.now(timezone.utc)
    TA.create_alert(kind="alarm", fire_at=now + timedelta(hours=2), label="later")
    assert len(TA.due_alerts(now=now)) == 0
    assert len(TA.due_alerts(now=now + timedelta(hours=3))) == 1


def test_recurring_chains_forward_and_skips_weekend():
    _fresh()
    from mongoose.services import time_alerts as TA
    fri = datetime(2026, 6, 19, 6, 45, tzinfo=timezone.utc)   # Friday
    a = TA.create_alert(kind="alarm", fire_at=fri, label="wake", recurrence="weekdays")
    nxt = TA.reschedule_recurring(a, now=fri)
    assert nxt["fire_at"][:10] == "2026-06-22"   # Monday


# --- honest delivery (the core promise) -------------------------------------

def test_firing_does_not_fake_delivery_when_push_unconfigured():
    _fresh()
    import asyncio
    from mongoose.services import time_alerts as TA
    from mongoose.services.time_alerts_fire import fire_due_alerts
    now = datetime.now(timezone.utc)
    TA.create_alert(kind="alarm", fire_at=now - timedelta(seconds=1), label="wake")
    res = asyncio.run(fire_due_alerts(now=now))
    # ntfy not configured in tests -> delivered must be empty, NOT faked
    assert res["fired_count"] == 1
    assert res["fired"][0]["delivered"] == []


def test_firing_reports_push_when_configured(monkeypatch):
    _fresh()
    import asyncio
    import mongoose.services.notifications as N

    async def fake_push(title, body="", **kw):
        return {"sent": True}
    monkeypatch.setattr(N, "send_to_phone", fake_push)
    from mongoose.services import time_alerts as TA
    from mongoose.services.time_alerts_fire import fire_due_alerts
    now = datetime.now(timezone.utc)
    TA.create_alert(kind="timer", fire_at=now - timedelta(seconds=1), label="pasta", duration_seconds=600)
    res = asyncio.run(fire_due_alerts(now=now))
    assert "push" in res["fired"][0]["delivered"]


def test_fired_alert_leaves_active_list():
    _fresh()
    import asyncio
    from mongoose.services import time_alerts as TA
    from mongoose.services.time_alerts_fire import fire_due_alerts
    now = datetime.now(timezone.utc)
    TA.create_alert(kind="alarm", fire_at=now - timedelta(seconds=1), label="wake")
    asyncio.run(fire_due_alerts(now=now))
    assert len(TA.list_alerts(active_only=True)) == 0


def test_announce_off_by_default():
    # speaker announce must be off unless explicitly enabled (it widens house control)
    from mongoose.services.time_alerts_fire import _announce_enabled
    os.environ.pop("MONGOOSE_ALERT_ANNOUNCE", None)
    assert _announce_enabled() is False


# --- orchestration + safety -------------------------------------------------

def test_set_from_text_records_ledger_with_say():
    _fresh()
    from mongoose.services.time_alerts import set_from_text
    now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    r = set_from_text("wake me at 7am tomorrow", now=now)
    assert r["ok"] and r["alert"]["kind"] == "alarm"
    assert "phone" in r["say"].lower()


def test_reminder_extracts_label():
    _fresh()
    from mongoose.services.time_alerts import set_from_text
    now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    r = set_from_text("remind me to call mum at 3pm", kind="reminder", now=now)
    assert r["alert"]["label"] == "call mum"


def test_unparseable_set_is_honest():
    _fresh()
    from mongoose.services.time_alerts import set_from_text
    r = set_from_text("do something", now=datetime.now(timezone.utc))
    assert r["ok"] is False
    assert r["alert"] is None


# --- intent routing ---------------------------------------------------------

def test_time_alert_intents():
    from mongoose.role_router import looks_like_time_alert as la, time_alert_action as act
    assert la("set an alarm for 7am") and act("set an alarm for 7am") == "set"
    assert la("cancel all timers") and act("cancel all timers") == "cancel"
    assert la("what alarms do I have?") and act("what alarms do I have?") == "list"


def test_time_alert_ignores_ordinary():
    from mongoose.role_router import looks_like_time_alert as la
    assert not la("turn off the lights")
    assert not la("what is the weather")
    assert not la("are you ready?")
