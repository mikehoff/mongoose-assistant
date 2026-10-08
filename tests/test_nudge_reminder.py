"""The 'nudge next week' hallucination fix: a bare nudge must REALLY set a reminder, not just say so."""
from datetime import datetime, timezone


def test_parse_when_handles_offsets_without_clock():
    from mongoose.services.time_parse import parse_when
    now = datetime(2026, 7, 17, 12, 40, tzinfo=timezone.utc)
    assert parse_when("next week", now=now)["fire_at"].date().isoformat() == "2026-07-24"
    assert parse_when("nudge next week", now=now)["fire_at"].date().isoformat() == "2026-07-24"
    assert parse_when("in 3 days", now=now)["fire_at"].date().isoformat() == "2026-07-20"
    assert parse_when("tomorrow", now=now)["fire_at"].date().isoformat() == "2026-07-18"
    # a bare clock still works (no regression)
    assert parse_when("at 7am", now=now)["fire_at"].hour == 7


def test_bare_nudge_sets_reminder_on_recent_task():
    from mongoose.services.chat_actions import capture_task, attach_nudge_to_recent_task
    from mongoose.services.calendar_tasks import most_recent_open_task
    capture_task("add task: confirmation statement by 31 july")
    assert most_recent_open_task()["reminder_at"] is None
    reply = attach_nudge_to_recent_task("nudge next week")
    assert reply is not None
    t = most_recent_open_task()
    assert t["reminder_at"], "the reminder must actually be persisted, not just promised"


def test_labelled_reminder_is_not_treated_as_bare_nudge():
    from mongoose.services.chat_actions import capture_task, attach_nudge_to_recent_task
    capture_task("add task: something")
    # A reminder that carries its OWN subject must fall through to the normal reminder path.
    assert attach_nudge_to_recent_task("remind me to call Bob next week") is None


def test_nudge_routes_as_a_time_alert():
    from mongoose.role_router import looks_like_time_alert
    assert looks_like_time_alert("nudge next week") is True
    assert looks_like_time_alert("nudge me tomorrow") is True
