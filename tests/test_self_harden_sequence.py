from __future__ import annotations

import json
import zipfile
from pathlib import Path


def _toy_repo(root: Path, version: str = "9.9.9") -> Path:
    repo = root / "candidate"
    (repo / "src" / "mongoose").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text(f'[project]\nname = "mongoose"\nversion = "{version}"\n', encoding="utf-8")
    (repo / "release.json").write_text(json.dumps({"package": "mongoose", "version": version, "expected_commands": [], "schema": 1}), encoding="utf-8")
    (repo / "src" / "mongoose" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (repo / "src" / "mongoose" / "cli.py").write_text('def main(): pass\n', encoding="utf-8")
    (repo / "tests" / "test_ok.py").write_text('def test_ok():\n    assert True\n', encoding="utf-8")
    return repo


def test_reingest_codebase_indexes_claims_and_owners(tmp_path, monkeypatch):
    from mongoose.db import init_db
    from mongoose.services import self_harden as H

    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite3"))
    init_db()
    root = tmp_path / "repo"
    (root / "src" / "mongoose" / "services").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "mongoose" / "setup_wizard.py").write_text(
        '"""Setup."""\nimport os\ndef route(app):\n    os.getenv("GOOGLE_CLIENT_SECRETS")\n',
        encoding="utf-8",
    )
    (root / "src" / "mongoose" / "services" / "scheduler.py").write_text(
        'def registry():\n    reg = {}\n    reg["self_harden_scan"] = ("interval", None)\n    return reg\n',
        encoding="utf-8",
    )
    (root / "tests" / "test_setup_wizard.py").write_text("def test_x(): pass\n", encoding="utf-8")
    out = H.reingest_codebase(root, out=tmp_path / "reingest")
    assert out["modules"] == 2
    rows = H.module_index(query="setup_wizard")
    assert rows
    assert "configured_truth" in rows[0]["claims"]
    assert rows[0]["owner"]["area"] == "setup/onboarding"
    sched = H.module_index(query="scheduler")
    assert sched[0]["scheduler_jobs"] == ["self_harden_scan"]
    assert Path(out["md_path"]).exists()


def test_reviewer_packet_includes_observation_and_related_modules(tmp_path, monkeypatch):
    from mongoose.db import init_db
    from mongoose.services import self_harden as H

    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite3"))
    init_db()
    root = tmp_path / "repo"
    (root / "src" / "mongoose").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "mongoose" / "setup_wizard.py").write_text('def x(): pass\n', encoding="utf-8")
    H.rebuild_module_index(root)
    obs = H.record_observation("user", "issue", "setup", "Setup says configured from placeholder", severity="P0", claim_key="configured_truth")
    pkt = H.create_reviewer_packet(tmp_path / "review", observation_id=obs["id"], reviewer="claude")
    md = Path(pkt["md_path"]).read_text(encoding="utf-8")
    js = json.loads(Path(pkt["json_path"]).read_text(encoding="utf-8"))
    assert "Setup says configured" in md
    assert "Hidden falsifying state" in md
    assert js["claim"]["key"] == "configured_truth"
    assert pkt["related_modules"] >= 1


def test_stage_update_candidate_is_safe_and_requires_approval(tmp_path, monkeypatch):
    from mongoose import updater
    from mongoose.db import init_db

    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    repo = _toy_repo(tmp_path, "9.8.7")
    stage = updater.stage_update_candidate(repo)
    assert stage["target_version"] == "9.8.7"
    assert stage["approved"] is False
    assert Path(stage["candidate_root"]).exists()
    stages = updater.list_update_stages()
    assert any(s["stage_id"] == stage["stage_id"] for s in stages)
    checked = updater.check_update_stage(stage["stage_id"])
    assert any(c["name"] == "compileall" for c in checked["checks"])
    approved = updater.approve_update_stage(stage["stage_id"])
    assert approved["approved"] is True


def test_stage_update_rejects_unsafe_zip(tmp_path, monkeypatch):
    from mongoose import updater
    from mongoose.db import init_db

    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../evil.txt", "no")
    try:
        updater.stage_update_candidate(z)
    except ValueError as exc:
        assert "unsafe zip" in str(exc)
    else:
        raise AssertionError("unsafe zip was accepted")


def test_cli_parses_new_safe_update_and_harden_commands():
    # Parser smoke is covered by command-surface tests; here we assert the source has the commands
    src = Path("src/mongoose/cli.py").read_text(encoding="utf-8")
    assert "--stage" in src
    assert "--check-stage" in src
    assert "review-packet" in src
    assert "reingest" in src


def test_stage_update_does_not_copy_live_state_or_nested_stages(tmp_path, monkeypatch):
    from mongoose import updater
    from mongoose.db import init_db

    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "live" / "data" / "mongoose.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "live" / "data"))
    live = _toy_repo(tmp_path / "live", "9.8.8")
    (live / "data" / "update_stages" / "old").mkdir(parents=True)
    (live / "data" / "update_stages" / "old" / "huge.txt").write_text("do not copy", encoding="utf-8")
    (live / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")
    (live / "secrets").mkdir()
    (live / "secrets" / "token.txt").write_text("secret", encoding="utf-8")
    init_db()
    stage = updater.stage_update_candidate(live)
    root = Path(stage["candidate_root"])
    assert not (root / "data").exists()
    assert not (root / ".env").exists()
    assert not (root / "secrets").exists()
