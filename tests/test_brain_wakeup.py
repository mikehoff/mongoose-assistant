"""Brain wake-up self-check: the brain must come back with FULL working memory after a
restart or reinstall — WAL recovery, missing-table repair, and FTS (memory-search) rebuild.
All against the real DB and real memory layer; no mocks."""
import os
import sqlite3
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.db as DB
    DB._SCHEMA_READY.clear()
    DB.init_db()
    return d


def test_wakeup_clean_brain_is_noop():
    """A healthy fresh brain needs no repair beyond the routine WAL checkpoint."""
    _fresh()
    from mongoose.brain_wakeup import wake_up
    rep = wake_up()
    assert rep["ok"], rep["problems"]
    assert rep["missing_tables"] == []
    assert rep["fts"]["desynced"] == []
    # Only the routine checkpoint, no table recreation, no fts rebuild.
    assert all(not a.startswith("rebuilt_fts") and not a.startswith("recreated") for a in rep["actions"])


def test_wakeup_rebuilds_desynced_memory_search():
    """The core guarantee: if the memory FTS index is wiped (as a partial restore does),
    wake_up rebuilds it so memory SEARCH works again — not just that the DB opens."""
    _fresh()
    from mongoose.services import memory as M
    from mongoose.db import connect
    M.remember_fact("Alex's dog is named Toby")
    M.remember_fact("Alex lives in Anytown")
    assert M.search_facts("Toby")  # works before

    # Wipe the FTS index but keep the base rows (the silent-broken-search failure mode)
    with connect() as con:
        con.execute("INSERT INTO memory_fts(memory_fts) VALUES('delete-all')")
    assert M.search_facts("Toby") == []  # search is now silently dead

    from mongoose.brain_wakeup import wake_up
    rep = wake_up()
    assert "memory_fts" in rep["fts"]["rebuilt"]
    assert M.search_facts("Toby")  # recall restored
    assert len(M.all_facts()) == 2   # no data lost


def test_wakeup_recovers_committed_wal_rows():
    """A hard kill can strand committed rows in the WAL. wake_up checkpoints so nothing is lost."""
    d = _fresh()
    from mongoose.services import memory as M
    M.remember_fact("fact before kill")
    db = os.path.join(d, "m.sqlite3")
    # Write a committed row directly and DON'T checkpoint (simulate a kill with WAL pending)
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("INSERT INTO memory_facts(fact, source, dedupe_key, scope, created_at, updated_at) "
                "VALUES ('stranded in wal','t','k_wal','owner','2026-01-01','2026-01-01')")
    raw.commit()
    raw.close()

    from mongoose.brain_wakeup import wake_up
    rep = wake_up()
    assert "wal_checkpoint" in rep["actions"]
    facts = [f["fact"] for f in M.all_facts()]
    assert "stranded in wal" in facts, "committed WAL row was lost on wake-up"


def test_wakeup_recreates_missing_table():
    """A partial restore can drop a table. wake_up re-materialises it without touching other data."""
    _fresh()
    from mongoose.services import memory as M
    from mongoose.db import connect
    M.remember_fact("keep me")
    # Drop a non-memory table to simulate a partial restore
    with connect() as con:
        con.execute("DROP TABLE IF EXISTS weather_cache")
    from mongoose.brain_wakeup import wake_up
    rep = wake_up()
    assert "weather_cache" in rep.get("missing_tables", [])
    # memory untouched
    assert any("keep me" in f["fact"] for f in M.all_facts())
    # table is back
    with connect() as con:
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weather_cache'").fetchone()


def test_expected_tables_match_init_db():
    """EXPECTED_TABLES must exactly equal what init_db creates, or wake_up will falsely flag a
    'missing' table on every boot and needlessly re-run init_db. This guards that contract."""
    _fresh()
    from mongoose.db import connect
    from mongoose.brain_wakeup import EXPECTED_TABLES
    with connect() as con:
        actual = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = [t for t in EXPECTED_TABLES if t not in actual]
    assert not missing, f"EXPECTED_TABLES lists tables init_db doesn't create: {missing}"


def test_wakeup_summary_is_check_only():
    """wakeup_summary must not mutate (no rebuild) — it's the read-only status."""
    _fresh()
    from mongoose.services import memory as M
    from mongoose.db import connect
    M.remember_fact("x")
    with connect() as con:
        con.execute("INSERT INTO memory_fts(memory_fts) VALUES('delete-all')")
    from mongoose.brain_wakeup import wakeup_summary
    s = wakeup_summary()
    # It reports the desync but does NOT rebuild (search still broken after a check-only call)
    assert "memory_fts" in s["fts"]["desynced"]
    assert M.search_facts("x") == []
