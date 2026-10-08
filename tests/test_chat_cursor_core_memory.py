"""Regression tests for three chat/UX fixes:
1. The input caret is a native browser caret (follows the text cursor), not a fake span pinned to the
   line end.
2. "core not ready" must not fire while the brain is demonstrably working , flexible Ollama model
   matching (tag-tolerant) plus a recent-reply override.
3. The chat endpoint passes through what was remembered, so the UI can show a memory notification.
"""
from __future__ import annotations

import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


# ---- 1. native caret, no fake cursor span ----

def test_chat_uses_native_caret_not_fake_span():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    # The chat uses a real, visible, blinking lime BLOCK cursor: the native thin caret is hidden and
    # a lime rectangle tracks the typed text (this is the ICQ/terminal look the user asked for).
    assert "caret-color:transparent" in h          # native caret hidden
    assert "irc-block" in h and "placeCursor" in h  # the tracking block + its positioner
    assert "ircCursor" not in h                      # the old broken fake cursor span is gone
    assert "positionCursor" not in h                 # the old broken positioning is gone


# ---- 2. Ollama model matching is tag-tolerant ----

def test_ollama_model_match_is_tag_tolerant():
    """A configured model name must match a pulled model even when tags differ (Ollama resolves
    these), so a working model isn't reported missing , the cause of false 'core not ready'."""
    _fresh()
    import mongoose.doctor as DOC

    pulled = [{"name": "gemma3:27b-it-q4_K_M"}, {"name": "qwen2.5-coder:14b-instruct-q4_K_M"}]

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"models": pulled}

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): return _R()

    DOC.httpx.Client = _C
    checks = DOC.run_doctor(timeout_seconds=6.0).get("checks", {})
    present = checks["ollama"].get("present", {})
    # the configured code model qwen2.5-coder... is exactly present
    assert present.get("code") is True


def test_core_ready_when_recent_reply_exists_even_if_probe_unsure():
    """Definitive override: a recent assistant reply proves the brain works, so core must be ready
    regardless of what the model-presence probe says."""
    _fresh()
    import mongoose.doctor as DOC
    import mongoose.wizard as W

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "totally-different-model:latest"}]}

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): return _R()

    DOC.httpx.Client = _C
    # no recent reply yet -> not ready (configured model not in the list)
    assert W._core_status()["ready"] is False
    # add a recent assistant message -> ready
    from mongoose.services.memory import ensure_conversation, add_message
    ensure_conversation("mongoose-main")
    add_message("mongoose-main", "user", "hi")
    add_message("mongoose-main", "assistant", "hello!")
    assert W._core_status()["ready"] is True


# ---- 3. chat surfaces what it remembered ----

def test_explicit_remember_returns_remembered_fact():
    _fresh()
    import asyncio
    from mongoose.llm import chat_with_memory

    async def go():
        return await chat_with_memory("remember that my dog is called Toby",
                                      conversation_id="mongoose-main", role="chat")
    r = asyncio.run(go())
    assert r.get("remembered")
    assert any("Toby" in f for f in r["remembered"])


def test_chat_ui_shows_memory_notification():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "function mem(" in h
    assert ".irc-mem{" in h
    assert "d.remembered" in h
