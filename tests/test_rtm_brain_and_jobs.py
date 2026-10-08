"""Tests for #3 (RTM wired into the conversational brain) and #2 (RTM scheduler jobs)."""
import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


# --- #3: conversational brain routes RTM queries ----------------------------

def test_brain_routes_rtm_arrears_query():
    _fresh()
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.create_demand(document_number="OD-1", flat="1ECF1", period="2026", amount=500,
                    issue_date="2026-01-01")
    M.refresh_all_demand_statuses()
    from mongoose.llm import chat_with_memory
    r = asyncio.run(chat_with_memory("what is overdue at example court?", conversation_id="t1"))
    assert r["source"] == "rtm_operational"
    assert "arrears" in r["reply"].lower() and "1ECF1" in r["reply"]


def test_brain_routes_rtm_money_position():
    _fresh()
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.create_demand(document_number="D-1", flat="1ECF2", period="2026", amount=900,
                    issue_date="2026-06-01")
    from mongoose.llm import chat_with_memory
    r = asyncio.run(chat_with_memory("what's the money position for the block?", conversation_id="t2"))
    assert r["source"] == "rtm_operational"
    assert "£900" in r["reply"] or "900" in r["reply"]


def test_brain_routes_rtm_build_query():
    _fresh()
    from mongoose.llm import chat_with_memory
    r = asyncio.run(chat_with_memory("how is the rtm build going?", conversation_id="t3"))
    assert r["source"] == "rtm_build"
    # honest about approval gate or creation state
    assert any(w in r["reply"].lower() for w in ("approv", "build", "wordpress"))


def test_rtm_intent_does_not_hijack_unrelated_chat():
    _fresh()
    from mongoose.role_router import looks_like_rtm_operational, looks_like_rtm_build
    # ordinary messages must NOT route to RTM
    for msg in ["what's the weather", "how are you", "tell me a joke", "what's 2 plus 2"]:
        assert not looks_like_rtm_operational(msg)
        assert not looks_like_rtm_build(msg)


def test_rtm_operational_focus_classification():
    _fresh()
    from mongoose.role_router import rtm_operational_focus
    assert rtm_operational_focus("what is overdue") == "arrears"
    assert rtm_operational_focus("any payments to allocate") == "review"
    assert rtm_operational_focus("what payables are due") == "payables"
    assert rtm_operational_focus("show the rtm dashboard") == "dashboard"


# --- #2: RTM scheduler jobs --------------------------------------------------

def test_rtm_jobs_registered():
    _fresh()
    import mongoose.services.scheduler as S
    reg = S._build_registry()
    assert "rtm_refresh" in reg and reg["rtm_refresh"][0][0] == "interval"
    assert "rtm_digest" in reg and reg["rtm_digest"][0][0] == "daily"
    assert "rtm_build" in reg and reg["rtm_build"][0][0] == "weekly"


def test_weekly_due_logic():
    _fresh()
    import mongoose.services.scheduler as S
    from datetime import datetime
    from zoneinfo import ZoneInfo
    S._ensure_table()
    tz = ZoneInfo(S.get_settings().timezone)
    mon = datetime(2026, 6, 22, 3, 0, tzinfo=tz)   # Monday
    tue = datetime(2026, 6, 23, 3, 0, tzinfo=tz)   # Tuesday
    assert S._weekly_due("wk_test", 0, 3, 0, now=mon) is True
    assert S._weekly_due("wk_test", 0, 3, 0, now=tue) is False


def test_rtm_refresh_job_runs():
    _fresh()
    import mongoose.services.scheduler as S
    r = asyncio.run(S._job_rtm_refresh())
    assert r["ok"] is True


def test_rtm_build_job_respects_approval_gate():
    _fresh()
    import mongoose.services.scheduler as S
    # first run creates the plan but does NOT build (unapproved)
    r = asyncio.run(S._job_rtm_build())
    assert r["ok"] is True and r["built"] is False
    # it either created the plan or is awaiting approval , never built without approval
    assert ("created_plan" in r) or ("awaiting approval" in r.get("note", ""))


def test_rtm_digest_silent_when_no_actions():
    _fresh()
    import mongoose.services.scheduler as S
    from mongoose.services import rtm_management as M
    M.seed_flats()  # no demands -> no actions
    r = asyncio.run(S._job_rtm_digest())
    assert r["ok"] is True and r["nudged"] is False
