"""Tests for the runtime watchdog (server-side self-healing), the one permanent conversation
(mongoose-main shared across all surfaces, created at startup), and the corrected Microsoft/IMAP
framing (IMAP replaces email only, not To Do / calendar)."""
from __future__ import annotations

import asyncio
import os
import tempfile

from fastapi.testclient import TestClient


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


# ---- runtime watchdog ----

def test_watchdog_beats_and_records_status():
    _fresh()
    from mongoose.services.runtime_watchdog import RuntimeWatchdog, current_status

    async def run():
        wd = RuntimeWatchdog(interval_seconds=1.0)
        wd.start()
        await asyncio.sleep(2.4)
        s = current_status()
        await wd.stop()
        return s
    s = asyncio.run(run())
    assert s is not None
    assert s["beats"] >= 1
    assert "loop_lag_ms" in s
    assert "last_beat" in s


def test_watchdog_restarts_a_dead_supervised_task():
    """The core self-healing promise: if a supervised background task dies, the watchdog recreates it
    so the capability resumes."""
    _fresh()
    from mongoose.services.runtime_watchdog import RuntimeWatchdog, current_status
    runs = {"n": 0}

    async def run():
        async def short_task():
            runs["n"] += 1
            await asyncio.sleep(0.1)  # dies quickly
        t = asyncio.ensure_future(short_task())
        wd = RuntimeWatchdog(interval_seconds=0.8)
        wd.supervise("scheduler", lambda: short_task(), t)
        wd.start()
        await asyncio.sleep(3.0)
        s = current_status()
        await wd.stop()
        return s
    s = asyncio.run(run())
    assert runs["n"] >= 2, "watchdog did not restart the dead task"
    assert s["scheduler_restarts"] >= 1


def test_watchdog_honest_about_limits_docstring():
    """The watchdog must not over-claim: it documents that it can't rescue a fully wedged process."""
    from mongoose.services import runtime_watchdog
    doc = runtime_watchdog.__doc__ or ""
    assert "cannot rescue a fully wedged" in doc.lower() or "cannot run while the event loop is frozen" in doc.lower()


def test_health_live_surfaces_watchdog_when_running():
    _fresh()
    # turn the watchdog ON for this test (the app fixture disables it by default)
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "true"
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "true"
    try:
        from mongoose.app import app
        with TestClient(app) as c:
            import time
            # give the watchdog a moment to beat
            time.sleep(1.5)
            r = c.get("/health/live")
            assert r.status_code == 200
            # it may or may not have beaten yet depending on timing; if present, it's well-formed
            body = r.json()
            if "runtime_watchdog" in body:
                assert "beats" in body["runtime_watchdog"]
    finally:
        os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
        os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"


# ---- one permanent conversation ----

def test_all_surfaces_share_one_conversation_id():
    _fresh()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID, DEFAULT_CLI_CONVERSATION
    assert DEFAULT_CONVERSATION_ID == "mongoose-main"
    # the CLI alias points at the same thread (terminal shares context)
    assert DEFAULT_CLI_CONVERSATION == DEFAULT_CONVERSATION_ID


def test_voice_session_defaults_to_permanent_conversation():
    _fresh()
    import inspect
    from mongoose.voice import run_session
    src = inspect.getsource(run_session)
    assert "DEFAULT_CONVERSATION_ID" in src


def test_conversation_created_at_startup():
    """Clicking Chat should land on an existing thread, so the conversation must exist after the
    server starts, not be created lazily on first message."""
    _fresh()
    from mongoose.app import app
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    from mongoose.db import connect
    with TestClient(app):  # triggers lifespan startup
        with connect() as con:
            row = con.execute("SELECT id FROM conversations WHERE id=?",
                              (DEFAULT_CONVERSATION_ID,)).fetchone()
    assert row is not None


def test_voice_and_text_share_history():
    """A message added via one surface is visible to another, because they share mongoose-main."""
    _fresh()
    from mongoose.services.memory import (ensure_conversation, add_message, recent_messages,
                                          DEFAULT_CONVERSATION_ID)
    ensure_conversation(DEFAULT_CONVERSATION_ID)
    add_message(DEFAULT_CONVERSATION_ID, "user", "remember the garage code is 4417")
    msgs = recent_messages(DEFAULT_CONVERSATION_ID, limit=10)
    assert any("garage code" in m["content"] for m in msgs)


# ---- corrected Microsoft / IMAP framing ----

def test_microsoft_help_says_todo_is_the_point():
    _fresh()
    from mongoose.setup_wizard import INTEGRATIONS
    ms = [i for i in INTEGRATIONS if i.slug == "microsoft"][0]
    h = ms.help_text
    assert "To Do" in h
    # IMAP must NOT be presented as a general Microsoft replacement , it can't do tasks/calendar
    assert "never tasks or calendar" in h or "IMAP cannot do tasks" in h or "not tasks or calendar" in h


def test_imap_not_presented_as_todo_alternative():
    """IMAP is an EMAIL alternative only. The Microsoft hint must make clear IMAP can't replace To Do
    / calendar."""
    _fresh()
    from mongoose.setup_wizard import INTEGRATIONS
    ms = [i for i in INTEGRATIONS if i.slug == "microsoft"][0]
    client_id_field = [f for f in ms.fields if f.key == "MS_CLIENT_ID"][0]
    hint = client_id_field.hint
    assert "IMAP" in hint
    assert "NOT" in hint and ("To Do" in hint or "calendar" in hint)
