import json

import pytest

import mongoose.tools as tools
from mongoose.db import init_db
from mongoose.services.calendar_tasks import EventIn, add_event


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def _fake_models(monkeypatch, plan: dict):
    """Planner returns `plan` once, then finishes; persona echoes a line."""
    _fake_model_sequence(monkeypatch, [plan])


def _fake_model_sequence(monkeypatch, plans: list[dict]):
    """Planner returns each plan in turn, then {"tool": null} forever."""
    calls = {"i": 0}

    async def fake_ask_ollama(prompt, system=None, *, role="chat", model=None):
        i = calls["i"]
        calls["i"] += 1
        if i < len(plans):
            return json.dumps(plans[i])
        return json.dumps({"tool": None})

    async def fake_ask_persona(prompt, *, role="chat"):
        return "Done."

    monkeypatch.setattr(tools, "ask_ollama", fake_ask_ollama)
    monkeypatch.setattr(tools, "ask_persona", fake_ask_persona)


@pytest.mark.asyncio
async def test_tool_turn_executes_read_only_search(db, monkeypatch):
    from mongoose.services.email_index import EmailIn, ingest_email
    ingest_email(EmailIn(subject="Battery order", body="AAA batteries"))
    # A plain search phrase now routes deterministically to unified item search (search_files), rather
    # than depending on the local model planner to pick the right per-source tool. Unified search still
    # finds the email across sources, so the read-only search behaviour is preserved and more reliable.
    _fake_models(monkeypatch, {"tool": "search_email", "args": {"query": "batteries"}})
    out = await tools.run_tool_turn("search my email for the battery order")
    assert out["executed"] is True
    assert out["tool"] == "search_files"
    import json as _json
    assert "battery" in _json.dumps(out["result"]).lower()


@pytest.mark.asyncio
async def test_tool_turn_rejects_unknown_tool(db, monkeypatch):
    _fake_models(monkeypatch, {"tool": "delete_all_email", "args": {}})
    out = await tools.run_tool_turn("delete everything")
    assert out["executed"] is False
    assert out["tool"] is None
    assert out.get("rejected_tool") == "delete_all_email"


@pytest.mark.asyncio
async def test_tool_turn_no_tool_degrades_to_chat(db, monkeypatch):
    _fake_models(monkeypatch, {"tool": None})
    out = await tools.run_tool_turn("tell me a joke")
    assert out["executed"] is False
    assert out["reply"] == "Done."


@pytest.mark.asyncio
async def test_availability_tool(db, monkeypatch):
    add_event(EventIn(title="Busy", start_at="2026-06-13T10:00:00+01:00", end_at="2026-06-13T11:00:00+01:00"))
    _fake_models(monkeypatch, {"tool": "check_availability", "args": {"start_at": "2026-06-13T10:30:00+01:00", "end_at": "2026-06-13T10:45:00+01:00"}})
    out = await tools.run_tool_turn("am I free then?")
    assert out["executed"] is True
    assert out["result"]["is_free"] is False


@pytest.mark.asyncio
async def test_allowlist_contains_no_destructive_tools():
    # Guard: the agent must never gain send/delete/purchase tools.
    names = set(tools.TOOLS)
    forbidden = {"send_email", "delete_email", "archive_email", "purchase", "modify_calendar"}
    assert names.isdisjoint(forbidden)
    # Only one acting tool (home control); everything else is read-only.
    assert "control_home" in names


@pytest.mark.asyncio
async def test_multi_step_chains_two_tools(db, monkeypatch):
    from mongoose.services.calendar_tasks import EventIn, add_event
    add_event(EventIn(title="Busy", start_at="2026-06-13T10:00:00+01:00", end_at="2026-06-13T11:00:00+01:00"))
    # Step 1: check availability. Step 2: a different tool (weather). Then finish.
    _fake_model_sequence(monkeypatch, [
        {"tool": "check_availability", "args": {"start_at": "2026-06-13T10:30:00+01:00", "end_at": "2026-06-13T10:45:00+01:00"}},
        {"tool": "search_files", "args": {"query": "nothing"}},
    ])
    out = await tools.run_tool_turn("check my calendar then search files")
    assert out["executed"] is True
    assert len(out["steps"]) == 2
    assert out["steps"][0]["tool"] == "check_availability"
    assert out["steps"][1]["tool"] == "search_files"
    assert out["stop_reason"] == "model_finished"


@pytest.mark.asyncio
async def test_repeat_identical_call_is_stopped(db, monkeypatch):
    # Model tries the exact same call twice; the second is refused.
    _fake_model_sequence(monkeypatch, [
        {"tool": "search_files", "args": {"query": "x"}},
        {"tool": "search_files", "args": {"query": "x"}},
    ])
    out = await tools.run_tool_turn("search files")
    assert out["stop_reason"] == "repeat_call"
    assert len([s for s in out["steps"] if not s.get("rejected")]) == 1


@pytest.mark.asyncio
async def test_max_steps_ceiling(db, monkeypatch):
    # Model keeps asking for distinct calls; loop must stop at MAX_STEPS.
    plans = [{"tool": "search_files", "args": {"query": f"q{i}"}} for i in range(10)]
    _fake_model_sequence(monkeypatch, plans)
    out = await tools.run_tool_turn("keep searching")
    assert out["stop_reason"] == "max_steps"
    assert len(out["steps"]) == tools.MAX_STEPS


@pytest.mark.asyncio
async def test_chain_with_unknown_tool_stops(db, monkeypatch):
    _fake_model_sequence(monkeypatch, [
        {"tool": "search_files", "args": {"query": "ok"}},
        {"tool": "wipe_disk", "args": {}},
    ])
    out = await tools.run_tool_turn("search then do something bad")
    # First (valid) step executed; second rejected and loop stopped.
    assert out["executed"] is True
    assert out["stop_reason"] == "rejected_tool"
    assert any(s.get("rejected") for s in out["steps"])
