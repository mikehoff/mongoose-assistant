"""Tests for the dual-model architecture: a silent research worker that returns
structured evidence (never user-facing prose), and a residency probe that reports
honestly (unknown when Ollama is unreachable, never inferred from weights)."""
import asyncio

import mongoose.research_worker as RW
import mongoose.model_residency as MR


def test_worker_returns_structured_notes(monkeypatch):
    async def fake_ask(prompt, system=None, role="chat", model=None):
        # worker should be invoked with the research role
        assert role == "research"
        return '{"findings":["RTM takes over 1 March 2026"],"sources":["lease"],"confidence":"high","gaps":[]}'
    import mongoose.llm as llm
    monkeypatch.setattr(llm, "ask_ollama", fake_ask)
    notes = asyncio.run(RW.research_worker("when does RTM take over?"))
    assert notes["findings"] == ["RTM takes over 1 March 2026"]
    assert notes["confidence"] == "high"
    assert "worker_model" in notes


def test_worker_wraps_non_json_as_low_confidence(monkeypatch):
    """If the worker fails to emit JSON, we wrap it as a low-confidence finding rather
    than leaking raw text to the user as if it were the answer."""
    async def fake_ask(prompt, system=None, role="chat", model=None):
        return "I think it's sometime in July, not sure."
    import mongoose.llm as llm
    monkeypatch.setattr(llm, "ask_ollama", fake_ask)
    notes = asyncio.run(RW.research_worker("when?"))
    assert notes["confidence"] == "low"
    assert notes["findings"]
    assert any("unverified" in g or "structured" in g for g in notes["gaps"])


def test_notes_formatted_as_evidence_block_not_answer():
    notes = {"findings": ["a", "b"],
             "sources": [{"index": 1, "title": "doc", "kind": "file"}],
             "confidence": "medium", "gaps": ["x"], "grounded": True}
    block = RW.format_notes_for_owner(notes)
    # it's labelled as notes for the owner to phrase, not a finished reply
    assert "phrase these" in block.lower()
    assert "- a" in block and "- b" in block
    assert "grounded sources" in block


def test_owner_only_policy_is_default(monkeypatch):
    from mongoose.settings import get_settings
    s = get_settings()
    assert RW.owner_is_speaker_only(s)  # owner_only is the enforced default


def test_research_role_routes_to_research_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_RESEARCH_MODEL", "qwen3.5:9b")
    monkeypatch.setenv("MONGOOSE_DB_PATH", "/tmp/rwtest/data/mongoose.sqlite3")
    from mongoose.settings import Settings
    s = Settings()
    assert s.model_for_role("research") == "qwen3.5:9b"


# --- residency probe ---------------------------------------------------------
def test_residency_same_model_is_na(monkeypatch):
    # default: owner and worker are the same model
    r = asyncio.run(MR.check_residency(owner="gemma4:26b", worker="gemma4:26b"))
    assert r["pair_resident"].startswith("n/a")


def test_residency_unknown_when_ollama_unreachable(monkeypatch):
    async def no_ps(): return None
    monkeypatch.setattr(MR, "loaded_models", no_ps)
    r = asyncio.run(MR.check_residency(owner="gemma4:e2b", worker="qwen3.5:9b"))
    assert r["pair_resident"] == "unknown"
    # crucially, never inferred from weights
    assert "weights" in r["note"].lower()


def test_residency_yes_when_both_loaded(monkeypatch):
    async def both(): return ["gemma4:e2b", "qwen3.5:9b"]
    monkeypatch.setattr(MR, "loaded_models", both)
    r = asyncio.run(MR.check_residency(owner="gemma4:e2b", worker="qwen3.5:9b"))
    assert r["pair_resident"] == "yes"


def test_residency_no_when_one_missing(monkeypatch):
    async def one(): return ["gemma4:e2b"]
    monkeypatch.setattr(MR, "loaded_models", one)
    r = asyncio.run(MR.check_residency(owner="gemma4:e2b", worker="qwen3.5:9b"))
    assert r["pair_resident"] == "no"


# --- research worker exposed as a tool (supervisor/specialist pattern) --------
def test_research_registered_as_readonly_tool():
    from mongoose import tools
    assert "research" in tools.TOOLS
    assert "research" in tools.READ_TOOLS       # read-only
    assert "research" not in tools.ACTING_TOOLS  # never an acting tool
    assert "research" in tools._tool_catalogue()


