"""Tests for two memory hardening fixes:
1. The fact-bearing gate must catch habitual/routine self-disclosure (the household-valuable
   facts it previously dropped), without firing on ordinary turns.
2. Conversation history must be bounded by a TOKEN budget, not just a message count, so
   verbose turns can't silently overflow the model context window."""
from datetime import datetime, timezone

from mongoose.db import connect, init_db
from mongoose.services import memory as M


# --- fact-extraction gate coverage -----------------------------------------

def test_gate_catches_habitual_and_relational_facts():
    should_pass = [
        "I live in Anytown",
        "I have a dog called Toby",
        "I work at Examplecorp",
        "my wife is called Sarah",
        "I drive a Honda",
        "my daughter goes to St Mary primary school",   # my-X-verb (previously missed)
        "I usually cycle on Saturdays",                   # habitual (previously missed)
        "I go running on weekends",                      # habitual verb (previously missed)
        "we meet every Friday",                          # routine (previously missed)
    ]
    for t in should_pass:
        assert M.looks_fact_bearing(t), f"should be fact-bearing: {t!r}"


def test_gate_does_not_fire_on_ordinary_turns():
    should_not = [
        "what is the weather today",
        "tell me a joke",
        "can you turn on the lights",
        "what time is my meeting",
        "how do I cook rice",
        "remind me to call the dentist",   # a task, not self-disclosure
    ]
    for t in should_not:
        assert not M.looks_fact_bearing(t), f"should NOT be fact-bearing: {t!r}"


def test_negated_remember_still_excluded():
    """The widened gate must not regress the 'don't remember that' / 'forget' exclusion."""
    assert not M.looks_fact_bearing("please don't remember that I live in Anytown")
    assert not M.looks_fact_bearing("forget that my wife is called Sarah")


# --- token-budgeted history --------------------------------------------------

def _add(cid, content):
    with connect() as con:
        con.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
                    (cid, "user", content, datetime.now(timezone.utc).isoformat()))
        con.commit()


def test_short_history_all_fits():
    init_db()
    cid = M.ensure_conversation()
    for i in range(M.DEFAULT_HISTORY_LIMIT):
        _add(cid, f"short msg {i}")
    h = M.recent_messages(cid)
    assert len(h) == M.DEFAULT_HISTORY_LIMIT     # nothing trimmed when well under budget


def test_long_history_is_token_trimmed():
    init_db()
    cid = M.ensure_conversation()
    big = "word " * 300                            # ~375 tokens each
    for _ in range(12):
        _add(cid, big)
    budgeted = M.recent_messages(cid, token_budget=1800)
    count_only = M.recent_messages(cid, token_budget=None)
    used = sum(M.estimate_tokens(m["content"]) for m in budgeted)
    assert len(count_only) == 12                   # count-only keeps all (would overflow)
    assert used <= 1800                            # token-budgeted respects the budget
    assert len(budgeted) < 12                      # and therefore trims


def test_always_keeps_at_least_the_latest_message():
    init_db()
    cid = M.ensure_conversation()
    _add(cid, "x " * 5000)                          # one huge message, alone
    h = M.recent_messages(cid, token_budget=100)
    assert len(h) == 1                              # never drop the immediate prior turn


def test_history_is_oldest_first():
    init_db()
    cid = M.ensure_conversation()
    for w in ["first", "second", "third"]:
        _add(cid, w)
    assert [m["content"] for m in M.recent_messages(cid)] == ["first", "second", "third"]


def test_build_context_applies_token_budget():
    init_db()
    cid = M.ensure_conversation()
    big = "word " * 300
    for _ in range(12):
        _add(cid, big)
    history, _facts = M.build_context(cid, "hello")
    used = sum(M.estimate_tokens(m["content"]) for m in history)
    from mongoose.settings import get_settings
    assert used <= get_settings().history_token_budget
