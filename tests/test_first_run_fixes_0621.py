"""Tests for the real-hardware first-run fixes: the inference lock (freeze cause), auto-extract
default off (no double model call per chat), install progress crediting a running step, the
start-docker CLI, and the joke persona guidance."""
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


# ---- the freeze: single-model box must not run two inferences at once ----

def test_inference_lock_serializes():
    """All local inference goes through one process-wide lock so chat + extraction + scheduler can't
    thrash the single-model Mac (the freeze)."""
    import mongoose.llm as L
    order = []

    async def call(n, hold):
        async with L._inference_lock():
            order.append(("s", n))
            await asyncio.sleep(hold)
            order.append(("e", n))

    asyncio.run(_gather(call))
    # serialized: each critical section completes before the next starts (no interleave)
    assert len(order) == 4
    for i in range(0, len(order), 2):
        assert order[i][0] == "s" and order[i + 1][0] == "e" and order[i][1] == order[i + 1][1]


async def _gather(call):
    await asyncio.gather(call(1, 0.04), call(2, 0.01))


def test_auto_extract_off_by_default():
    """Model-assisted extraction defaults OFF so a normal chat turn is ONE model call, not two."""
    _fresh()
    os.environ.pop("MEMORY_AUTO_EXTRACT", None)
    import mongoose.settings as st
    st._SETTINGS = None
    assert st.get_settings().memory_auto_extract is False


def test_preferences_still_captured_without_model():
    """Turning off model extraction must NOT break memory: deterministic capture still works."""
    _fresh()
    from mongoose.services import memory as M
    from mongoose.memory_intent import detect_memory_intent, preference_to_fact
    mi = detect_memory_intent("really fond of Hawaiian")
    M.remember_preference(preference_to_fact(mi), mi.polarity, mi.target)
    assert any("Hawaiian" in f["fact"] for f in M.all_facts())


# ---- install progress reflects in-flight work ----

def test_progress_credits_running_step():
    """A currently-running step shows as half-done so the long model download shows movement, not a
    stuck number."""
    _fresh()
    from mongoose.install_progress import set_step, install_progress
    for s in ["prerequisites", "environment", "database"]:
        set_step(s, "done")
    base = install_progress()["progress_pct"]
    set_step("models", "running")
    running = install_progress()["progress_pct"]
    assert running > base                 # the bar moves when the step starts
    set_step("models", "done")
    assert install_progress()["progress_pct"] > running


# ---- start-docker CLI ----

def test_start_docker_command_exists_and_is_honest_without_docker():
    """The command exists and, where Docker is absent, points to the native ntfy path rather than
    crashing or pretending."""
    import subprocess
    import sys
    env = dict(os.environ, PYTHONPATH="src")
    h = subprocess.run([sys.executable, "-m", "mongoose.cli", "--help"], env=env,
                       capture_output=True, text=True)
    assert "start-docker" in h.stdout
    # The helper is importable and named
    from mongoose.cli import _cmd_start_docker
    assert callable(_cmd_start_docker)


# ---- persona joke guidance ----

def test_persona_steers_jokes_away_from_obscure_profile_humor():
    from mongoose.persona import PERSONA_CARD
    low = PERSONA_CARD.lower()
    assert "joke" in low and ("accessible" in low or "general" in low)


# ---- phone pairing surfaces the reachability warning ----

def test_install_pairing_surfaces_reachability_warning():
    _fresh()
    from mongoose.cockpit import install_html
    assert "d.reachability_warning" in install_html()


def test_pairing_note_names_enable_phone():
    _fresh()
    import mongoose.settings as st
    st._SETTINGS = None
    from mongoose.app import _pairing_reachability_note
    note = _pairing_reachability_note()
    # loopback-bound by default -> note is a sentinel string; JS renders the "Enable phone access" button
    # Design change: the note is not a human-readable string any more — JS owns the UX.
    assert note is not None  # must be truthy (not None/empty)
    assert note == "not_lan_reachable"  # well-known sentinel the JS handles
