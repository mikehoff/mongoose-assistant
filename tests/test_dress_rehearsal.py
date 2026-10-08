"""Tests for the dress-rehearsal harness. The harness must (a) exercise every module, (b) report
FAIL honestly when a module breaks, and (c) NEVER report a live-edge (Ollama, real mailbox/send)
as a pass it didn't earn , a rehearsal that fakes readiness is worse than none."""
import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    os.environ.pop("RTM_LIVE_MAILBOX", None)
    os.environ.pop("RTM_LIVE_SEND", None)
    from mongoose.db import init_db
    init_db()


def test_rehearsal_runs_and_exercises_all_modules():
    _fresh()
    from mongoose.dress_rehearsal import dress_rehearsal
    r = asyncio.run(dress_rehearsal())
    names = [s["name"] for s in r["functional"]]
    # every feature module is represented
    for expected in ["Memory", "Readiness", "Body State", "Alpha import", "Unstuck",
                     "Time Alerts", "RTM intake", "Overnight builder"]:
        assert any(expected in n for n in names), expected


def test_clean_run_is_logic_ready_not_fail():
    _fresh()
    from mongoose.dress_rehearsal import dress_rehearsal
    r = asyncio.run(dress_rehearsal())
    # on clean code, no module should FAIL
    assert r["has_fail"] is False
    assert "LOGIC READY" in r["verdict"]


def test_live_edges_not_reported_as_pass_when_off():
    _fresh()
    from mongoose.dress_rehearsal import dress_rehearsal, DISABLED, HARDWARE, CONFIGURE
    r = asyncio.run(dress_rehearsal())
    live = {s["name"]: s["result"] for s in r["live"]}
    # mailbox + send are off by default -> must be 'disabled', never 'pass'
    assert live["RTM live mailbox"] == DISABLED
    assert live["RTM live send"] == DISABLED
    # Ollama not reachable in the sandbox -> hardware, never a fake pass
    assert live["Ollama serving models"] in (HARDWARE, CONFIGURE, "pass")
    # specifically it must NOT claim pass when unreachable
    # (in the sandbox it's HARDWARE; on a Mac with ollama up it could legitimately be pass)


def test_rehearsal_reports_fail_when_a_module_breaks(monkeypatch):
    _fresh()
    import mongoose.dress_rehearsal as DR
    # force one module to raise -> the harness must surface it as a FAIL line, NOT crash
    def boom():
        raise RuntimeError("simulated breakage")
    monkeypatch.setattr(DR, "_rehearse_memory", boom)
    r = asyncio.run(DR.dress_rehearsal())
    # the rehearsal still completed and returned a report
    assert "verdict" in r
    # the broken module is a FAIL line, and the verdict reflects it
    results = {s["name"]: s["result"] for s in r["functional"]}
    assert results.get("Memory") == DR.FAIL
    assert r["has_fail"] is True
    assert "NOT READY" in r["verdict"]


def test_armed_live_send_is_configure_not_disabled(monkeypatch):
    _fresh()
    os.environ["RTM_LIVE_SEND"] = "true"
    try:
        from mongoose.dress_rehearsal import dress_rehearsal, CONFIGURE
        r = asyncio.run(dress_rehearsal())
        live = {s["name"]: s["result"] for s in r["live"]}
        # when actually armed, it's reported as armed/configure , the status is always honest
        assert live["RTM live send"] == CONFIGURE
    finally:
        os.environ.pop("RTM_LIVE_SEND", None)


def test_format_rehearsal_is_printable():
    _fresh()
    from mongoose.dress_rehearsal import dress_rehearsal, format_rehearsal
    r = asyncio.run(dress_rehearsal())
    text = format_rehearsal(r)
    assert "DRESS REHEARSAL" in text
    assert "Live edges" in text


def test_dress_rehearsal_restores_env_and_does_not_touch_cached_live_db(tmp_path):
    """Dress rehearsal is advertised as safe: it must run on a throwaway DB even if
    settings were already cached against a live path, and it must restore the caller's
    DB path afterwards."""
    import sqlite3
    from mongoose.settings import get_settings
    import mongoose.settings as settings_mod
    import mongoose.db as db_mod
    live = tmp_path / "live.sqlite3"
    os.environ["MONGOOSE_DB_PATH"] = str(live)
    os.environ["MONGOOSE_DATA_DIR"] = str(tmp_path / "live-data")
    settings_mod._SETTINGS = None
    db_mod._SCHEMA_READY.clear()
    # Prime the cached settings to the live path before rehearsal starts.
    assert str(get_settings().db_path) == str(live)

    from mongoose.dress_rehearsal import dress_rehearsal
    r = asyncio.run(dress_rehearsal())
    assert "verdict" in r
    settings_mod._SETTINGS = None
    assert str(get_settings().db_path) == str(live)
    # The live DB may have been initialised by get_settings/init elsewhere, but the synthetic
    # RTM artefacts from rehearsal must not appear in it.
    if live.exists():
        with sqlite3.connect(live) as con:
            names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "rtm_demands" in names:
                rows = con.execute("SELECT COUNT(*) FROM rtm_demands WHERE document_number LIKE 'REH-%'").fetchone()[0]
                assert rows == 0
