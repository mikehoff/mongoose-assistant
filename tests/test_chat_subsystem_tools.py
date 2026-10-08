"""Tests for plain-language access to subsystems via chat tools , the read-only status layer and the
sensitive propose/confirm gate."""
import asyncio
import os
import tempfile



def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    from mongoose.db import init_db
    init_db()


def test_new_read_tools_registered_and_classified():
    from mongoose.tools import TOOLS, READ_TOOLS
    for name in ["system_status", "consistency_check", "rtm_statement", "recent_activity"]:
        assert name in TOOLS, name
        assert name in READ_TOOLS, f"{name} must be read-only"


def test_sensitive_tools_registered_and_not_read():
    from mongoose.tools import TOOLS, READ_TOOLS, SENSITIVE_TOOLS
    for name in ["run_hardening_scan", "check_update", "apply_update", "run_due_jobs"]:
        assert name in TOOLS and name in SENSITIVE_TOOLS
        assert name not in READ_TOOLS


def test_system_status_all_returns_subsystems():
    _fresh()
    from mongoose.services import status_tools as ST
    r = asyncio.run(ST.tool_system_status({"area": "all"}))
    assert "subsystems" in r
    for area in ["scheduler", "hardening", "consistency", "updates", "connectivity", "rtm", "benchmark", "home"]:
        assert area in r["subsystems"], area


def test_system_status_consistency_area_runs_the_checker():
    _fresh()
    from mongoose.services import status_tools as ST
    r = asyncio.run(ST.tool_system_status({"area": "consistency"}))
    assert r["area"] == "consistency"
    assert r["consistency"]["claims_checked"] >= 2


def test_consistency_check_tool_is_clean():
    _fresh()
    from mongoose.services import status_tools as ST
    r = asyncio.run(ST.tool_consistency_check({}))
    assert r["ok"] is True and r["diverging_count"] == 0


def test_unknown_area_falls_back_to_all():
    _fresh()
    from mongoose.services import status_tools as ST
    r = asyncio.run(ST.tool_system_status({"area": "nonsense"}))
    assert r["area"] == "all" and "subsystems" in r


def test_sensitive_propose_does_not_execute_and_returns_token():
    _fresh()
    from mongoose.services import sensitive_tools as S
    p = S.propose_sensitive_action("run_hardening_scan", {})
    assert p["needs_confirmation"] is True and p["confirm_token"]
    assert "summary" in p


def test_sensitive_confirm_runs_and_token_is_single_use():
    _fresh()
    from mongoose.services import sensitive_tools as S
    p = S.propose_sensitive_action("run_hardening_scan", {})
    res = S.confirm_sensitive_action(p["confirm_token"])
    assert res["ok"] is True
    # reuse fails
    assert S.confirm_sensitive_action(p["confirm_token"])["ok"] is False


def test_confirm_unknown_token_is_safe():
    _fresh()
    from mongoose.services import sensitive_tools as S
    assert S.confirm_sensitive_action("not-a-real-token")["ok"] is False


def test_apply_update_without_stage_is_handled():
    _fresh()
    from mongoose.services import sensitive_tools as S
    p = S.propose_sensitive_action("apply_update", {})
    res = S.confirm_sensitive_action(p["confirm_token"])
    assert res["ok"] is False  # no stage_id, but no crash


def test_planner_does_not_execute_sensitive_tool_inline():
    """THE safety test: when the planner picks a sensitive tool, the loop returns a proposal and stops ,
    it must not execute the mutation. Confirmation requires a separate, explicit turn."""
    _fresh()
    import json
    import mongoose.tools as T

    async def fake_ask(planner_input, system=None, role=None):
        return json.dumps({"tool": "apply_update", "args": {"stage_id": "x"}})

    async def fake_persona(*a, **k):
        return "ok"

    T.ask_ollama = fake_ask
    T.ask_persona = fake_persona
    r = asyncio.run(T.run_tool_turn("apply the update"))
    assert r.get("stop_reason") == "needs_confirmation"
    proposals = [s for s in r.get("steps", []) if s.get("proposal")]
    assert len(proposals) == 1
    assert proposals[0]["result"]["needs_confirmation"] is True
