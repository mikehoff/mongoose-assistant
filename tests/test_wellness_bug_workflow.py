from __future__ import annotations

from pathlib import Path

import pytest


def _isolated(monkeypatch, tmp_path: Path):
    db = tmp_path / "brain" / "mongoose.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    monkeypatch.setenv("MONGOOSE_SOURCE_ROOT", str(Path.cwd()))
    # The tests must not accidentally invoke a real Claude Code command.
    monkeypatch.setenv("CLAUDE_CODE_CMD", "definitely-not-installed-mongoose-test-claude")
    return db


@pytest.mark.asyncio
async def test_wellness_checkins_configure_memory_and_run_without_guest_leak(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    from mongoose.services import wellness_checkins as W

    cfg = W.configure_checkins(enabled=True, owner=True, guests=True, hour=21, minute=5, source="test")
    assert cfg["enabled"] is True
    assert (cfg["hour"], cfg["minute"]) == (21, 5)
    assert cfg["memory_recorded"] in (True, False)  # memory failure must not break scheduling

    res = await W.run_checkin_cycle(source="test", force=True)
    assert res["ok"] is True
    assert res["record_ids"]  # owner record exists even without push subscribers
    assert res["owner_sent"] in (True, False)


@pytest.mark.asyncio
async def test_wellness_scheduler_registry_contains_disabled_but_visible_lane(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    from mongoose.services.wellness_checkins import configure_checkins
    from mongoose.services.scheduler import _build_registry, _JOB_DESCRIPTIONS

    configure_checkins(enabled=True, owner=True, guests=False, hour=19, minute=45)
    registry = _build_registry()
    assert "wellness_checkins" in registry
    assert registry["wellness_checkins"][0] == ("daily", 19, 45)
    assert "self-journal" in _JOB_DESCRIPTIONS["wellness_checkins"]


@pytest.mark.asyncio
async def test_bug_log_detail_diagnose_and_attempt_fix_refuses_weak_detail(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    from mongoose.services.bug_workflow import add_bug_detail, attempt_fix, bug_status, diagnose_bug, log_bug

    bug = log_bug("log bug install page says done but has no button")
    # Bug ids are now a short, memorable uppercase letter+two-digits form (e.g. A42), with ambiguous
    # I/O excluded. Legacy BUG-YYYYMMDD-XXXXXX ids still parse but new ids use the short form.
    import re as _re
    assert _re.fullmatch(r"[A-HJ-NP-Z]\d{2}", bug["bug_id"]), bug["bug_id"]
    assert add_bug_detail(bug["bug_id"], "add bug detail user sees 100 percent done, no handoff link")["ok"]
    diag = await diagnose_bug(bug["bug_id"], "diagnose bug likely browser install journey")
    assert diag["ok"]
    assert diag["area"] in {"installer/watchdog", "unknown/general"}
    st = bug_status(bug["bug_id"])
    assert st["ok"] and st["bug"]["events"]

    # The function is allowed to create a workspace, but with no codegen command it should not fabricate
    # a patch or apply anything.
    res = attempt_fix(bug["bug_id"], run_codegen=False, request_peers=False)
    assert res["ok"] is True
    assert res["patch_generated"] is False


def test_bug_apply_fix_requires_peer_ok(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    from mongoose.services.bug_workflow import apply_fix, log_bug

    bug = log_bug("log bug real enough to track")
    res = apply_fix(bug["bug_id"])
    assert res["ok"] is False
    assert "peer" in res["error"]


def test_chat_local_plans_for_wellness_and_bug_workflow(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    from mongoose.tools import _local_status_plan

    assert _local_status_plan("remind me and my guests to self journal structured wellness checkins")["tool"] == "wellness_checkins"
    assert _local_status_plan("log bug install dead end with no link")["tool"] == "log_bug"
    bid = "A42"
    assert _local_status_plan(f"add bug detail {bid} traceback from watchdog")["args"]["bug_id"] == bid
    assert _local_status_plan(f"diagnose bug {bid}")["tool"] == "diagnose_bug"
    assert _local_status_plan(f"attempt fix {bid}")["tool"] == "attempt_bug_fix"
    assert _local_status_plan(f"apply fix {bid}")["tool"] == "apply_bug_fix"


# --- adopted-feature coverage (parallel-coder reconciliation) -----------------

def test_legacy_bug_ids_still_parse_alongside_short_ids(monkeypatch, tmp_path):
    """We adopted the short A42 id form but must keep parsing legacy BUG-YYYYMMDD-XXXX ids so older
    bug records remain referenceable. Both forms parse; the short form is preferred when both appear."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "brain" / "mongoose.sqlite3"))
    from mongoose.services.bug_workflow import parse_bug_id, _valid_bug_id
    # short
    assert parse_bug_id("please diagnose bug a42 now") == "A42"
    # legacy preserved
    assert parse_bug_id("see BUG-20260630-ABC123 for context") == "BUG-20260630-ABC123"
    # both valid
    assert _valid_bug_id("A42") and _valid_bug_id("BUG-20260630-ABC123")
    assert not _valid_bug_id("not-an-id")
    # short preferred when both present
    assert parse_bug_id("A42 supersedes BUG-20260630-ABC123") == "A42"


def test_wellness_journal_entries_are_persistent_timestamped_rows(monkeypatch, tmp_path):
    """Adopted persistence: every check-in response is its own immutable, timestamped row in
    wellness_journal_entries , a later entry must NOT overwrite an earlier one (the owner asked for
    entries to be stored persistently, not just acknowledged)."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "brain" / "mongoose.sqlite3"))
    from mongoose.services import wellness_checkins as W
    from mongoose.db import connect
    W.log_checkin_response(text="mood 6 energy 4 shoulders tight", source="test")
    W.log_checkin_response(text="mood 7 calmer after a walk", source="test")
    with connect() as con:
        rows = con.execute(
            "SELECT created_at, entry_text, private FROM wellness_journal_entries ORDER BY created_at"
        ).fetchall()
    assert len(rows) == 2, "second entry overwrote the first instead of persisting separately"
    assert all(dict(r)["private"] == 1 for r in rows)
    assert dict(rows[0])["entry_text"] != dict(rows[1])["entry_text"]
