"""Tests for the backup module: creates a brain+config archive, restores it (brain AND config come
back), and the grandfather-father-son local rota keeps the right set."""
from __future__ import annotations

import os
import tarfile
import tempfile
from datetime import datetime, timezone, timedelta


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "data", "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = os.path.join(d, "data")
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    os.chdir(d)
    with open(os.path.join(d, ".env"), "w") as f:
        f.write("MONGOOSE_API_TOKEN=secret123\nNTFY_TOPIC=mine\n")
    with open(os.path.join(d, "data", "session_salt"), "w") as f:
        f.write("SALT")
    from mongoose.db import init_db
    init_db()
    from mongoose.services.memory import ensure_conversation, add_message, DEFAULT_CONVERSATION_ID
    ensure_conversation(DEFAULT_CONVERSATION_ID)
    add_message(DEFAULT_CONVERSATION_ID, "user", "my car is a Honda")
    return d


def test_create_backup_contains_brain_and_config():
    _fresh()
    from mongoose import backup as B
    r = B.create_backup("test")
    assert r["ok"] is True
    assert "brain_db" in r["manifest"]["contents"]
    assert "env" in r["manifest"]["contents"]
    assert r["manifest"]["kind"] == "brain+config"
    # archive is a real tar.gz with a manifest inside
    with tarfile.open(r["archive"], "r:gz") as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert any(n.endswith(".sqlite3") for n in names)


def test_restore_brings_back_brain_and_config():
    d = _fresh()
    from mongoose import backup as B
    from mongoose.services.memory import recent_messages, DEFAULT_CONVERSATION_ID
    r = B.create_backup("test")
    # wipe brain + env
    os.remove(os.path.join(d, "data", "m.sqlite3"))
    os.remove(os.path.join(d, ".env"))
    rr = B.restore_backup(r["archive"])
    assert rr["ok"] is True
    assert "brain_db" in rr["restored"]
    assert "env" in rr["restored"]
    # content actually came back
    msgs = recent_messages(DEFAULT_CONVERSATION_ID, limit=5)
    assert any("Honda" in m["content"] for m in msgs)
    with open(os.path.join(d, ".env")) as f:
        assert "secret123" in f.read()


def test_restore_brain_only_skips_config():
    d = _fresh()
    from mongoose import backup as B
    r = B.create_backup("test")
    # change env after backup; brain-only restore must NOT overwrite it
    with open(os.path.join(d, ".env"), "w") as f:
        f.write("CHANGED=yes\n")
    os.remove(os.path.join(d, "data", "m.sqlite3"))
    rr = B.restore_backup(r["archive"], restore_config=False)
    assert rr["ok"] is True
    assert "env" not in rr["restored"]
    with open(os.path.join(d, ".env")) as f:
        assert "CHANGED=yes" in f.read()  # config left alone


def test_rota_keeps_grandfather_father_son():
    """7 daily + 4 weekly + 12 monthly. Synthesize archives across a year and confirm the rota keeps a
    sensible spread and prunes the rest."""
    _fresh()
    from mongoose import backup as B
    root = B._backup_root()
    now = datetime.now(timezone.utc)
    # make 60 daily archives going back 60 days
    made = []
    for i in range(60):
        dt = now - timedelta(days=i)
        name = f"mongoose-backup-daily-{dt.strftime('%Y%m%dT%H%M%S')}.tar.gz"
        p = root / name
        p.write_bytes(b"x")
        made.append(p)
    res = B.apply_rota(now=now)
    # should keep far fewer than 60 (5 daily + a few weekly + a few monthly), and delete the rest
    assert res["kept"] < 60
    assert res["deleted"] > 0
    remaining = list(root.glob("mongoose-backup-*.tar.gz"))
    assert len(remaining) == res["kept"]
    # the newest ROTA_DAILY days must all be kept
    for i in range(B.ROTA_DAILY):
        dt = now - timedelta(days=i)
        assert (root / f"mongoose-backup-daily-{dt.strftime('%Y%m%dT%H%M%S')}.tar.gz").exists()


def test_safe_extract_rejects_traversal():
    _fresh()
    from mongoose import backup as B
    import io
    # build a malicious tar with a traversal member
    bad = tempfile.mktemp(suffix=".tar.gz")
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        data = b"pwned"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    dest = tempfile.mkdtemp()
    import pytest
    with tarfile.open(bad, "r:gz") as tar:
        with pytest.raises(ValueError):
            B._safe_extract(tar, __import__("pathlib").Path(dest))
