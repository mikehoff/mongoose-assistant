"""Regression for the IRC transcript bug: the owner asked Mongoose to message a friend and it falsely claimed
it couldn't. Two root causes: (1) the role router classified messaging requests as 'chat' so the
message_person tool never ran, and (2) message_person couldn't resolve a human name to a paired
device. Both fixed; these tests use the exact failing phrases."""
import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _pair_sam():
    from mongoose.devices import create_pairing_code, claim_pairing_code
    code = create_pairing_code("trusted_guest")
    code_str = code["code"] if isinstance(code, dict) else code
    claim = claim_pairing_code(code_str, "Sam's iPhone", "phone")
    return claim["device_id"]


# ---- routing: messaging requests must classify as 'tool' ----

def test_messaging_phrases_route_to_tool():
    from mongoose.role_router import classify_role
    for p in ("send Sam a message", "send Sam a message (Sam's iphone)",
              "tell Sam i really like him", "message Sam hello", "send a message to Sam",
              "let Sam know dinner is at 8", "tell Alex that I am running late",
              "leave Sam a note", "text Sam hello"):
        assert classify_role(p) == "tool", p


def test_self_referential_phrases_stay_chat():
    """Must not over-trigger: 'tell me a joke' is chat, not a message-send."""
    from mongoose.role_router import classify_role
    for p in ("tell me a joke", "tell me about yourself", "tell me the weather",
              "let me know what you think", "tell us a story", "send help"):
        assert classify_role(p) == "chat", p


# ---- delivery: name resolves to the paired device and the message lands ----

def test_message_by_name_delivers_to_guest():
    _fresh()
    dev_id = _pair_sam()
    import mongoose.tools as T
    r = asyncio.run(T._tool_message_person({"to": "Sam", "text": "I really like you"}))
    assert r["ok"]
    assert r["sent_to"] == f"guest:{dev_id}"
    from mongoose.services.mailbox import list_thread
    thread = list_thread(participant=f"guest:{dev_id}")
    assert any("really like" in m.get("body", "") for m in thread)


def test_message_unknown_name_is_graceful():
    _fresh()
    _pair_sam()
    import mongoose.tools as T
    r = asyncio.run(T._tool_message_person({"to": "Nonexistent", "text": "hi"}))
    assert r["ok"] is False
    assert "no paired device" in r["error"]


def test_message_everyone_broadcasts():
    _fresh()
    _pair_sam()
    import mongoose.tools as T
    r = asyncio.run(T._tool_message_person({"to": "everyone", "text": "dinner at 8"}))
    assert r["ok"]


def test_resolver_requires_unambiguous_match():
    """If two devices match the name, resolve to neither (don't message the wrong person)."""
    _fresh()
    from mongoose.devices import create_pairing_code, claim_pairing_code
    for label in ("Samir's iPad", "Sam's iPhone"):  # both start with 'sam'
        code = create_pairing_code("trusted_guest")
        code_str = code["code"] if isinstance(code, dict) else code
        claim_pairing_code(code_str, label, "phone")
    import mongoose.tools as T
    # 'sam' matches both -> ambiguous -> graceful refusal, not a wrong send
    r = asyncio.run(T._tool_message_person({"to": "sam", "text": "hi"}))
    assert r["ok"] is False


# ---- fact-quality gate: the garbled "likes him." from the transcript must not be stored ----

def test_low_quality_auto_facts_rejected():
    _fresh()
    from mongoose.services.memory import remember_fact
    for bad in ("likes him.", "likes him", "is nice", "him", "does it", "really", "he."):
        r = remember_fact(bad, source="auto")
        assert r["stored"] is False, bad
        assert r["reason"] == "low_quality"


def test_genuine_auto_facts_still_stored():
    _fresh()
    from mongoose.services.memory import remember_fact
    for good in ("The user's name is Alex.", "Alex has a friend named Sam."):
        r = remember_fact(good, source="auto")
        assert r["stored"] is True, good


def test_explicit_facts_bypass_quality_gate():
    """A user who explicitly says 'remember that ...' vouches for it — don't second-guess them."""
    _fresh()
    from mongoose.services.memory import remember_fact
    r = remember_fact("likes him.", source="explicit")
    assert r["stored"] is True