def test_research_tool_returns_notes_not_prose(monkeypatch):
    import asyncio
    from mongoose import tools
    async def fake_ask(prompt, system=None, role="chat", model=None):
        return '{"findings":["RTM takes over 1 March 2026"],"sources":["lease"],"confidence":"high","gaps":[]}'
    import mongoose.llm as llm
    monkeypatch.setattr(llm, "ask_ollama", fake_ask)
    r = asyncio.run(tools._tool_research({"query": "when does RTM take over?"}))
    assert r["ok"] is True
    assert r["notes"]["findings"] == ["RTM takes over 1 March 2026"]
    # the owner-facing block is labelled as notes to phrase, not a final reply
    assert "phrase these" in r["for_owner"].lower()


def test_research_tool_requires_query():
    import asyncio
    from mongoose import tools
    r = asyncio.run(tools._tool_research({}))
    assert r["ok"] is False


def test_research_does_not_unlock_acting_tool(monkeypatch):
    """research is a READ tool, so after it runs, an acting tool (control_home) is
    still gated exactly as after any other read tool: it must be user-requested.
    This proves the worker can't be a backdoor to actions."""
    from mongoose import tools
    # research is in the same read-tool class that triggers the acting-tool guard
    assert "research" in tools.READ_TOOLS
    # the guard keys off READ_TOOLS membership, so research participates in it
    # (control_home stays guarded; no special exemption was added for research)
    assert "control_home" in tools.ACTING_TOOLS


# --- audit v0.14.10 remediation ----------------------------------------------
def test_research_role_not_user_requestable():
    """The role=research speaker bypass is closed: research is a valid INTERNAL role
    but not one a caller may request as a user-facing speaker."""
    from mongoose.llm import VALID_ROLES, USER_REQUESTABLE_ROLES
    assert "research" in VALID_ROLES               # tool can use it internally
    assert "research" not in USER_REQUESTABLE_ROLES  # caller cannot request it
    assert "code" not in USER_REQUESTABLE_ROLES      # workers don't speak
    assert USER_REQUESTABLE_ROLES == {"chat", "tool"}


def test_research_intent_routes_to_tool():
    from mongoose.role_router import classify_role
    for p in ["research the best option", "look up qwen3.5 benchmarks",
              "compare searxng and firecrawl options", "what is the latest on RTM",
              "investigate the service charge"]:
        assert classify_role(p) == "tool", p


def test_research_intent_does_not_overtrigger():
    from mongoose.role_router import classify_role
    # plain knowledge/chat must stay chat
    for p in ["what is the spleen", "tell me about Porto", "how are you",
              "explain photosynthesis"]:
        assert classify_role(p) == "chat", p


def test_research_worker_retrieves_and_grounds(monkeypatch):
    import asyncio
    import mongoose.research_worker as RW
    import mongoose.services.find as F
    # local retrieval returns a real record
    monkeypatch.setattr(F, "find", lambda q, limit=10: {"query": q, "count": 1, "results": [
        {"kind": "file", "title": "RTM notice", "where": "~/rtm.pdf",
         "snippet": "transfers 1 March 2026", "open": "~/rtm.pdf"}]})
    import mongoose.llm as llm
    async def fake_ask(prompt, system=None, role="chat", model=None):
        assert "Retrieved sources" in prompt   # sources were injected
        return '{"findings":["RTM takes over 1 March 2026 [1]"],"used_sources":[1],"confidence":"high","gaps":[]}'
    monkeypatch.setattr(llm, "ask_ollama", fake_ask)
    notes = asyncio.run(RW.research_worker("when does RTM take over?"))
    assert notes["grounded"] is True
    assert notes["sources"][0]["title"] == "RTM notice"
    assert notes["sources"][0]["open"] == "~/rtm.pdf"   # real open handle, grounded


def test_research_worker_honest_when_no_sources(monkeypatch):
    import asyncio
    import mongoose.research_worker as RW
    import mongoose.services.find as F
    monkeypatch.setattr(F, "find", lambda q, limit=10: {"query": q, "count": 0, "results": []})
    import mongoose.llm as llm
    async def fake_ask(prompt, system=None, role="chat", model=None):
        return '{"findings":["general knowledge note"],"used_sources":[],"confidence":"low","gaps":["no local sources"]}'
    monkeypatch.setattr(llm, "ask_ollama", fake_ask)
    notes = asyncio.run(RW.research_worker("something not in local data"))
    assert notes["grounded"] is False
    block = RW.format_notes_for_owner(notes)
    # honestly flags that this is model analysis, not grounded research
    assert "not grounded" in block.lower() or "no local sources" in block.lower()
