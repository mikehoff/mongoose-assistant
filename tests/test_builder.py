import subprocess
from pathlib import Path
import pytest
from mongoose.db import init_db
import mongoose.services.builder as B


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def _mock_local(good_ids):
    def fake(task, repo):
        for sid in good_ids:
            if sid in task:
                (Path(repo) / f"{sid}.txt").write_text("ok")
        return True, "local mock"
    return fake


def _mock_code(ok=True, rate_limited=False, writes=None):
    def fake(task, repo):
        if writes:
            for name in writes:
                (Path(repo) / name).write_text("ok")
        return ok, "code mock", rate_limited
    return fake


def test_requires_approval(db, monkeypatch):
    monkeypatch.setattr(B, "_invoke_local_draft", _mock_local(["a"]))
    monkeypatch.setattr(B, "_invoke_claude_code", _mock_code())
    plan = B.create_plan("t", [{"id": "a", "title": "a", "task": "a", "check": "test -f a.txt"}])
    r = B.run_plan(plan["plan_id"], post_to_bus=False)
    assert r["error"] == "plan not approved"


def test_local_draft_passes_and_commits(db, monkeypatch):
    # local draft alone satisfies the check; Code is still called to review but
    # the step passes regardless. Verify commit happens.
    monkeypatch.setattr(B, "_invoke_local_draft", _mock_local(["a"]))
    monkeypatch.setattr(B, "_invoke_claude_code", _mock_code())
    plan = B.create_plan("t", [{"id": "a", "title": "mk a", "task": "a", "check": "test -f a.txt"}])
    B.approve_plan(plan["plan_id"])
    out = B.run_plan(plan["plan_id"], post_to_bus=False)
    assert out["done"] == 1 and not out["blocked"]
    repo = Path(B._plan_dir(plan["plan_id"])) / "repo"
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout
    assert "mk a" in log


def test_code_review_fixes_failed_local_draft(db, monkeypatch):
    # local draft does NOT satisfy the check; Code writes the needed file.
    monkeypatch.setattr(B, "_invoke_local_draft", lambda t, r: (True, "partial"))
    monkeypatch.setattr(B, "_invoke_claude_code", _mock_code(writes=["done.txt"]))
    plan = B.create_plan("t", [{"id": "a", "title": "a", "task": "a", "check": "test -f done.txt"}])
    B.approve_plan(plan["plan_id"])
    out = B.run_plan(plan["plan_id"], post_to_bus=False)
    assert out["done"] == 1 and out["code_calls"] == 1


def test_code_budget_cap(db, monkeypatch):
    monkeypatch.setattr(B, "_invoke_local_draft", lambda t, r: (True, "partial"))
    monkeypatch.setattr(B, "_invoke_claude_code", _mock_code(writes=["done.txt"]))
    plan = B.create_plan("t", [
        {"id": "01", "title": "s1", "task": "t1", "check": "test -f x01.txt", "retries": 0},
        {"id": "02", "title": "s2", "task": "t2", "check": "test -f x02.txt", "retries": 0},
    ])
    B.approve_plan(plan["plan_id"])
    out = B.run_plan(plan["plan_id"], post_to_bus=False, code_call_budget=1)
    assert out["code_calls"] == 1  # cap held: Code called once only


def test_rate_limit_backoff(db, monkeypatch):
    monkeypatch.setattr(B, "_invoke_local_draft", _mock_local(["01", "02", "03"]))
    monkeypatch.setattr(B, "_invoke_claude_code", _mock_code(ok=False, rate_limited=True))
    plan = B.create_plan("t", [
        {"id": "01", "title": "s1", "task": "01", "check": "test -f 01.txt"},
        {"id": "02", "title": "s2", "task": "02", "check": "test -f 02.txt"},
        {"id": "03", "title": "s3", "task": "03", "check": "test -f 03.txt"},
    ])
    B.approve_plan(plan["plan_id"])
    out = B.run_plan(plan["plan_id"], post_to_bus=False, code_call_budget=10)
    assert out["rate_limited"] is True
    assert out["code_calls"] == 1  # stopped escalating after first rate-limit
    assert out["done"] == 3        # local draft satisfied the checks


