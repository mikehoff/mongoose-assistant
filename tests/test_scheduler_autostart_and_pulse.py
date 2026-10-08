"""Tests for v0.52.17 additions: the scheduler auto-starting with the server (item 3), the chat-tab
attention pulse (item 4), and that the reconciled memory fixes hold (items 6)."""
from __future__ import annotations

import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


# ---- item 3: scheduler auto-starts in the server lifespan ----

def test_scheduler_starts_in_lifespan_by_default():
    _fresh()
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "true"
    import mongoose.services.scheduler as SCH
    from mongoose.app import app, lifespan
    started = {"v": False}

    async def fake_run():
        started["v"] = True
        while True:
            await asyncio.sleep(0.01)

    orig = SCH.run_scheduler
    SCH.run_scheduler = fake_run
    try:
        async def go():
            async with lifespan(app):
                await asyncio.sleep(0.05)
            return started["v"]
        assert asyncio.run(go()) is True
    finally:
        SCH.run_scheduler = orig


def test_scheduler_optout_respected():
    _fresh()
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    import mongoose.services.scheduler as SCH
    from mongoose.app import app, lifespan
    started = {"v": False}

    async def fake_run():
        started["v"] = True
        while True:
            await asyncio.sleep(0.01)

    orig = SCH.run_scheduler
    SCH.run_scheduler = fake_run
    try:
        async def go():
            async with lifespan(app):
                await asyncio.sleep(0.05)
            return started["v"]
        assert asyncio.run(go()) is False
    finally:
        SCH.run_scheduler = orig
        os.environ.pop("MONGOOSE_INPROCESS_SCHEDULER", None)


# ---- item 4: chat tab pulse ----

def test_chat_tab_has_attention_pulse():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "@keyframes chatGlow" in h and "@keyframes chatBeat" in h
    assert 'b.classList.add("chat-pulse")' in h
    assert 'btn.classList.add("seen")' in h          # stops once opened
    assert "nav button.chat-pulse, nav button.chat-pulse::after{ animation:none" in h  # reduced-motion


def test_chat_pulse_does_not_break_chat_lime_css():
    """The pulse must not have re-trapped or removed the terminal CSS (the recurring failure mode)."""
    import re
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    style = next(s for s in re.findall(r"<style>(.*?)</style>", h, re.S) if ".irc-win{" in s)
    irc = style.index(".irc-win{")
    for m in re.finditer(r"@media\s*\(prefers-reduced-motion[^{]*\{", style):
        depth = 0
        i = m.end() - 1
        end = None
        while i < len(style):
            if style[i] == "{":
                depth += 1
            elif style[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        assert not (end and m.start() < irc < end)
    assert "#39ff14" in style


# ---- item 6 reconciliation: the two audit bugs stay fixed ----

def test_broad_pref_does_not_supersede_narrow():
    _fresh()
    from mongoose.services import memory as M
    from mongoose.memory_intent import detect_memory_intent, preference_to_fact
    M.remember_fact("The user dislikes pineapple on pizza.", source="test")
    mi = detect_memory_intent("I like pineapple")
    res = M.remember_preference(preference_to_fact(mi), mi.polarity, mi.target)
    assert res.get("superseded") is None
    facts = [f["fact"] for f in M.all_facts()]
    assert "The user likes pineapple." in facts
    assert "The user dislikes pineapple on pizza." in facts


def test_polarity_aware_forget_keeps_opposite():
    _fresh()
    from mongoose.llm import chat_with_memory
    from mongoose.services import memory as M
    M.remember_fact("The user dislikes anchovies.", source="test")
    asyncio.run(chat_with_memory("forget that I liked anchovies", conversation_id="c", role="chat"))
    # the dislike must survive , the forget was about a 'liked' anchovies memory that doesn't exist
    assert any("dislikes anchovies" in f["fact"] for f in M.all_facts())


def test_ha_temporary_dns_failure_is_actionable():
    _fresh()
    # we can't force the exact OS error here, but the message branch must exist for it
    import inspect
    import mongoose.setup_wizard as SW
    src = inspect.getsource(SW.verify_home_assistant)
    assert "temporary failure in name resolution" in src


def test_scheduler_shutdown_does_not_hang_with_app_boot():
    """Regression: the in-process scheduler must not deadlock server shutdown. Booting the app via
    TestClient (which starts the scheduler in lifespan), hitting an endpoint that itself touches the
    DB, then exiting (which cancels the scheduler) must complete promptly , not hang on the cancel."""
    import os
    import tempfile
    import time
    os.environ["MONGOOSE_DB_PATH"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "true"
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    start = time.time()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    # the whole enter+exit cycle (incl. cancelling the scheduler) must be quick
    assert time.time() - start < 10, "server shutdown hung on the in-process scheduler"


def test_scheduler_running_true_for_live_loop_before_any_job():
    """Regression: _scheduler_running() must report True for a freshly-started loop via its per-tick
    heartbeat, BEFORE any job has fired. Two bugs caused false negatives: (1) it imported from the
    wrong path (.db vs ..db) so it always threw and returned False, and (2) it only checked recent
    job runs, so a loop with no fired jobs read as not-running."""
    import asyncio
    import os
    import tempfile
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    from mongoose.services.scheduler import run_scheduler, _scheduler_running

    async def go():
        task = asyncio.create_task(run_scheduler(tick_seconds=60))
        await asyncio.sleep(0.4)  # boots + writes heartbeat; no job is due yet
        running = _scheduler_running()
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except BaseException:
            pass
        return running

    assert asyncio.run(go()) is True


def test_scheduler_running_false_when_stale():
    """A stale heartbeat (loop died) with no recent job runs must report not-running , the honest
    signal, so we don't imply jobs will fire when they won't."""
    import os
    import tempfile
    from datetime import datetime, timezone, timedelta
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db, set_app_state
    init_db()
    set_app_state("scheduler_heartbeat", (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    from mongoose.services.scheduler import _scheduler_running
    assert _scheduler_running() is False


def test_scheduler_endpoint_reports_running_for_live_loop():
    """End to end: the cockpit Scheduler tab endpoint must show running=True when the in-process loop
    is up (this is what the user looks at)."""
    import os
    import tempfile
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "true"
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    with TestClient(app) as client:
        # the lifespan started the loop; give it a tick to write the heartbeat
        import time
        time.sleep(0.5)
        r = client.get("/cockpit/scheduler")
        assert r.status_code == 200
        assert r.json()["running"] is True
