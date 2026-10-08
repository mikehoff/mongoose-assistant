"""Named verifiers: the safe replacement for model-generated check commands."""
from pathlib import Path
import pytest
from mongoose.services.verifiers import (
    run_verifier, validate_verify, VERIFIER_NAMES, VERIFIERS)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("# Project\n\n## Usage\nrun it\n")
    return tmp_path


def test_file_exists(repo):
    assert run_verifier({"type": "file_exists", "path": "src/app.py"}, repo)[0] is True
    assert run_verifier({"type": "file_exists", "path": "src/missing.py"}, repo)[0] is False


def test_dir_exists(repo):
    assert run_verifier({"type": "dir_exists", "path": "tests"}, repo)[0] is True
    assert run_verifier({"type": "dir_exists", "path": "nope"}, repo)[0] is False


def test_file_contains(repo):
    assert run_verifier({"type": "file_contains", "path": "README.md", "text": "Usage"}, repo)[0] is True
    assert run_verifier({"type": "file_contains", "path": "README.md", "text": "absent"}, repo)[0] is False


def test_path_escape_is_blocked(repo):
    # the whole point: no verifier can be pointed outside the repo
    for vtype in ("file_exists", "dir_exists"):
        assert run_verifier({"type": vtype, "path": "../../../etc/passwd"}, repo)[0] is False
        assert run_verifier({"type": vtype, "path": "/etc/passwd"}, repo)[0] is False


def test_python_imports_validates_module_name(repo):
    # a real module name is allowed; an injection attempt is rejected before running
    assert run_verifier({"type": "python_imports", "module": "os; import sys"}, repo)[0] is False
    assert run_verifier({"type": "python_imports", "module": "json"}, repo)[0] is True


def test_pytest_target_contained(repo):
    # a target outside the repo is refused (no execution)
    ok, msg = run_verifier({"type": "pytest", "target": "../evil_test.py"}, repo)
    assert ok is False and "escapes repo" in msg


def test_always_pass(repo):
    assert run_verifier({"type": "always_pass"}, repo)[0] is True


def test_unknown_type_fails_closed(repo):
    assert run_verifier({"type": "rm_rf", "path": "x"}, repo)[0] is False


def test_validate_verify():
    assert validate_verify({"type": "file_exists", "path": "x"}) is None
    assert "unknown verify type" in validate_verify({"type": "evil"})
    assert validate_verify("not a dict") is not None


def test_no_verifier_executes_a_model_string():
    # Structural guarantee: there is no verifier that takes a raw command string.
    # Every verifier builds its own argv; the registry is a fixed allowlist.
    assert set(VERIFIER_NAMES) == set(VERIFIERS)
    assert "shell" not in VERIFIER_NAMES and "command" not in VERIFIER_NAMES


def test_builder_uses_verify_spec(tmp_path, monkeypatch):
    # A plan with a verify spec runs and passes when the file is created.
    from mongoose.db import init_db
    dbp = tmp_path / "data" / "mongoose.sqlite3"
    dbp.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(dbp))
    init_db(dbp)
    import mongoose.services.builder as B

    def fake_local(t, r):
        (Path(r) / "made.txt").write_text("ok")
        return True, "drafted"
    monkeypatch.setattr(B, "_invoke_local_draft", fake_local)
    monkeypatch.setattr(B, "_invoke_claude_code", lambda t, r: (True, "ok", False))
    plan = B.create_plan("v", [{"id": "01-a", "title": "a", "task": "make it",
                                "verify": {"type": "file_exists", "path": "made.txt"}}])
    B.approve_plan(plan["plan_id"])
    out = B.run_plan(plan["plan_id"], post_to_bus=False)
    assert out["done"] == 1


def test_example_court_uses_verify_specs():
    from mongoose.services.build_plans import EXAMPLE_COURT_STEPS
    from mongoose.services.verifiers import validate_verify
    for st in EXAMPLE_COURT_STEPS:
        assert st.get("verify"), f"{st['id']} should use a verify spec"
        assert validate_verify(st["verify"]) is None
