"""Tests for referent-aware memory resolution (the general solution to 'same memory, different
words'). Covers the cross-synonym cases token matching can't (Hawaiian <-> pineapple on pizza),
general domain aliases (HA <-> Home Assistant, the apprenticeship <-> Examplecorp), the conservative
ambiguity policy (weak/ambiguous asks, never blind-deletes), and supersession of contradicting
preferences."""
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


def _facts(*texts):
    return [{"id": i + 1, "fact": t} for i, t in enumerate(texts)]


# ---- cross-synonym resolution (the whole point) ----

def test_forget_pineapple_resolves_to_stored_hawaiian():
    from mongoose.memory_resolver import resolve_forget
    d = resolve_forget("pineapple on pizza", _facts("The user likes Hawaiian pizza."))
    assert d["action"] == "delete"
    assert "Hawaiian" in d["candidate"]["fact"]


def test_forget_hawaiian_resolves_to_stored_pineapple():
    from mongoose.memory_resolver import resolve_forget
    d = resolve_forget("Hawaiian", _facts("The user likes pineapple on pizza."))
    assert d["action"] == "delete"
    assert "pineapple" in d["candidate"]["fact"]


# ---- general (non-food) domain aliases ----

@pytest.mark.parametrize("query,stored", [
    ("HA", "The user's Home Assistant runs on a Pi 5."),
    ("Home Assistant", "The user's HA automations control the lights."),
    ("the apprenticeship", "The user works at Examplecorp on the L4 course."),
    ("the rtm company", "The user directs Example Court RTM."),
])
def test_domain_aliases_resolve(query, stored):
    from mongoose.memory_resolver import resolve_forget
    d = resolve_forget(query, _facts(stored))
    assert d["action"] == "delete"


# ---- conservative ambiguity policy ----

def test_ambiguous_pineapple_asks_not_deletes():
    from mongoose.memory_resolver import resolve_forget
    d = resolve_forget("pineapple", _facts(
        "The user likes pineapple generally.",
        "The user dislikes pineapple juice."))
    assert d["action"] == "confirm"
    assert len(d["candidates"]) == 2


def test_weak_single_token_does_not_delete():
    """Only a common token shared ('pizza') must not be enough to delete."""
    from mongoose.memory_resolver import resolve_forget
    d = resolve_forget("pineapple", _facts("The user likes making pizza dough."))
    assert d["action"] == "none"


def test_no_match_returns_none():
    from mongoose.memory_resolver import resolve_forget
    d = resolve_forget("I drive a Ferrari", _facts("The user likes truffle."))
    assert d["action"] == "none"


# ---- end-to-end through memory.forget_memory + chat ----

def test_forget_memory_cross_synonym_deletes_in_db():
    _fresh()
    from mongoose.services import memory as M
    M.remember_fact("The user likes Hawaiian pizza.", source="test")
    res = M.forget_memory("pineapple on pizza")
    assert res["status"] == "forgotten"
    from mongoose.db import connect
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM memory_facts WHERE fact LIKE '%Hawaiian%'").fetchone()[0]
    assert n == 0


def test_chat_forget_cross_synonym_emits_event():
    _fresh()
    from mongoose.llm import chat_with_memory
    from mongoose.services import memory as M
    M.remember_fact("The user likes pineapple on pizza.", source="test")
    r = asyncio.run(chat_with_memory("forget Hawaiian", conversation_id="c", role="chat"))
    assert any(e["type"] == "forgotten" for e in r["memory_events"])
    assert not any("pineapple" in f["fact"] for f in M.all_facts())


# ---- supersession of contradicting preferences ----

def test_contradicting_preference_supersedes_not_duplicates():
    _fresh()
    from mongoose.services import memory as M
    from mongoose.memory_intent import detect_memory_intent, preference_to_fact
    mi = detect_memory_intent("really fond of Hawaiian")
    M.remember_preference(preference_to_fact(mi), mi.polarity, mi.target)
    mi2 = detect_memory_intent("I can't stand Hawaiian")
    res = M.remember_preference(preference_to_fact(mi2), mi2.polarity, mi2.target)
    assert res.get("superseded") == "The user likes Hawaiian."
    facts = [f["fact"] for f in M.all_facts()]
    assert sum("Hawaiian" in f for f in facts) == 1
    assert any(f.startswith("The user dislikes Hawaiian") for f in facts)
    assert "The user likes Hawaiian." not in facts




def test_broad_preference_does_not_supersede_narrow_context_preference():
    _fresh()
    from mongoose.services import memory as M
    # Context-specific dislike should survive a broader like; they are not the same referent.
    M.remember_preference("The user dislikes pineapple on pizza.", "dislikes", "pineapple on pizza")
    res = M.remember_preference("The user likes pineapple.", "likes", "pineapple")
    assert res.get("superseded") is None
    facts = [f["fact"] for f in M.all_facts()]
    assert "The user dislikes pineapple on pizza." in facts
    assert "The user likes pineapple." in facts


def test_broad_forget_pineapple_does_not_delete_pineapple_on_pizza_fact():
    _fresh()
    from mongoose.services import memory as M
    M.remember_fact("The user dislikes pineapple on pizza.", source="test")
    res = M.forget_memory("pineapple")
    assert res["status"] == "needs_confirmation"
    facts = [f["fact"] for f in M.all_facts()]
    assert "The user dislikes pineapple on pizza." in facts

def test_concept_keys_are_general_infrastructure():
    """Concept resolution must cover the user's real domains, not just pizza."""
    from mongoose.memory_resolver import concepts_for
    assert "home_assistant" in concepts_for("Home Assistant")
    assert "home_assistant" in concepts_for("HA")
    assert "pizza_hawaiian_pineapple" in concepts_for("Hawaiian pizza")
    assert "pizza_hawaiian_pineapple" in concepts_for("pineapple on pizza")
    assert "employer_work" in concepts_for("the apprenticeship")
