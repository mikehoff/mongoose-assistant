"""Regression tests for the external v0.11.2 audit findings (fixed in v0.11.3)."""
import sqlite3
import pytest
from pathlib import Path
from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- #2 structured-check path arguments must stay in the repo ---
def test_check_path_args_contained(db, tmp_path):
    from mongoose.services.builder import _is_safe_structured_check
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_x.py").write_text("def test_ok(): pass")
    assert _is_safe_structured_check("python -m pytest -q tests/test_x.py", repo) is True
    assert _is_safe_structured_check("python -m pytest -q ../evil.py", repo) is False
    assert _is_safe_structured_check("test -f ../outside.txt", repo) is False
    assert _is_safe_structured_check("ruff check ..", repo) is False


# --- #7 negated 'do not remember' must not store ---
def test_negated_remember_does_not_store(db):
    from mongoose.services.memory import detect_explicit_memory as d
    assert d("remember that my dog is Fido") == "my dog is Fido"
    assert d("do not remember that my dog is Fido") is None
    assert d("please do not remember that my dog is Fido") is None
    assert d("don't remember that I live in Hull") is None
    assert d("forget that I have a car") is None


# --- #6 broad recall returns stored facts ---
def test_broad_recall_returns_facts(db):
    from mongoose.services.memory import remember_fact, build_context
    remember_fact("The user has a dog named Toby.")
    remember_fact("The user lives in Anytown.")
    _, facts = build_context("c", "what do you remember about me?")
    assert any("Toby" in f for f in facts)
    # narrow query still uses FTS
    _, narrow = build_context("c", "Anytown")
    assert any("Anytown" in f for f in narrow)


# --- #8 bus.share_file byte-copies (binary not corrupted) ---
def test_share_file_byte_copies_binary(db, tmp_path, monkeypatch):
    from mongoose.services import bus
    monkeypatch.setenv("MONGOOSE_SHARED_FILE_ROOTS", str(tmp_path))
    # bytes that are NOT valid utf-8
    raw = bytes([0x89, 0x50, 0x4e, 0x47, 0xff, 0xfe, 0x00, 0x01])
    src = tmp_path / "img.bin"
    src.write_bytes(raw)
    r = bus.share_file(str(src))
    assert r.get("ok"), r
    copied = Path(r["path"]).read_bytes()
    assert copied == raw, "binary file was corrupted by the copy"


# --- #5 migration dedupes messy data before unique index ---
def test_migration_dedupes_before_unique_index(tmp_path):
    dbp = tmp_path / "old.sqlite3"
    con = sqlite3.connect(dbp)
    con.executescript("""
        CREATE TABLE email_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_message_id TEXT,
            mailbox TEXT, sender TEXT, recipients TEXT, subject TEXT NOT NULL, body TEXT NOT NULL,
            sent_at TEXT, received_at TEXT, privacy TEXT, raw_json TEXT, indexed_at TEXT
        );
        CREATE VIRTUAL TABLE email_fts USING fts5(subject, body, sender, mailbox, content='');
        INSERT INTO email_messages(source,source_message_id,subject,body) VALUES('manual',NULL,'Dentist','x');
        INSERT INTO email_messages(source,source_message_id,subject,body) VALUES('manual',NULL,'School','y');
        INSERT INTO email_messages(source,source_message_id,subject,body) VALUES('imap','abc','Re','y');
        INSERT INTO email_messages(source,source_message_id,subject,body) VALUES('imap','abc','Re','y');
    """)
    con.commit()
    con.close()
    init_db(dbp)  # must not raise
    con = sqlite3.connect(dbp)
    rows = con.execute("SELECT subject, source_message_id FROM email_messages ORDER BY id").fetchall()
    idx = con.execute("SELECT name FROM sqlite_master WHERE name='idx_email_dedupe'").fetchone()
    con.close()
    # The two NULL-id rows are DISTINCT user data: both must survive with
    # synthesized local ids. The two genuine imap/abc duplicates collapse to one.
    subjects = sorted(r[0] for r in rows)
    assert subjects == ["Dentist", "Re", "School"], subjects
    assert idx
    # NULL ids were replaced with deterministic local: ids
    null_ids = [r[1] for r in rows if r[0] in ("Dentist", "School")]
    assert all(str(i).startswith("local:") for i in null_ids)


# --- #9 --max-hours flows through create_and_run ---
def test_create_and_run_accepts_max_seconds(db, monkeypatch):
    import mongoose.services.builder as B
    monkeypatch.setattr(B, "plan_project", lambda d: [
        {"id": "01-a", "title": "a", "task": "a", "check": "test -f a.txt"}])
    out = B.create_and_run("build x", auto_approve=True, post_to_bus=False, max_seconds=0)
    # max_seconds=0 stops immediately, so nothing ran
    assert out.get("hit_time_cap") is True


# --- audit #10: doctor distinguishes core readiness from build/HA/cloud ---
def test_doctor_reports_readiness_breakdown(db):
    from mongoose.doctor import run_doctor
    r = run_doctor()
    assert "readiness" in r
    for key in ("core", "home_assistant", "overnight_builds", "cloud_escalation"):
        assert key in r["readiness"]
        assert isinstance(r["readiness"][key], bool)
