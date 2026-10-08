"""The forked-brain incident, made structural: an evening's conversation vanished because the
serving process, launched from a download folder, had its brain rebound to that folder by a
local .env. Three layers now prevent and detect it:

1. Brain-location keys (MONGOOSE_DB_PATH, MONGOOSE_DATA_DIR) are refused from ANY .env file at
   BOTH load sites (import time and get_settings, which re-loads and would otherwise quietly
   reinstate them). Only the real process environment (tests/CI) can move the brain.
2. Refusals are recorded in settings.REJECTED_BRAIN_OVERRIDES for doctor to name.
3. Doctor's brain_location check flags a non-canonical brain without explicit override, any
   refused .env override, and any stray brain with real messages in the realistic legacy spots
   (cwd/data, install-folder data), with message counts."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import warnings

warnings.filterwarnings("ignore")


def test_env_file_cannot_move_the_brain(tmp_path):
    """Run in a clean subprocess (settings loads at import): a package-adjacent .env carrying
    brain keys is refused at import AND stays refused across repeated get_settings() calls,
    while non-brain keys still load normally."""
    src = os.path.abspath("src")
    envfile = os.path.join(os.path.dirname(src), ".env")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    try:
        with open(envfile, "w") as f:
            f.write("MONGOOSE_DB_PATH=/tmp/hijacked-brain.sqlite3\n"
                    "MONGOOSE_DATA_DIR=/tmp/hijacked-data\n"
                    "MONGOOSE_BRIEF_TIME=08:15\n")
        code = textwrap.dedent("""
            import os
            import mongoose.settings as S
            assert S.REJECTED_BRAIN_OVERRIDES.get("MONGOOSE_DB_PATH", "").endswith("hijacked-brain.sqlite3")
            assert "MONGOOSE_DB_PATH" not in os.environ
            assert os.environ.get("MONGOOSE_BRIEF_TIME") == "08:15"
            for _ in range(2):
                db = str(S.get_settings().db_path)
                assert "hijacked" not in db, db
                assert db.endswith(".mongoose/data/mongoose.sqlite3"), db
            print("OK")
        """)
        r = subprocess.run([sys.executable, "-c", code],
                           env={"HOME": str(fake_home), "PATH": os.environ.get("PATH", ""),
                                "PYTHONPATH": src},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
    finally:
        try:
            os.remove(envfile)
        except FileNotFoundError:
            pass


def test_explicit_process_env_still_moves_the_brain(tmp_path):
    src = os.path.abspath("src")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    code = textwrap.dedent("""
        import mongoose.settings as S
        assert S.REJECTED_BRAIN_OVERRIDES == {}
        assert str(S.get_settings().db_path) == "/tmp/explicit-brain.sqlite3"
        print("OK")
    """)
    r = subprocess.run([sys.executable, "-c", code],
                       env={"HOME": str(fake_home), "PATH": os.environ.get("PATH", ""),
                            "PYTHONPATH": src, "MONGOOSE_DB_PATH": "/tmp/explicit-brain.sqlite3"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_doctor_flags_a_stray_brain_with_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "brain.sqlite3"))
    from mongoose.db import init_db
    init_db()
    launch_dir = tmp_path / "fake_install"
    (launch_dir / "data").mkdir(parents=True)
    stray = launch_dir / "data" / "mongoose.sqlite3"
    con = sqlite3.connect(stray)
    con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id TEXT, role TEXT, "
                "content TEXT, created_at TEXT)")
    for i in range(7):
        con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                    "VALUES ('mongoose-main','user',?,datetime('now'))", (f"lost {i}",))
    con.commit()
    con.close()
    monkeypatch.chdir(launch_dir)
    from mongoose.doctor import run_doctor
    c = run_doctor()["checks"]["brain_location"]
    assert c["ok"] is False
    assert "7 messages" in c["detail"] and "split across brains" in c["detail"]
    stray.unlink()
    assert run_doctor()["checks"]["brain_location"]["ok"] is True
