"""Regression tests for the external v0.12.3 QA audit findings.

Each test pins a fix so the finding cannot silently return:
- negated memory must not reach the model extractor (gate, not just explicit path)
- named verifiers must contain absolute and traversal paths
- migration must preserve DISTINCT rows with NULL source ids
- the role classifier and the /assist home-control detector must agree
- plan_project must use the shared validator
- setup_voice must report failure when deps remain missing
"""
import tempfile
from pathlib import Path

import pytest

from mongoose.db import init_db, connect


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- Finding 1: negated memory must not be fact-bearing ----------------------
@pytest.mark.parametrize("text", [
    "do not remember that I drive a Honda",
    "don't remember my home address",
    "please do not remember my phone number",
    "forget that I live in Anytown",
    "please forget my email",
])
def test_negated_memory_is_not_fact_bearing(text):
    from mongoose.services.memory import looks_fact_bearing, detect_explicit_memory
    # the extractor gate must refuse, so the model is never asked to extract
    assert looks_fact_bearing(text) is False
    assert detect_explicit_memory(text) is None


@pytest.mark.parametrize("text", [
    "I drive a Honda",
    "please remember I drive a Honda",
    "my dog is called Toby",
])
def test_real_facts_still_extract(text):
    from mongoose.services.memory import looks_fact_bearing
    assert looks_fact_bearing(text) is True


# --- Finding 2: verifier path containment ------------------------------------
@pytest.mark.parametrize("bad", ["/tmp", "/etc/passwd", "../escape", "../../x"])
def test_contained_rejects_absolute_and_traversal(bad):
    from mongoose.services.verifiers import _contained
    repo = Path(tempfile.mkdtemp())
    assert _contained(repo, bad) is None


def test_ruff_verifier_refuses_absolute_path():
    from mongoose.services.verifiers import run_verifier
    repo = Path(tempfile.mkdtemp())
    ok, msg = run_verifier({"type": "ruff", "path": "/tmp"}, repo)
    assert ok is False
    assert "escapes repo" in msg


def test_pytest_verifier_refuses_absolute_target():
    from mongoose.services.verifiers import run_verifier
    repo = Path(tempfile.mkdtemp())
    ok, msg = run_verifier({"type": "pytest", "target": "/tmp/evil.py"}, repo)
    assert ok is False
    assert "escapes repo" in msg


# --- Finding 3: migration preserves distinct NULL-id rows --------------------
def test_migration_preserves_distinct_null_id_events(tmp_path, monkeypatch):
    dbp = tmp_path / "old.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(dbp))
    init_db(dbp)
    ins = ("INSERT INTO events (source, source_event_id, title, start_at, end_at, last_seen) "
           "VALUES ('manual', NULL, ?, ?, ?, '2026-06-14T00:00')")
    with connect(dbp) as con:
        con.execute(ins, ("Dentist", "2026-07-01T09:00", "2026-07-01T10:00"))
        con.execute(ins, ("School run", "2026-07-02T08:00", "2026-07-02T09:00"))
    import mongoose.db as db
    db._SCHEMA_READY.clear()
    init_db(dbp)  # re-run migration
    with connect(dbp) as con:
        rows = con.execute("SELECT title, source_event_id FROM events ORDER BY id").fetchall()
    titles = sorted(r[0] for r in rows)
    assert titles == ["Dentist", "School run"]  # neither distinct row was deleted
    assert all(str(r[1]).startswith("local:") for r in rows)  # NULLs got synthetic ids


# --- Finding 4: role classifier agrees with the /assist detector -------------
@pytest.mark.parametrize("text", [
    "close the blinds",
    "open the curtains",
    "set temperature to 19",
    "turn on the lamp",
    "dim the lights",
])
def test_home_control_routes_to_tool_on_both_paths(text):
    from mongoose.role_router import classify_role
    from mongoose.tools import _user_asked_for_home_control
    assert classify_role(text) == "tool"
    assert _user_asked_for_home_control(text) is True


@pytest.mark.parametrize("text", ["tell me a joke", "what is the capital of France"])
def test_chat_stays_chat_on_both_paths(text):
    from mongoose.role_router import classify_role
    from mongoose.tools import _user_asked_for_home_control
    assert classify_role(text) == "chat"
    assert _user_asked_for_home_control(text) is False


# --- Finding 5: plan_project uses the shared validator -----------------------
def test_plan_project_rejects_invalid_plan(db, monkeypatch):
    import mongoose.services.builder as B

    async def fake_ask(description, system=None, role="code"):
        # a plan with a bad step id (should be rejected by validate_steps)
        return '[{"id":"bad id!!","title":"t","task":"x","verify":{"type":"always_pass"}}]'
    monkeypatch.setattr("mongoose.llm.ask_ollama", fake_ask)
    steps = B.plan_project("build something")
    assert steps == []  # malformed plan dropped, not half-trusted


def test_plan_project_accepts_valid_plan(db, monkeypatch):
    import mongoose.services.builder as B

    async def fake_ask(description, system=None, role="code"):
        return ('[{"id":"01-scaffold","title":"scaffold","task":"make pyproject",'
                '"verify":{"type":"file_exists","path":"pyproject.toml"}}]')
    monkeypatch.setattr("mongoose.llm.ask_ollama", fake_ask)
    steps = B.plan_project("build something")
    assert len(steps) == 1
    assert steps[0]["id"] == "01-scaffold"


# --- Finding 6: setup_voice reports failure when deps remain missing ---------
def test_setup_voice_returns_failure_when_still_missing(monkeypatch, capsys):
    import mongoose.voice as V
    # brew + pip "succeed" but the tools are STILL not on PATH afterwards
    monkeypatch.setattr(V.shutil, "which", lambda c: "/usr/bin/brew" if c == "brew" else None)
    monkeypatch.setattr(V, "_whisper_cmd", lambda: None)
    monkeypatch.setattr(V.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    rc = V.setup_voice(yes=True)
    assert rc == 1  # not a false success
    assert "Still missing" in capsys.readouterr().out
