"""Regression tests for the external-audit findings fixed in v0.10.2."""
import pytest
from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- #1 path containment ---
def test_builder_path_containment_blocks_escape(db, tmp_path, monkeypatch):
    import mongoose.services.builder as B
    repo = tmp_path / "repo"
    repo.mkdir()
    # A draft trying to escape the repo via ../sibling must be rejected.
    async def fake(prompt, system=None, role="code", model=None):
        import json
        return json.dumps([{"path": "../repoevil/pwn.txt", "content": "x"},
                           {"path": "ok.txt", "content": "fine"}])
    monkeypatch.setattr("mongoose.llm.ask_ollama", fake)
    ok, msg = B._invoke_local_draft("task", repo)
    assert (repo.parent / "repoevil" / "pwn.txt").exists() is False
    assert (repo / "ok.txt").exists() is True


# --- #2 shell-check hardening ---
def test_shell_check_refused_by_default(db, tmp_path):
    from mongoose.services.builder import _run_check, _is_safe_structured_check
    assert _is_safe_structured_check("python -m pytest -q tests/test_x.py")
    assert _is_safe_structured_check("test -f foo")
    assert not _is_safe_structured_check("pytest && rm -rf /")
    ok, out = _run_check("echo hi && rm -rf /", tmp_path)
    assert ok is False and "refused" in out
    ok2, _ = _run_check("echo hi > /tmp/x", tmp_path, allow_shell=True)
    # allow_shell permits it (echo succeeds)
    assert ok2 is True


# --- #5 NULL-source-id dedupe ---
def test_null_source_id_dedupe(db):
    from mongoose.services.calendar_tasks import add_event, add_task, EventIn, TaskIn
    from mongoose.db import connect
    for _ in range(2):
        add_event(EventIn(title="X", start_at="2026-06-15T09:00:00Z", end_at="2026-06-15T10:00:00Z"))
        add_task(TaskIn(title="Y", due_at="2026-06-15T12:00:00Z"))
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


# --- #6 availability error handling ---
def test_availability_rejects_bad_window(db):
    from mongoose.services.calendar_tasks import availability
    assert availability("not-a-date", "also-bad").get("error") == "invalid_time_window"
    r = availability("2026-06-15T10:00:00Z", "2026-06-15T09:00:00Z")
    assert "error" in r  # end before start


# --- #7 HA action verb mapping ---
def test_verb_service_mapping(db):
    from mongoose.services.entity_resolver import _verb_service
    assert _verb_service("cover", "close blinds") == ("cover", "close_cover")
    assert _verb_service("cover", "open the blinds") == ("cover", "open_cover")
    assert _verb_service("media_player", "pause music") == ("media_player", "media_pause")
    assert _verb_service("media_player", "stop") == ("media_player", "media_stop")
    assert _verb_service("light", "turn off") == ("light", "turn_off")
    assert _verb_service("fan", "turn on") == ("fan", "turn_on")


def test_fan_in_allowed_services(db):
    from mongoose.services.home_router import ALLOWED_HA_SERVICES
    assert ("fan", "turn_on") in ALLOWED_HA_SERVICES
    assert ("fan", "turn_off") in ALLOWED_HA_SERVICES


# --- #8 privacy default ---
def test_home_command_privacy_defaults_medium(db):
    from mongoose.services.home_router import HomeCommand
    c = HomeCommand(room="study", device_id="lamp", action="on")
    assert c.privacy == "medium"


# --- #9/#10 bus frontmatter + id validation ---
def test_bus_rejects_newline_in_metadata(db):
    from mongoose.services import bus
    r = bus.send_message(sender="a\nb", to="x", subject="ok", type="note")
    assert r.get("ok") is False


def test_bus_read_rejects_bad_id(db):
    from mongoose.services import bus
    assert bus.read_message("../../etc/passwd") is None
    assert bus.read_message("not-an-id") is None


# --- #4 share_file root restriction ---
def test_share_file_rejects_outside_roots(db, tmp_path, monkeypatch):
    from mongoose.services import bus
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    # data_dir is the allowed root; this file is elsewhere
    r = bus.share_file(str(outside))
    assert r.get("ok") is False


# --- #17 shared step validation ---
def test_create_plan_validates_steps(db):
    from mongoose.services.builder import create_plan
    with pytest.raises(ValueError):
        create_plan("bad", [{"id": "a", "title": "a"}])  # missing task/check
    with pytest.raises(ValueError):
        create_plan("bad", [{"id": "a", "title": "a", "task": "t", "check": "c",
                             "depends_on": ["nonexistent"]}])
