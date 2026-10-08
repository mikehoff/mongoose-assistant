import pytest

from mongoose.db import init_db
from mongoose.services import memory as mem


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "m.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def test_conversation_history_roundtrip(db):
    cid = mem.ensure_conversation("c1")
    mem.add_message(cid, "user", "hello")
    mem.add_message(cid, "assistant", "hi there")
    mem.add_message(cid, "user", "how are you")
    hist = mem.recent_messages(cid)
    assert [m["role"] for m in hist] == ["user", "assistant", "user"]
    assert hist[0]["content"] == "hello"  # oldest first for replay


def test_history_limit_keeps_most_recent(db):
    cid = mem.ensure_conversation("c2")
    for i in range(20):
        mem.add_message(cid, "user", f"msg{i}")
    hist = mem.recent_messages(cid, limit=5)
    assert len(hist) == 5
    assert hist[-1]["content"] == "msg19"


def test_explicit_remember_and_search(db):
    mem.remember_fact("The user prefers tea over coffee.", source="explicit")
    found = mem.search_facts("what does the user drink, tea?")
    assert any("tea" in f for f in found)


def test_auto_extract_name(db):
    facts = mem.auto_extract_facts("hi, my name is Alex")
    assert facts == ["The user's name is Alex."]


def test_detect_explicit_memory():
    assert mem.detect_explicit_memory("please remember that I hate cilantro") == "I hate cilantro"
    assert mem.detect_explicit_memory("what's the weather") is None


def test_forget_removes_fact(db):
    mem.remember_fact("Delete me.", source="explicit")
    assert mem.search_facts("delete")
    r = mem.forget_fact("Delete me.")
    assert r["forgotten"] is True
    assert not mem.search_facts("delete")


def test_dedupe_same_fact_once(db):
    mem.remember_fact("Same fact.", source="explicit")
    mem.remember_fact("same fact.", source="explicit")  # case-insensitive dedupe
    assert len(mem.all_facts()) == 1


def test_clear_conversation(db):
    cid = mem.ensure_conversation("c3")
    mem.add_message(cid, "user", "x")
    mem.add_message(cid, "assistant", "y")
    r = mem.clear_conversation(cid)
    assert r["deleted_messages"] == 2
    assert mem.recent_messages(cid) == []


@pytest.mark.asyncio
async def test_model_extraction_proposes_not_stores(db, monkeypatch):
    import mongoose.llm as llm
    async def fake(prompt, system=None, *, role="chat", model=None):
        return '["The user has a dog named Toby."]'
    monkeypatch.setattr(llm, "ask_ollama", fake)
    proposed = await mem.propose_facts_via_model("walked my dog Toby")
    assert proposed == ["The user has a dog named Toby."]
    assert mem.all_facts() == []  # proposing must not store


@pytest.mark.asyncio
async def test_model_extraction_handles_garbage(db, monkeypatch):
    import mongoose.llm as llm
    async def fake(prompt, system=None, *, role="chat", model=None):
        return "not json at all"
    monkeypatch.setattr(llm, "ask_ollama", fake)
    assert await mem.propose_facts_via_model("x") == []


def test_fact_bearing_prefilter():
    # Ordinary turns must NOT trigger extraction (stay fast).
    for t in ["what is the weather", "tell me a joke", "what do I like?",
              "turn off the lights", "name three colours", "do I like coffee?"]:
        assert mem.looks_fact_bearing(t) is False, t
    # Self-disclosing turns SHOULD trigger extraction.
    for t in ["I like teal", "I prefer tea", "my dog is called Toby",
              "I live in Anytown", "call me Alex", "I'm allergic to peanuts",
              "remember that I hate cilantro"]:
        assert mem.looks_fact_bearing(t) is True, t


@pytest.mark.asyncio
async def test_extraction_skipped_on_non_fact_turn(db, monkeypatch):
    # If the message isn't fact-bearing, the model extraction must not even run.
    import mongoose.llm as llm
    called = {"n": 0}
    async def fake_chat(selected, messages, settings): return "ok"
    async def fake_extract(text, *, role="chat"):
        called["n"] += 1
        return []
    monkeypatch.setattr(llm, "_chat_call", fake_chat)
    monkeypatch.setattr(mem, "propose_facts_via_model", fake_extract)
    monkeypatch.setenv("MEMORY_AUTO_EXTRACT", "true")
    await llm.chat_with_memory("what is the weather", conversation_id="q", role="chat")
    assert called["n"] == 0  # pre-filter skipped extraction
    await llm.chat_with_memory("I like teal", conversation_id="q", role="chat")
    assert called["n"] == 1  # fact-bearing -> extraction ran
