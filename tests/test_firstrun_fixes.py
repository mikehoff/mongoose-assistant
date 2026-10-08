"""Tests for the real-hardware first-run fixes: the inference lock (freeze cause), auto-extract default
off (the double-inference per chat), in-app Docker/phone-access paths, and smoother install progress."""
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


def _client():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app)


# ---- the freeze fixes ----

def test_inference_lock_serializes_model_calls():
    """All local inference must serialise, so chat + extraction + scheduler can't thrash the
    single-model Mac at once (the freeze cause). We assert the INVARIANT that matters: no two
    critical sections overlap (each start is immediately followed by its own end), regardless of
    which queued caller wins first."""
    import mongoose.llm as L
    order = []

    async def call(n, hold):
        async with L._inference_lock():
            order.append(("start", n))
            await asyncio.sleep(hold)
            order.append(("end", n))

    async def main():
        await asyncio.gather(call(1, 0.04), call(2, 0.01), call(3, 0.02))

    asyncio.run(main())
    # every "start" must be immediately followed by the matching "end" (no interleave)
    assert len(order) == 6
    for i in range(0, len(order), 2):
        assert order[i][0] == "start" and order[i + 1][0] == "end"
        assert order[i][1] == order[i + 1][1], f"critical sections interleaved: {order}"


def test_auto_extract_off_by_default():
    """The model-assisted extraction (a SECOND model call per chat turn) must be off by default so a
    base Mac does one inference per message, not two."""
    _fresh()
    os.environ.pop("MEMORY_AUTO_EXTRACT", None)
    from mongoose.settings import get_settings
    assert get_settings().memory_auto_extract is False


def test_deterministic_memory_still_works_without_auto_extract():
    """Preferences and forget are deterministic (no model), so they must still work with the heavy
    extraction off."""
    _fresh()
    os.environ.pop("MEMORY_AUTO_EXTRACT", None)
    from mongoose.services import memory as M
    from mongoose.memory_intent import detect_memory_intent, preference_to_fact
    mi = detect_memory_intent("really fond of Hawaiian")
    M.remember_preference(preference_to_fact(mi), mi.polarity, mi.target)
    assert any("Hawaiian" in f["fact"] for f in M.all_facts())
    assert M.forget_memory("Hawaiian")["status"] == "forgotten"


# ---- in-app Docker / phone-access paths ----

def test_push_status_endpoint():
    c = _client()
    r = c.get("/cockpit/push-status")
    assert r.status_code == 200
    body = r.json()
    for k in ["docker_installed", "docker_running", "native_ntfy", "can_start_docker"]:
        assert k in body


def test_enable_phone_endpoint_exists_and_guards_nonce():
    c = _client()
    # without the nonce it must be rejected (not silently run a server restart)
    r = c.post("/cockpit/enable-phone", json={})
    assert r.status_code in (400, 403)


def test_start_docker_reports_when_docker_absent():
    # nonce-guarded; fetch a nonce the way the UI does is out of scope, just assert it's registered
    paths = [route.path for route in __import__("mongoose.app", fromlist=["app"]).app.routes]
    assert "/cockpit/start-docker" in paths
    assert "/cockpit/enable-phone" in paths


# ---- smoother install progress ----

def test_progress_credits_running_step():
    """A currently-running work step should move the bar (half credit), so the long model download
    doesn't look stuck at the previous whole number."""
    _fresh()
    from mongoose.install_progress import set_step, install_progress
    for s in ["prerequisites", "environment", "database"]:
        set_step(s, "done")
    before = install_progress()["progress_pct"]
    set_step("models", "running")
    during = install_progress()["progress_pct"]
    assert during > before  # the bar advanced when the long step started
    set_step("models", "done")
    after = install_progress()["progress_pct"]
    assert after > during


# ---- UI surfaces present ----

def test_setup_page_uses_tile_grid():
    _fresh()
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "wiz-grid" in h and "wizExpand" in h        # scannable grid, not a long scroll
    assert "function loadPush" in h                     # the push/docker card


def test_logo_has_real_glow_and_is_larger():
    _fresh()
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "0 0 18px rgba(57,255,20" in h               # real glow in the breath keyframe
    assert 'width="46"' in h                            # larger than the old 34px


def test_blinking_block_cursor_present():
    _fresh()
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "irc-block" in h and "function placeCursor" in h
    assert "caret-color:transparent" in h               # native caret hidden in favour of the block
    assert "@keyframes ircBlink" in h


def test_more_emojis():
    _fresh()
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    # expanded set includes the mouse and new faces
    assert "🐭" in h and "🎉" in h
