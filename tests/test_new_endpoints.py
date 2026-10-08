from datetime import datetime
from zoneinfo import ZoneInfo

from mongoose.db import init_db
from mongoose.services.calendar_tasks import EventIn, TaskIn, add_event, add_task, events_today, tasks_today
from mongoose.services.home_router import list_route_preferences


def _today_iso(hour: int, minute: int = 0) -> str:
    tz = ZoneInfo("Europe/London")
    now = datetime.now(tz)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def test_events_today_includes_todays_event(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    monkeypatch.setenv("MONGOOSE_TIMEZONE", "Europe/London")
    init_db(db)
    add_event(EventIn(title="Dentist", start_at=_today_iso(10), end_at=_today_iso(11)))
    add_event(EventIn(title="Old", start_at="2020-01-01T10:00:00+00:00", end_at="2020-01-01T11:00:00+00:00"))
    today = events_today()
    titles = [e["title"] for e in today]
    assert "Dentist" in titles
    assert "Old" not in titles


def test_private_event_title_hidden_in_today(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    monkeypatch.setenv("MONGOOSE_TIMEZONE", "Europe/London")
    init_db(db)
    add_event(EventIn(title="Therapy session", start_at=_today_iso(14), end_at=_today_iso(15), detail_available=False))
    today = events_today()
    assert today and today[0]["title"] == "Busy"


def test_tasks_today_due_filter(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    monkeypatch.setenv("MONGOOSE_TIMEZONE", "Europe/London")
    init_db(db)
    add_task(TaskIn(title="Due today", due_at=_today_iso(17)))
    add_task(TaskIn(title="No due date"))
    assert [t["title"] for t in tasks_today()] == ["Due today"]
    with_open = [t["title"] for t in tasks_today(include_open_without_due=True)]
    assert "No due date" in with_open


def test_route_preferences_empty_initially(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    init_db(db)
    assert list_route_preferences() == []
