"""Hallucinated-failure fix: the phrasing step must be handed a deterministic, authoritative outcome
computed from the tool result, so the model can't report a successful action as a failure (the
'turn on desk lamp' transcript bug where HA returned ok:True but Mongoose said it failed)."""
import asyncio
import json
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


def _run_with_scripted_home(monkeypatch, home_result):
    """Run the loop forcing a single control_home call returning home_result, capturing the phrasing
    prompt the model receives."""
    import mongoose.tools as T
    captured = {}

    plan = iter([
        json.dumps({"tool": "control_home", "args": {"device_id": "desk lamp", "action": "on"}}),
        json.dumps({"tool": None}),
    ])

    async def fake_ask_ollama(prompt, system=None, role=None):
        try:
            return next(plan)
        except StopIteration:
            return json.dumps({"tool": None})

    async def fake_persona(prompt, role=None):
        captured["phrasing_prompt"] = prompt
        return "(phrased reply)"

    async def fake_home(args):
        return home_result

    monkeypatch.setattr(T, "ask_ollama", fake_ask_ollama)
    monkeypatch.setattr(T, "ask_persona", fake_persona)
    monkeypatch.setattr(T, "_tool_home", fake_home)
    monkeypatch.setitem(T.TOOLS["control_home"], "executor", fake_home)
    asyncio.run(T.run_tool_turn("turn on desk lamp"))
    return captured.get("phrasing_prompt", "")


def test_success_marked_authoritative(monkeypatch):
    _fresh()
    prompt = _run_with_scripted_home(monkeypatch,
                                     {"ok": True, "detail": "Turning on light: study_cat_neon"})
    assert "AUTHORITATIVE OUTCOME" in prompt
    assert "control_home: SUCCEEDED" in prompt
    assert "MUST NOT contradict" in prompt
    # the instruction forbids claiming failure on success
    assert "never claim it failed" in prompt


def test_failure_marked_authoritative(monkeypatch):
    _fresh()
    prompt = _run_with_scripted_home(monkeypatch,
                                     {"ok": False, "error": "home_assistant_unreachable"})
    assert "control_home: FAILED" in prompt
    assert "home_assistant_unreachable" in prompt


def test_ground_truth_ignores_prior_turns(monkeypatch):
    """The instruction must tell the model to judge only THIS turn, not remembered prior errors."""
    _fresh()
    prompt = _run_with_scripted_home(monkeypatch, {"ok": True, "detail": "done"})
    assert "Do NOT let any earlier turn" in prompt or "remembered state override" in prompt
