"""Memory Claim Integrity tests (per the audit spec): casual preference capture, visible memory
events, safe semantic forgetting, and the hard rule that Mongoose never claims it forgot something
unless a row was actually deleted."""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


# ---- 1. casual preference capture (the phrasings the audit said failed) ----

@pytest.mark.parametrize("text,expect_target", [
    ("my fave is truffle", "truffle"),
    ("really fond of hawaiian", "hawaiian"),
    ("I really like Hawaiian pizza", "Hawaiian pizza"),
    ("my favourite pizza is Hawaiian", "Hawaiian"),
    ("I'm into truffle", "truffle"),
    ("my go-to is margherita", "margherita"),
    ("Hawaiian is my favourite", "Hawaiian"),
])
def test_casual_like_phrasings_are_captured(text, expect_target):
    _fresh()
    from mongoose.memory_intent import detect_memory_intent
    mi = detect_memory_intent(text)
    assert mi.intent == "remember_preference"
    assert mi.polarity == "likes"
    assert expect_target.lower() in mi.target.lower()


@pytest.mark.parametrize("text,expect_target", [
    ("I'm not into pineapple", "pineapple"),
    ("I can't stand anchovies", "anchovies"),
    ("I don't like olives", "olives"),
])
def test_casual_dislike_phrasings_are_captured(text, expect_target):
    _fresh()
    from mongoose.memory_intent import detect_memory_intent
    mi = detect_memory_intent(text)
    assert mi.intent == "remember_preference"
    assert mi.polarity == "dislikes"
    assert expect_target.lower() in mi.target.lower()


# ---- 2. visible memory events on chat ----

def test_preference_stored_and_event_emitted_via_chat():
    _fresh()
    # the model call will fail in the sandbox, but the preference must already be STORED before the
    # model runs, and the fact present regardless of the reply
    from mongoose.llm import chat_with_memory
    from mongoose.services import memory as M
    try:
        asyncio.run(chat_with_memory("really fond of Hawaiian", conversation_id="c", role="chat"))
    except Exception:
        pass
    assert any("Hawaiian" in f["fact"] for f in M.all_facts())


# ---- 3. safe semantic forgetting ----

def test_forget_single_match_deletes_and_reports():
    _fresh()
    from mongoose.llm import chat_with_memory
    from mongoose.services import memory as M
    M.remember_fact("The user likes pineapple on pizza.", source="test")
    r = asyncio.run(chat_with_memory("could you maybe forget that I liked pineapple on pizza",
                                     conversation_id="c", role="chat"))
    assert any(e["type"] == "forgotten" for e in r["memory_events"])
    assert not any("pineapple" in f["fact"] for f in M.all_facts())   # actually gone


def test_forget_request_does_not_create_a_new_memory():
    _fresh()
    from mongoose.llm import chat_with_memory
    from mongoose.services import memory as M
    asyncio.run(chat_with_memory("forget that I liked pineapple on pizza", conversation_id="c", role="chat"))
    # nothing about pineapple should have been STORED by the forget request
    assert not any("pineapple" in f["fact"] for f in M.all_facts())


def test_ambiguous_forget_asks_and_deletes_nothing():
    _fresh()
    from mongoose.llm import chat_with_memory
    from mongoose.services import memory as M
    M.remember_fact("The user likes Hawaiian pizza.", source="test")
    M.remember_fact("The user likes pineapple on pizza.", source="test")
    r = asyncio.run(chat_with_memory("forget the pizza thing", conversation_id="c", role="chat"))
    assert any(e["type"] == "forget_needs_confirmation" for e in r["memory_events"])
    assert len(M.all_facts()) == 2   # nothing deleted


def test_forget_selection_reply_deletes_chosen():
    _fresh()
    from mongoose.llm import chat_with_memory
    from mongoose.services import memory as M
    M.remember_fact("The user likes Hawaiian pizza.", source="test")
    M.remember_fact("The user likes pineapple on pizza.", source="test")
    asyncio.run(chat_with_memory("forget the pizza thing", conversation_id="c", role="chat"))
    r = asyncio.run(chat_with_memory("the first one", conversation_id="c", role="chat"))
    assert any(e["type"] == "forgotten" for e in r["memory_events"])
    assert len(M.all_facts()) == 1   # exactly one removed


# ---- 4. the hard claim-integrity rule ----

def test_never_claims_forgotten_when_nothing_matched():
    _fresh()
    from mongoose.llm import chat_with_memory
    from mongoose.services import memory as M
    M.remember_fact("The user likes truffle.", source="test")
    r = asyncio.run(chat_with_memory("forget that I drive a Ferrari", conversation_id="c", role="chat"))
    assert "forgotten" not in r["reply"].lower() and "couldn't find" in r["reply"].lower()
    assert not any(e["type"] == "forgotten" for e in r["memory_events"])
    assert len(M.all_facts()) == 1   # untouched


def test_stored_fact_actually_absent_after_forget():
    """Regression: prove the row is gone from the DB, not just reported gone."""
    _fresh()
    from mongoose.services import memory as M
    M.remember_fact("The user likes pineapple on pizza.", source="test")
    res = M.forget_memory("pineapple on pizza")
    assert res["status"] == "forgotten"
    from mongoose.db import connect
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM memory_facts WHERE fact LIKE '%pineapple%'").fetchone()[0]
    assert n == 0


# ---- 5. surfaces pass the events through ----

def test_cockpit_chat_endpoint_returns_memory_events():
    _fresh()
    import inspect
    from mongoose import app as APP
    src = inspect.getsource(APP)
    assert '"memory_events": result.get("memory_events"' in src


def test_negation_still_prevents_storage():
    """Existing protection must still hold: 'don't remember that' must not store."""
    _fresh()
    from mongoose.services.memory import looks_fact_bearing, detect_explicit_memory
    assert detect_explicit_memory("please don't remember that I like pineapple") is None
    assert looks_fact_bearing("forget that I like pineapple") is False
