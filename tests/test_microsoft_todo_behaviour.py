"""Microsoft To Do task behaviour , the lived-use properties, not just unit correctness.

These tests pin behaviours that were silently wrong even though individual functions "worked":
- ordering: dated tasks chronological, undated tasks stable at the end (not interleaved by last_seen);
- reconciliation: a task deleted in the To Do app stops appearing locally (no ghost);
- completion: a task completed in the app reflects as completed locally (not stuck open);
- SAFETY: a transient/partial sync failure never reaps real tasks (no accidental clearing);
- overdue: an overdue task still surfaces in the today view (it is the most urgent thing).

DB isolation is automatic via conftest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _add(tid, title, *, due=None, reminder=None, status="open", list_name="Tasks"):
    from mongoose.services.calendar_tasks import add_task, TaskIn
    return add_task(TaskIn(source="microsoft_todo", source_task_id=tid, list_name=list_name,
                           title=title, due_at=due, reminder_at=reminder, status=status))


# ── ordering ────────────────────────────────────────────────────────────────────────────────────
def test_dated_tasks_sort_chronologically_and_undated_sort_last():
    from mongoose.services.calendar_tasks import list_tasks
    _add("a", "Due next month", due="2026-08-01T09:00:00")
    _add("b", "Due tomorrow", due="2026-07-01T09:00:00")
    _add("c", "Undated zebra")
    _add("d", "Reminder today", reminder="2026-06-30T18:00:00")
    _add("e", "Undated apple")
    titles = [t["title"] for t in list_tasks()]
    # dated tasks first, in date order
    assert titles.index("Reminder today") < titles.index("Due tomorrow") < titles.index("Due next month")
    # undated tasks come after all dated tasks
    assert titles.index("Due next month") < titles.index("Undated apple")
    assert titles.index("Due next month") < titles.index("Undated zebra")
    # undated tasks are stably ordered (alphabetical), not by last_seen
    assert titles.index("Undated apple") < titles.index("Undated zebra")


def test_undated_task_does_not_interleave_between_future_dated_tasks():
    """The old COALESCE(..., last_seen) ordering let an undated task sort between future-dated tasks
    because last_seen looked like a near-future timestamp. It must not."""
    from mongoose.services.calendar_tasks import list_tasks
    _add("future1", "Due in a week", due="2026-07-07T09:00:00")
    _add("undated", "No date")
    _add("future2", "Due in two weeks", due="2026-07-14T09:00:00")
    titles = [t["title"] for t in list_tasks()]
    # the two dated tasks must be adjacent in the ordering (undated cannot split them)
    assert abs(titles.index("Due in a week") - titles.index("Due in two weeks")) == 1


# ── reconciliation: deleted-in-app ────────────────────────────────────────────────────────────────
def test_task_deleted_in_app_is_reconciled_away():
    from mongoose.services.calendar_tasks import list_tasks, reconcile_todo_tasks
    _add("keep", "Buy milk", list_name="Groceries")
    _add("gone", "Buy eggs", list_name="Groceries")
    assert {t["title"] for t in list_tasks()} == {"Buy milk", "Buy eggs"}
    # next sync sees only 'keep'; 'gone' was deleted in the app. Groceries synced OK.
    res = reconcile_todo_tasks(seen_task_ids={"keep"}, seen_list_names={"Groceries"})
    assert res["resolved"] == 1
    assert {t["title"] for t in list_tasks()} == {"Buy milk"}


# ── SAFETY: partial failure must not reap ─────────────────────────────────────────────────────────
def test_transient_sync_failure_never_reaps_tasks():
    """If no list synced successfully (transient Graph error), reconciliation must resolve nothing ,
    otherwise a blip would wipe the real task list (accidental clearing)."""
    from mongoose.services.calendar_tasks import list_tasks, reconcile_todo_tasks
    _add("t1", "Real task one", list_name="Work")
    _add("t2", "Real task two", list_name="Work")
    # nothing seen, no lists synced
    res = reconcile_todo_tasks(seen_task_ids=set(), seen_list_names=set())
    assert res["resolved"] == 0
    assert {t["title"] for t in list_tasks()} == {"Real task one", "Real task two"}


def test_task_in_unsynced_list_is_not_reaped():
    """A task in a list that did not sync this run must be left alone, even if other lists synced."""
    from mongoose.services.calendar_tasks import list_tasks, reconcile_todo_tasks
    _add("g1", "Groceries task", list_name="Groceries")
    _add("w1", "Work task", list_name="Work")
    # only Groceries synced this run, and it returned nothing
    res = reconcile_todo_tasks(seen_task_ids=set(), seen_list_names={"Groceries"})
    titles = {t["title"] for t in list_tasks()}
    # the Work task survives (its list was not synced), Groceries task is reaped
    assert "Work task" in titles
    assert "Groceries task" not in titles
    assert res["resolved"] == 1


# ── overdue surfacing ─────────────────────────────────────────────────────────────────────────────
def test_overdue_tasks_surface_in_today_view_with_flag():
    from mongoose.services.calendar_tasks import tasks_today
    now = datetime.now(timezone.utc)
    _add("od", "Overdue task", due=(now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"))
    _add("td", "Today task", due=now.strftime("%Y-%m-%dT%H:%M:%S"))
    titles = [t["title"] for t in tasks_today()]
    assert "Overdue task" in titles, "overdue tasks must not vanish from the today view"
    overdue_rows = [t for t in tasks_today() if t.get("overdue")]
    assert any(t["title"] == "Overdue task" for t in overdue_rows)


def test_completed_status_excludes_task_from_open_views():
    from mongoose.services.calendar_tasks import list_tasks
    _add("done", "Finished thing", status="completed")
    _add("open", "Pending thing", status="open")
    titles = [t["title"] for t in list_tasks()]
    assert "Pending thing" in titles
    assert "Finished thing" not in titles
