"""Tests for the three limitation fixes: capability detect/install layer, role
classifier confidence + ambiguity + regression corpus, and the row-level data
verifier with dry-run repair."""
from pathlib import Path

import pytest

from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- capability layer --------------------------------------------------------
def test_manifest_loads_capabilities():
    from mongoose.services.capabilities import detect_all
    caps = {d["capability"] for d in detect_all()}
    for expected in ("core", "documents", "google", "microsoft", "voice", "qa"):
        assert expected in caps


def test_detect_reports_present_and_missing():
    from mongoose.services.capabilities import detect_capability
    # httpx is always installed -> a fake 'core-like' capability is ready
    ready = detect_capability("x", {"pip": ["httpx"]})
    assert ready["ready"] and not ready["missing_pip"]
    # a definitely-absent package -> missing
    missing = detect_capability("y", {"pip": ["definitely-not-real-pkg-zzz"]})
    assert not missing["ready"] and "definitely-not-real-pkg-zzz" in missing["missing_pip"]


def test_install_capability_dry_run_does_not_install(monkeypatch):
    from mongoose.services import capabilities as C
    called = {"installed": False}

    def fake_run(*a, **k):
        called["installed"] = True
        class R: returncode = 0; stderr = ""; stdout = ""
        return R()
    monkeypatch.setattr(C.subprocess, "run", fake_run)
    res = C.install_capability("documents", dry_run=True)
    assert res["ok"] and called["installed"] is False  # dry-run never shells out


def test_install_uses_active_interpreter(monkeypatch):
    from mongoose.services import capabilities as C
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        class R: returncode = 0; stderr = ""; stdout = ""
        return R()
    monkeypatch.setattr(C.subprocess, "run", fake_run)
    # force a missing package so it attempts install
    monkeypatch.setattr(C, "_module_present", lambda p: False)
    C.install_capability("voice")
    import sys
    # must install into THIS interpreter, not a bare 'pip'
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1:4] == ["-m", "pip", "install"]


def test_capabilities_report_shape():
    from mongoose.services.capabilities import capabilities_report
    rep = capabilities_report()
    assert "ready" in rep and "missing" in rep
    for m in rep["missing"]:
        assert "fix" in m and "missing" in m


def test_doctor_includes_capabilities(db):
    from mongoose.doctor import run_doctor
    rep = run_doctor()
    assert "capabilities" in rep["checks"]


# --- role classifier confidence + ambiguity ----------------------------------
def test_role_decision_carries_confidence_and_rules():
    from mongoose.role_router import classify_role_detailed
    d = classify_role_detailed("turn off the lights")
    assert d.role == "tool" and d.confidence >= 0.9 and d.matched_rules


def test_ambiguous_phrase_flags_for_confirmation():
    from mongoose.role_router import classify_role_detailed
    d = classify_role_detailed("can you sort this out")
    assert d.needs_confirmation is True
    assert d.confidence < 0.6   # low confidence, not a silent action


def test_string_api_still_works():
    from mongoose.role_router import classify_role
    assert classify_role("turn off the lights") == "tool"
    assert classify_role("find my certificate") == "tool"
    assert classify_role("find peace") == "chat"


def test_role_regression_corpus():
    """Every case in the corpus must classify as expected. Precision on tool/code
    is the priority: a chat case misrouted to tool/code is a hard failure."""
    fixture = Path(__file__).parent / "fixtures" / "role_classifier_cases.yaml"
    text = fixture.read_text()
    # tiny parser: lines like "- {text: \"...\", expected: tool}"
    import re
    cases = []
    for line in text.splitlines():
        m = re.search(r'text:\s*"(.*?)",\s*expected:\s*(\w+)', line)
        if m:
            cases.append((m.group(1), m.group(2)))
    assert len(cases) >= 25, "corpus should have a meaningful number of cases"

    from mongoose.role_router import classify_role
    failures = []
    for text_in, expected in cases:
        got = classify_role(text_in)
        if got != expected:
            failures.append((text_in, expected, got))
    # Allow zero misroutes of chat->action (the dangerous direction).
    dangerous = [f for f in failures if f[1] == "chat" and f[2] in ("tool", "code")]
    assert not dangerous, f"chat misrouted to action: {dangerous}"
    # Overall accuracy should be high.
    acc = 1 - len(failures) / len(cases)
    assert acc >= 0.9, f"role accuracy {acc:.2f} too low; failures: {failures}"


# --- data verifier -----------------------------------------------------------
def test_verify_data_clean_on_healthy_db(db, tmp_path):
    from mongoose.services.file_index import index_files
    from mongoose.services.email_index import ingest_email, EmailIn
    from mongoose.services.data_verify import verify_data
    d = tmp_path / "f"
    d.mkdir()
    (d / "a.md").write_text("hello")
    index_files(str(d), device="local")
    ingest_email(EmailIn(source="imap", source_message_id="<1>", subject="Hi", body="x", privacy="low"))
    r = verify_data()
    assert r["ok"] and r["errors"] == 0


def test_verify_data_detects_duplicate_memory(db):
    from mongoose.db import connect
    from mongoose.services.data_verify import verify_data
    with connect() as con:
        con.execute("INSERT INTO memory_facts(fact, source, created_at, updated_at) VALUES('dup','x','t','t')")
        con.execute("INSERT INTO memory_facts(fact, source, dedupe_key, created_at, updated_at) VALUES('dup','x','k','t','t')")
    r = verify_data()
    mem = next(f for f in r["findings"] if f["check"] == "memory.no_exact_duplicates")
    assert mem["level"] == "warn" and mem["count"] == 1


def test_verify_data_detects_missing_physical_file(db, tmp_path):
    from mongoose.services.file_index import index_files
    from mongoose.services.data_verify import verify_data
    d = tmp_path / "f"
    d.mkdir()
    f = d / "gone.md"
    f.write_text("temp")
    index_files(str(d), device="local")
    f.unlink()   # delete the physical file after indexing
    r = verify_data()
    phys = next(x for x in r["findings"] if x["check"] == "files.physical_present")
    assert phys["level"] == "warn" and phys["count"] >= 1


def test_repair_data_dry_run_then_apply(db):
    from mongoose.db import connect
    from mongoose.services.data_verify import repair_data, verify_data
    with connect() as con:
        con.execute("INSERT INTO memory_facts(fact, source, created_at, updated_at) VALUES('d','x','t','t')")
        con.execute("INSERT INTO memory_facts(fact, source, dedupe_key, created_at, updated_at) VALUES('d','x','k','t','t')")
    dry = repair_data("dedupe-memory", dry_run=True)
    assert dry["dry_run"] and dry["would_affect"] == 1
    # dry run changed nothing
    assert verify_data()["warnings"] >= 1
    applied = repair_data("dedupe-memory", dry_run=False)
    assert applied["ok"] and applied.get("applied")
    assert verify_data()["warnings"] == 0


def test_repair_data_rejects_unknown_action(db):
    from mongoose.services.data_verify import repair_data
    r = repair_data("rm -rf everything", dry_run=False)
    assert r["ok"] is False
