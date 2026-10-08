"""Tests for brain preservation: a code update must never disrupt the brain (conversation, memory,
identity). Snapshots are consistent and taken while running; integrity is verified before and after a
migration; the brain can be rolled back independently of the code."""
from __future__ import annotations

import os
import sqlite3
import tempfile


def _fresh_with_brain():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "data", "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = os.path.join(d, "data")
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    from mongoose.db import init_db
    init_db()
    from mongoose.services.memory import ensure_conversation, add_message, DEFAULT_CONVERSATION_ID
    ensure_conversation(DEFAULT_CONVERSATION_ID)
    add_message(DEFAULT_CONVERSATION_ID, "user", "remember the gate code is 4417")
    # identity continuity file
    with open(os.path.join(d, "data", "session_salt"), "w") as f:
        f.write("SALT-XYZ")
    return d


def test_verify_brain_healthy():
    _fresh_with_brain()
    from mongoose import brain_preservation as BP
    v = BP.verify_brain()
    assert v["ok"] is True
    assert v["integrity"] == "ok"
    assert v["tables"]["messages"] >= 1


def test_snapshot_is_consistent_and_includes_salt():
    _fresh_with_brain()
    from mongoose import brain_preservation as BP
    snap = BP.snapshot_brain("test")
    assert snap["ok"] is True
    assert snap["db_bytes"] > 0
    assert "session_salt_snapshot" in snap
    # the snapshot itself must pass verification
    import pathlib
    snap_db = list(pathlib.Path(snap["snapshot_dir"]).glob("*.sqlite3"))[0]
    assert BP.verify_brain(snap_db)["ok"] is True


def test_damaged_brain_is_detected_and_restorable():
    """The core guarantee: if a migration damages the brain, it's caught, and the brain rolls back
    independently of code, with content intact."""
    _fresh_with_brain()
    from mongoose import brain_preservation as BP
    from mongoose.db import connect
    from mongoose.services.memory import recent_messages, DEFAULT_CONVERSATION_ID

    snap = BP.snapshot_brain("preupdate")
    assert snap["ok"]

    # simulate a destructive migration
    with connect() as c:
        c.execute("DROP TABLE messages")
    assert BP.verify_brain()["ok"] is False  # detected

    # roll the brain back
    r = BP.restore_brain(snap["snapshot_dir"])
    assert r["ok"] is True
    assert BP.verify_brain()["ok"] is True
    # the actual memory content survived
    msgs = recent_messages(DEFAULT_CONVERSATION_ID, limit=5)
    assert any("gate code" in m["content"] for m in msgs)


def test_restore_refuses_a_corrupt_snapshot():
    """Restoring from a bad snapshot must not overwrite the live brain (can't make things worse)."""
    d = _fresh_with_brain()
    from mongoose import brain_preservation as BP
    # make a fake snapshot dir with a corrupt db
    bad = os.path.join(d, "data", "brain_snapshots", "bad-snap")
    os.makedirs(bad, exist_ok=True)
    with open(os.path.join(bad, "m.sqlite3"), "w") as f:
        f.write("this is not a sqlite database")
    r = BP.restore_brain(bad)
    assert r["ok"] is False
    # live brain still fine
    assert BP.verify_brain()["ok"] is True


def test_preserve_around_update_snapshots_and_verifies():
    _fresh_with_brain()
    from mongoose import brain_preservation as BP
    rec = BP.preserve_around_update()
    assert rec["ok"] is True
    assert rec["pre_verify"]["ok"] is True
    assert rec["snapshot"]["ok"] is True


def test_snapshot_works_while_db_has_open_connection():
    """The snapshot uses SQLite's online backup, so it must succeed even with a live connection
    writing , the server doesn't have to stop for the brain to be preserved."""
    _fresh_with_brain()
    from mongoose import brain_preservation as BP

    paths = BP.brain_paths()
    live = sqlite3.connect(str(paths["db"]))
    try:
        live.execute("BEGIN")
        live.execute("INSERT INTO app_state(key,value,updated_at) VALUES('x','y','now')")
        # do NOT commit yet; snapshot while a write txn is open
        snap = BP.snapshot_brain("concurrent")
        assert snap["ok"] is True
    finally:
        live.rollback()
        live.close()


def test_list_and_prune_snapshots():
    _fresh_with_brain()
    from mongoose import brain_preservation as BP
    for i in range(3):
        BP.snapshot_brain(f"s{i}")
    rows = BP.list_snapshots()
    assert len(rows) >= 3
    pr = BP.prune_snapshots(keep=2)
    assert pr["kept"] == 2
    assert len(BP.list_snapshots()) == 2
