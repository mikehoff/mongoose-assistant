from mongoose.services.brief import cross_signals, _fallback_brief, _parse_iso


def _picture(**kw):
    base = {"events": [], "tasks": [], "weather": None, "bus_unread": []}
    base.update(kw)
    return base


def test_detects_calendar_conflict():
    p = _picture(events=[
        {"title": "A", "start_at": "2026-06-14T14:00:00+00:00", "end_at": "2026-06-14T15:00:00+00:00"},
        {"title": "B", "start_at": "2026-06-14T14:30:00+00:00", "end_at": "2026-06-14T15:30:00+00:00"},
    ])
    sigs = cross_signals(p)
    assert any("CONFLICT" in s for s in sigs)


def test_no_false_conflict_for_back_to_back():
    p = _picture(events=[
        {"title": "A", "start_at": "2026-06-14T14:00:00+00:00", "end_at": "2026-06-14T15:00:00+00:00"},
        {"title": "B", "start_at": "2026-06-14T15:00:00+00:00", "end_at": "2026-06-14T16:00:00+00:00"},
    ])
    assert not any("CONFLICT" in s for s in cross_signals(p))


def test_conflict_across_timezone_offsets():
    # 15:30+01:00 == 14:30Z, overlaps 14:00-15:00Z
    p = _picture(events=[
        {"title": "A", "start_at": "2026-06-14T14:00:00+00:00", "end_at": "2026-06-14T15:00:00+00:00"},
        {"title": "B", "start_at": "2026-06-14T15:30:00+01:00", "end_at": "2026-06-14T16:30:00+01:00"},
    ])
    assert any("CONFLICT" in s for s in cross_signals(p))


def test_overdue_and_due_today(monkeypatch):
    import mongoose.services.brief as b
    from datetime import datetime, timezone
    fixed = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(b, "datetime", FixedDT)
    p = _picture(tasks=[
        {"title": "late", "due_at": "2026-06-13T10:00:00+00:00"},
        {"title": "today", "due_at": "2026-06-14T20:00:00+00:00"},
    ])
    sigs = cross_signals(p)
    assert any("overdue" in s for s in sigs)
    assert any("due today" in s for s in sigs)


def test_clear_day_has_no_signals():
    assert cross_signals(_picture()) == []


def test_fallback_brief():
    # Empty signals: conveys a clear day, invents nothing.
    assert "clear" in _fallback_brief([]).lower()
    # With a signal: the deterministic brief restates the signal content (cannot inflate it).
    assert "1 overdue" in _fallback_brief(["TASKS: 1 overdue."])


def test_parse_iso_handles_garbage():
    assert _parse_iso("") is None
    assert _parse_iso("not a date") is None
    assert _parse_iso("2026-06-14T10:00:00Z") is not None


def test_brief_excludes_self_sent_messages(tmp_path, monkeypatch):
    # The brief must not surface its own/builder's past briefs as "unread",
    # which would be a self-reference loop growing daily.
    import asyncio
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    from mongoose.db import init_db
    init_db(p)
    from mongoose.services import bus, brief
    from mongoose.settings import get_settings
    owner = get_settings().owner_id
    # a real message from a real agent (should appear)
    bus.send_message(sender="claude_code", to=owner, subject="real task", type="task")
    # self-sent noise (should be filtered)
    bus.send_message(sender="mongoose", to=owner, subject="yesterday's brief", type="brief")
    bus.send_message(sender="builder", to=owner, subject="build done", type="brief")
    picture = asyncio.run(brief.gather())
    senders = {m["sender"] for m in picture["bus_unread"]}
    assert "claude_code" in senders
    assert "mongoose" not in senders
    assert "builder" not in senders


def test_brief_weather_fetch_is_bounded(monkeypatch, tmp_path):
    """The brief/DOP must not hang the resident surfaces on a slow live weather provider."""
    import asyncio
    import mongoose.services.brief as b
    async def slow_weather_summary():
        await asyncio.sleep(0.2)
        return {"summary": "too slow"}
    monkeypatch.setenv("MONGOOSE_BRIEF_WEATHER_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr("mongoose.services.weather.weather_summary", slow_weather_summary)
    picture = asyncio.run(b.gather())
    assert picture["weather"] is None
