"""Router + self-knowledge: the single route sends each turn to exactly one trap door, and Mongoose's
self is real evidence, not narration."""
from mongoose.pipeline import TurnRequest, selfknowledge
from mongoose.pipeline.handlers import BenchmarkHandler, PlainChatHandler, SelfKnowledgeHandler
from mongoose.pipeline.router import TurnRouter


def _router():
    return TurnRouter([BenchmarkHandler(), SelfKnowledgeHandler()], PlainChatHandler())


def test_routing_sends_each_turn_to_one_trap_door():
    r = _router()
    assert isinstance(r.select(TurnRequest(text="how did gemma 12b do?")), BenchmarkHandler)
    assert isinstance(r.select(TurnRequest(text="how much ram do you have right now?")), SelfKnowledgeHandler)
    assert isinstance(r.select(TurnRequest(text="which model are you running?")), SelfKnowledgeHandler)
    assert isinstance(r.select(TurnRequest(text="tell me a joke about otters")), PlainChatHandler)


def test_selfknowledge_gathers_real_rows():
    ev = selfknowledge.gather()
    ids = {s.id for s in ev.sources}
    assert "self:version" in ids and "self:models" in ids and "self:tools" in ids
    mem = ev.get("self:memory")
    assert mem is not None and mem.data["total_ram_gb"] > 0     # a real probe, not a guess
    tools = ev.get("self:tools")
    assert "web_search" in tools.data["tools_available"]        # 'do you have web search' is now checkable
