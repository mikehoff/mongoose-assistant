
from mongoose.db import init_db
from mongoose.services.calendar_tasks import EventIn, add_event, availability


def test_availability_overlap(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    init_db(db)
    add_event(EventIn(title="Busy", start_at="2026-06-13T10:00:00+01:00", end_at="2026-06-13T11:00:00+01:00"))
    result = availability("2026-06-13T10:30:00+01:00", "2026-06-13T10:45:00+01:00")
    assert result["is_free"] is False
    assert len(result["busy"]) == 1