def test_blocking_no_cascade(db, monkeypatch):
    monkeypatch.setattr(B, "_invoke_local_draft", _mock_local(["a"]))
    monkeypatch.setattr(B, "_invoke_claude_code", _mock_code(ok=False))  # Code can't fix b
    plan = B.create_plan("t", [
        {"id": "a", "title": "a", "task": "a", "check": "test -f a.txt"},
        {"id": "b", "title": "b", "task": "b", "check": "test -f never.txt", "retries": 1},
        {"id": "c", "title": "c", "task": "c", "check": "test -f c.txt", "depends_on": ["b"]},
    ])
    B.approve_plan(plan["plan_id"])
    B.run_plan(plan["plan_id"], post_to_bus=False)
    plan = B.load_plan(plan["plan_id"])
    st = {s["id"]: s["status"] for s in plan["steps"]}
    assert st["a"] == "done"
    assert st["b"] == "blocked"
    assert st["c"] == "pending"


def test_resume_skips_done(db, monkeypatch):
    monkeypatch.setattr(B, "_invoke_local_draft", _mock_local(["a"]))
    monkeypatch.setattr(B, "_invoke_claude_code", _mock_code())
    plan = B.create_plan("t", [{"id": "a", "title": "a", "task": "a", "check": "test -f a.txt"}])
    B.approve_plan(plan["plan_id"])
    B.run_plan(plan["plan_id"], post_to_bus=False)
    out2 = B.run_plan(plan["plan_id"], post_to_bus=False)
    assert out2["ran"] == 0


def test_duplicate_step_id_rejected(db):
    with pytest.raises(ValueError):
        B.create_plan("t", [
            {"id": "x", "title": "x", "task": "x", "check": "true"},
            {"id": "x", "title": "x2", "task": "x", "check": "true"},
        ])


def test_plan_project_rejects_bad_json(db, monkeypatch):
    # planner returns non-JSON -> empty plan, create_and_run reports failure
    import mongoose.services.builder as B2
    monkeypatch.setattr(B2, "plan_project", lambda d: [])
    out = B2.create_and_run("build me a thing", auto_approve=True, post_to_bus=False)
    assert out["ok"] is False


def test_example_court_plan_is_valid_dag(db):
    from mongoose.services.build_plans import EXAMPLE_COURT_STEPS
    ids = {s["id"] for s in EXAMPLE_COURT_STEPS}
    for s in EXAMPLE_COURT_STEPS:
        for dep in s.get("depends_on", []):
            assert dep in ids
    # every step must declare a success criterion: a verify spec (preferred) or
    # a legacy check string.
    assert all(s.get("verify") or s.get("check") for s in EXAMPLE_COURT_STEPS)


def test_run_sync_rejects_async_context(db):
    # _run_sync must refuse to run inside a live event loop (would otherwise be a
    # cryptic 'event loop already running' crash if builder is called from a route).
    import asyncio
    import mongoose.services.builder as B2

    async def call_in_loop():
        return B2._run_sync(_noop())

    async def _noop():
        return 1

    with pytest.raises(RuntimeError, match="async context"):
        asyncio.run(call_in_loop())


def test_owner_id_used_for_bus_posts(db, monkeypatch):
    # builder should post to settings.owner_id, not a hardcoded name.
    monkeypatch.setenv("MONGOOSE_OWNER_ID", "tester")
    from mongoose.settings import get_settings
    assert get_settings().owner_id == "tester"
    monkeypatch.setattr(B, "_invoke_local_draft", _mock_local(["a"]))
    monkeypatch.setattr(B, "_invoke_claude_code", _mock_code())
    plan = B.create_plan("t", [{"id": "a", "title": "a", "task": "a", "check": "test -f a.txt"}])
    B.approve_plan(plan["plan_id"])
    B.run_plan(plan["plan_id"], post_to_bus=True)
    from mongoose.services import bus
    msgs = bus.list_messages(to="tester", unread_only=False)
    assert any(m["sender"] == "builder" for m in msgs)
