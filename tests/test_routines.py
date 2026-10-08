"""Named routines: CRUD, conservative matching, and the critical safety property that a routine
executes through the SAME guarded tool loop — it cannot bypass sensitive-action confirmation."""
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


def test_save_and_get():
    _fresh()
    from mongoose.services import routines as R
    assert R.save_routine("wind down", "warm the heater")["action"] == "created"
    r = R.get_routine("wind down")
    assert r["recipe"] == "warm the heater"


def test_update_preserves_trigger_phrase():
    _fresh()
    from mongoose.services import routines as R
    R.save_routine("wind down", "warm heater", trigger_phrase="turning in")
    R.save_routine("wind down", "warm heater and dim lights")  # no trigger supplied
    assert R.match_routine("turning in")["name"] == "wind down"


def test_match_variants():
    _fresh()
    from mongoose.services import routines as R
    R.save_routine("morning", "open blinds", trigger_phrase="good morning")
    for q in ("morning", "run morning", "start morning", "good morning", "MORNING"):
        assert R.match_routine(q) is not None, q


def test_match_is_conservative():
    _fresh()
    from mongoose.services import routines as R
    R.save_routine("wind down", "warm heater")
    # a partial / near-miss must NOT match (avoid running the wrong sequence)
    assert R.match_routine("wind") is None
    assert R.match_routine("winding down slowly tonight") is None


def test_delete():
    _fresh()
    from mongoose.services import routines as R
    R.save_routine("temp", "do a thing")
    assert R.delete_routine("temp")["ok"]
    assert R.get_routine("temp") is None


def test_empty_recipe_rejected():
    _fresh()
    from mongoose.services import routines as R
    assert R.save_routine("x", "")["ok"] is False


def test_run_missing_routine():
    import asyncio
    _fresh()
    from mongoose.services import routines as R
    r = asyncio.run(R.run_routine("nonexistent"))
    assert r["ok"] is False


def test_routine_runs_through_guarded_loop(monkeypatch):
    """A routine must call run_tool_turn — it inherits every guard, adds no privileged path."""
    import asyncio
    _fresh()
    from mongoose.services import routines as R
    called = {}

    async def fake_loop(prompt):
        called["prompt"] = prompt
        return {"reply": "done", "steps": [], "stop_reason": "model_finished"}

    monkeypatch.setattr("mongoose.tools.run_tool_turn", fake_loop)
    R.save_routine("test", "warm the heater")
    r = asyncio.run(R.run_routine("test"))
    assert r["ok"]
    assert called["prompt"] == "warm the heater"  # routine fed its recipe to the loop verbatim


def test_routine_cannot_bypass_sensitive_gating(monkeypatch):
    """SAFETY: if a routine's recipe leads the planner to a sensitive tool, the loop must return a
    PROPOSAL (needs_confirmation), not execute it. Routines are not a confirmation bypass."""
    import asyncio
    _fresh()
    from mongoose.services import routines as R
    import mongoose.tools as T

    # Force the planner to pick a sensitive tool (apply_update) on the first step.
    async def fake_ask_ollama(prompt, system=None, role=None):
        import json
        return json.dumps({"tool": "apply_update", "args": {}})

    async def fake_persona(prompt, role=None):
        return "I've prepared an update; it needs your confirmation before it runs."
    monkeypatch.setattr(T, "ask_ollama", fake_ask_ollama)
    monkeypatch.setattr(T, "ask_persona", fake_persona)

    R.save_routine("danger", "update yourself right now")
    r = asyncio.run(R.run_routine("danger"))
    # The loop must have stopped at confirmation, NOT executed the update.
    assert r.get("stop_reason") == "needs_confirmation"
    steps = r.get("steps", [])
    assert steps and steps[0].get("proposal") is True
    assert not steps[0].get("result", {}).get("executed")


def test_routine_drives_a_real_multi_tool_chain(monkeypatch):
    """End-to-end: a routine runs a genuine multi-step chain through the loop, executing REAL tool
    executors (not mocked), with a scripted planner standing in for Ollama. Proves 'runs end to end'
    rather than just 'hands the recipe to the loop'."""
    import asyncio
    import json
    _fresh()
    from mongoose.services import routines as R
    import mongoose.tools as T

    # Scripted planner: step 1 set a reminder, step 2 check the weather (a real read tool), then stop.
    plan = iter([
        json.dumps({"tool": "set_reminder", "args": {"text": "wind down", "when": "10pm"}}),
        json.dumps({"tool": "get_weather", "args": {}}),
        json.dumps({"tool": None}),
    ])

    async def fake_ask_ollama(prompt, system=None, role=None):
        try:
            return next(plan)
        except StopIteration:
            return json.dumps({"tool": None})

    async def fake_persona(prompt, role=None):
        return "Done: reminder set and weather checked."

    monkeypatch.setattr(T, "ask_ollama", fake_ask_ollama)
    monkeypatch.setattr(T, "ask_persona", fake_persona)

    R.save_routine("wind down", "set a wind-down reminder and check the weather")
    r = asyncio.run(R.run_routine("wind down"))

    assert r["ok"]
    tools_run = [s["tool"] for s in r.get("steps", [])]
    assert "set_reminder" in tools_run  # a REAL acting tool actually executed
    assert "get_weather" in tools_run   # chained to a second real tool
    # and the reminder really landed in storage
    from mongoose.services.notifications import search_notifications
    assert len(search_notifications("wind down")) >= 1


def test_new_acting_tools_respect_injection_guard(monkeypatch):
    """message_person/set_reminder are acting tools: after a READ tool runs on untrusted content,
    the loop must refuse them unless the user's prompt explicitly asked — same guard as control_home."""
    import asyncio
    import json
    _fresh()
    import mongoose.tools as T

    # Plan: first a read tool (search_email), then try to message_person — the injection guard
    # should block the message because the original prompt didn't ask to message anyone.
    plan = iter([
        json.dumps({"tool": "search_email", "args": {"query": "anything"}}),
        json.dumps({"tool": "message_person", "args": {"to": "owner", "text": "leaked"}}),
        json.dumps({"tool": None}),
    ])

    async def fake_ask_ollama(prompt, system=None, role=None):
        try:
            return next(plan)
        except StopIteration:
            return json.dumps({"tool": None})

    async def fake_persona(prompt, role=None):
        return "ok"

    async def fake_email(args):
        return {"results": []}

    monkeypatch.setattr(T, "ask_ollama", fake_ask_ollama)
    monkeypatch.setattr(T, "ask_persona", fake_persona)
    monkeypatch.setattr(T, "_tool_search_email", fake_email)
    monkeypatch.setitem(T.TOOLS["search_email"], "executor", fake_email)

    r = asyncio.run(T.run_tool_turn("summarise my email"))  # prompt does NOT ask to message anyone
    assert r.get("stop_reason") == "acting_blocked"
    blocked = [s for s in r.get("steps", []) if s.get("rejected")]
    assert any(s["tool"] == "message_person" for s in blocked)
