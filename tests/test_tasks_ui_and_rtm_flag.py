"""Tasks need a real UI contract: deadlines, priority, and an RTM label the owner controls.

Before this there was no tasks screen at all: 77 open tasks, 75 with no deadline, none classified,
and the due_at/priority columns existed but nothing ever wrote them. The Microsoft To Do integration
is deliberately ONE WAY (Tasks.Read, no write path), so RTM work can never travel back out.
"""
import pytest

from mongoose.services import calendar_tasks as CT


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.db import init_db
    init_db()
    return True


def _add(title, **kw):
    return CT.add_task(CT.TaskIn(source="mongoose", list_name="Tasks", title=title,
                                 status="open", **kw))["id"]


def test_owner_can_set_and_clear_a_deadline(store):
    tid = _add("book FRA")
    CT.update_task(tid, due_at="2026-08-01")
    assert CT.list_tasks(limit=5)[0]["due_at"] == "2026-08-01"
    CT.update_task(tid, clear=("due_at",))
    assert CT.list_tasks(limit=5)[0]["due_at"] is None, "clearing must be expressible, not just setting"


def test_priority_is_validated_not_free_text(store):
    tid = _add("something")
    assert CT.update_task(tid, priority="high")["ok"] is True
    bad = CT.update_task(tid, priority="URGENT!!")
    assert bad["ok"] is False, "an unknown priority must be refused, not silently stored"


def test_rtm_flag_has_three_states_including_unclassified(store):
    a, b, c = _add("rtm arrears chase"), _add("book pianist"), _add("nin")
    CT.update_task(a, is_rtm=True)
    CT.update_task(b, is_rtm=False)
    counts = CT.task_counts()
    assert counts["rtm"] == 1 and counts["personal"] == 1 and counts["unclassified"] == 1, \
        "an untouched task must read as UNCLASSIFIED, never silently personal"
    assert [t["id"] for t in CT.list_tasks(scope="rtm")] == [a]
    assert [t["id"] for t in CT.list_tasks(scope="personal")] == [b]
    assert [t["id"] for t in CT.list_tasks(scope="unclassified")] == [c]


def test_rtm_guess_is_advisory_and_reasonable(store):
    assert CT.guess_is_rtm("remind leaseholders to inspect their balconies") is True
    assert CT.guess_is_rtm("chase service charge arrears") is True
    assert CT.guess_is_rtm("book pianist") is False
    assert CT.guess_is_rtm("") is False


def test_completing_a_task_removes_it_from_the_open_list(store):
    tid = _add("done soon")
    CT.update_task(tid, status="completed")
    assert CT.list_tasks(limit=10) == []
    assert CT.task_counts()["total"] == 0


def test_dated_tasks_sort_before_undated(store):
    _add("no date")
    tid = _add("has date")
    CT.update_task(tid, due_at="2026-08-01")
    assert CT.list_tasks(limit=10)[0]["id"] == tid, "a task with a deadline must lead the list"


def test_microsoft_sync_stays_read_only():
    """The boundary the owner asked for is enforced by the OAuth scope, not by policy."""
    import inspect
    from mongoose.connectors import microsoft_graph as MG
    assert "Tasks.Read" in MG.SCOPES and not any("ReadWrite" in s for s in MG.SCOPES)
    src = inspect.getsource(MG)
    assert ".post(" not in src.split("def sync_microsoft_todo")[1][:4000], \
        "sync_microsoft_todo must never write back to Microsoft"


def test_static_task_routes_are_declared_before_the_parameterised_one():
    """Route-ordering guard.

    FastAPI matches in declaration order, so POST /cockpit/tasks/{task_id} declared FIRST swallowed
    /cockpit/tasks/classify-suggest and returned 422 ("classify-suggest is not an integer"). The unit
    tests passed because they exercised the service layer, not the routes; only a live call caught it.
    """
    import pathlib
    from mongoose import cockpit
    src = pathlib.Path(cockpit.__file__).with_name("app.py").read_text(encoding="utf-8")
    static_at = src.index('@app.post("/cockpit/tasks/classify-suggest")')
    param_at = src.index('@app.post("/cockpit/tasks/{task_id}")')
    assert static_at < param_at, (
        "a static sub-path must be declared BEFORE /cockpit/tasks/{task_id}, or the parameterised "
        "route captures it and the endpoint 422s")
