"""Regression tests for the external v0.11.1 audit findings (fixed in v0.11.2)."""
import sqlite3
import pytest
from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- #1 python -c no longer a safe check ---
def test_python_c_not_a_safe_check(db):
    from mongoose.services.builder import _is_safe_structured_check
    assert _is_safe_structured_check("python -c \"import os\"") is False
    assert _is_safe_structured_check("python -m pytest -q tests/test_x.py") is True
    assert _is_safe_structured_check("test -f foo") is True


# --- #2 step / plan id validation ---
def test_step_id_must_be_safe(db):
    from mongoose.services.builder import validate_steps
    with pytest.raises(ValueError):
        validate_steps([{"id": "../escape", "title": "t", "task": "t", "check": "test -f x"}])
    with pytest.raises(ValueError):
        validate_steps([{"id": "a/b", "title": "t", "task": "t", "check": "test -f x"}])
    # legit id passes
    validate_steps([{"id": "01-scaffold", "title": "t", "task": "t", "check": "test -f x"}])


def test_plan_dir_rejects_bad_id(db):
    from mongoose.services.builder import _plan_dir
    with pytest.raises(ValueError):
        _plan_dir("../etc")


# --- #3 stale running-step recovery ---
def test_recover_stale_running_step(db):
    import mongoose.services.builder as B
    plan = B.create_plan("r", [{"id": "01-a", "title": "a", "task": "a", "check": "test -f a.txt"}])
    pid = plan["plan_id"]
    p = B.load_plan(pid)
    p["steps"][0]["status"] = "running"
    p["steps"][0]["started_at"] = "2020-01-01T00:00:00+00:00"
    B._save_plan(p)
    assert B.recover_stale_steps(pid) == 1
    assert B.load_plan(pid)["steps"][0]["status"] == "failed"


# --- #4 wall-clock cap (max_seconds=0 stops immediately) ---
def test_wall_clock_cap(db, monkeypatch):
    import mongoose.services.builder as B
    from pathlib import Path
    plan = B.create_plan("w", [{"id": "01-a", "title": "a", "task": "a", "check": "test -f a.txt"}])
    pid = plan["plan_id"]
    B.approve_plan(pid)
    monkeypatch.setattr(B, "_invoke_local_draft", lambda t, r: ((Path(r) / "a.txt").write_text("x"), (True, "d"))[1])
    monkeypatch.setattr(B, "_invoke_claude_code", lambda t, r: (True, "ok", False))
    out = B.run_plan(pid, post_to_bus=False, max_seconds=0)
    assert out["hit_time_cap"] is True and out["ran"] == 0


# --- #11 schema migration adds missing columns, preserves data ---
def test_schema_migration_adds_columns(tmp_path, monkeypatch):
    dbp = tmp_path / "old.sqlite3"
    con = sqlite3.connect(dbp)
    con.executescript("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            source_event_id TEXT, calendar_name TEXT, title TEXT NOT NULL,
            start_at TEXT NOT NULL, end_at TEXT NOT NULL,
            busy_status TEXT NOT NULL DEFAULT 'busy', raw_json TEXT, last_seen TEXT NOT NULL
        );
        INSERT INTO events(source,title,start_at,end_at,last_seen)
        VALUES('manual','E','2026-06-15T09:00:00Z','2026-06-15T10:00:00Z','2026-06-14T00:00:00Z');
    """)
    con.commit()
    con.close()
    init_db(dbp)
    con = sqlite3.connect(dbp)
    cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    rows = con.execute("SELECT title FROM events").fetchall()
    con.close()
    assert "sensitivity" in cols and "detail_available" in cols
    assert ver == 1
    assert rows == [("E",)]


# --- #10 setup CSRF nonce ---
def test_setup_save_requires_nonce(db, tmp_path, monkeypatch):
    # Point write_env at a temp file so the test never writes the repo's .env.
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / ".env")
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    # no nonce -> 403
    assert c.post("/setup/save", json={"values": {"HA_URL": "http://x"}}).status_code == 403
    # wrong nonce -> 403
    assert c.post("/setup/save", json={"values": {"HA_URL": "http://x"}, "setup_nonce": "no"}).status_code == 403
    # correct nonce -> 200
    nonce = c.get("/setup/status").json()["setup_nonce"]
    assert c.post("/setup/save", json={"values": {"HA_URL": "http://x"}, "setup_nonce": nonce}).status_code == 200


# --- #8 voice session degrades to typed input when STT missing ---
def test_voice_session_degrades_to_typed(db, monkeypatch, capsys):
    import mongoose.voice as V
    import mongoose.llm as llm

    async def fake_chat(prompt, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "ok", "remembered": []}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    monkeypatch.setattr(V, "_stt_ready", lambda: (False, "need sox"))
    monkeypatch.setattr(V, "_tts_available", lambda: False)
    inputs = iter(["hi", "exit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    assert V.run_session() == 0


# --- #3 packaging: no real .env in the source tree ---
def test_no_dotenv_in_source_tree():
    """A release must ship .env.example, never a real .env. This guards the
    working tree; the packaging step also excludes .env explicitly."""
    from pathlib import Path
    # repo root = three up from this test file (tests/ -> repo)
    repo = Path(__file__).resolve().parent.parent
    assert not (repo / ".env").exists(), "a real .env is present in the source tree; it must not be packaged"
    assert (repo / ".env.example").exists(), ".env.example must be present"
