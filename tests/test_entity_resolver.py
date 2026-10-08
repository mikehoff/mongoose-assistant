import asyncio
import pytest

from mongoose.services import entity_resolver as er


@pytest.fixture
def fake_entities(monkeypatch):
    ents = [
        {"entity_id": "switch.study_cat_neon_socket_1", "friendly_name": "Study Desk Lamp"},
        {"entity_id": "switch.kitchen_cat_socket_1", "friendly_name": "Kitchen Cat"},
        {"entity_id": "switch.lounge_cat_socket_1", "friendly_name": "Lounge Cat"},
        {"entity_id": "light.study_japan_lamp_socket_1", "friendly_name": "Study Japan Lamp"},
        {"entity_id": "switch.study_floor_lamp_socket_1", "friendly_name": "Study Floor Lamp"},
    ]
    er._CACHE["entities"] = ents
    er._CACHE["fetched_at"] = 9e18
    yield
    er._CACHE["entities"] = None
    er._CACHE["fetched_at"] = 0.0


def test_resolves_specific_device(fake_entities):
    m = asyncio.run(er.resolve_entity("study", "cat neon", "turn_on"))
    assert m["entity_id"] == "switch.study_cat_neon_socket_1"
    assert (m["domain"], m["service"]) == ("switch", "turn_on")


def test_disambiguates_same_device_across_rooms(fake_entities):
    m = asyncio.run(er.resolve_entity("kitchen", "cat", "turn_on"))
    assert m["entity_id"] == "switch.kitchen_cat_socket_1"


def test_off_action_maps_to_turn_off(fake_entities):
    m = asyncio.run(er.resolve_entity("study", "floor lamp", "turn off"))
    assert m["entity_id"] == "switch.study_floor_lamp_socket_1"
    assert m["service"] == "turn_off"


def test_no_confident_match_returns_candidates(fake_entities):
    m = asyncio.run(er.resolve_entity("bedroom", "unicorn", "turn_on"))
    assert m["entity_id"] is None
    assert isinstance(m["candidates"], list)


def test_no_entities_returns_none(monkeypatch):
    er._CACHE["entities"] = []
    er._CACHE["fetched_at"] = 9e18
    m = asyncio.run(er.resolve_entity("study", "cat neon", "turn_on"))
    assert m is None
    er._CACHE["entities"] = None
    er._CACHE["fetched_at"] = 0.0


def test_model_resolver_picks_model_choice(fake_entities, monkeypatch):
    # When the model is available and returns an index, that choice is used.
    import mongoose.services.entity_resolver as erm
    async def fake_ask(user, system=None, *, role="chat", model=None):
        return '{"index": 0}'
    monkeypatch.setattr(erm, "ask_ollama", fake_ask, raising=False)
    # two candidates share 'cat' + room word so model is consulted
    m = asyncio.run(erm.resolve_entity_with_model("study", "cat neon", "turn_on"))
    assert m["entity_id"] == "switch.study_cat_neon_socket_1"


def test_model_resolver_falls_back_when_model_errors(fake_entities, monkeypatch):
    import mongoose.services.entity_resolver as erm
    from mongoose.llm import LocalModelError
    async def boom(user, system=None, *, role="chat", model=None):
        raise LocalModelError("no model")
    monkeypatch.setattr(erm, "ask_ollama", boom, raising=False)
    m = asyncio.run(erm.resolve_entity_with_model("study", "cat neon", "turn_on"))
    # deterministic fallback still resolves the all-words match
    assert m["entity_id"] == "switch.study_cat_neon_socket_1"


def test_unreachable_ha_raises_not_silent(monkeypatch):
    # When HA is configured but the fetch fails, the resolver must raise
    # HAUnreachable (so the user hears "can't reach HA", not "device not found").
    import mongoose.services.entity_resolver as erm
    erm._CACHE["entities"] = None
    erm._CACHE["fetched_at"] = 0.0
    monkeypatch.setenv("HA_URL", "http://192.0.2.1:8123")  # TEST-NET, unroutable
    monkeypatch.setenv("HA_TOKEN", "tok")

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k):
            raise erm.httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(erm.httpx, "AsyncClient", FakeClient)
    with pytest.raises(erm.HAUnreachable):
        asyncio.run(erm._fetch_entities())
