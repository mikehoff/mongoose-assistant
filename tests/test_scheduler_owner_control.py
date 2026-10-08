"""Owner scheduler controls: chat/UI can pause heavy background work without killing safety jobs."""
from __future__ import annotations

import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    from mongoose.db import init_db
    init_db()


def test_scheduler_control_can_disable_and_resume_job():
    _fresh()
    from mongoose.services import scheduler as S

    off = S.scheduler_control("disable", jobs=["benchmark"], reason="testing pause")
    assert off["ok"] is True
    rows = {r["job"]: r for r in S.job_status()}
    assert rows["benchmark"]["enabled"] is False
    assert "testing pause" in rows["benchmark"]["disabled_reason"]

    on = S.scheduler_control("enable", jobs="benchmark")
    assert on["ok"] is True
    rows = {r["job"]: r for r in S.job_status()}
    assert rows["benchmark"]["enabled"] is True


def test_free_memory_pauses_deferrable_jobs_not_alerts_or_readiness():
    _fresh()
    from mongoose.services import scheduler as S

    res = S.scheduler_control("free_memory", reason="owner asked via chat")
    assert res["ok"] is True
    disabled = set(res["disabled"])
    assert "benchmark" in disabled
    assert "reindex_files" in disabled
    assert "rtm_build_daily" in disabled
    assert "fire_alerts" not in disabled
    assert "readiness_refresh" not in disabled
    assert "rtm_refresh" not in disabled


def test_disabled_due_job_is_skipped(monkeypatch):
    _fresh()
    from mongoose.services import scheduler as S

    called = {"heavy": 0, "safe": 0}

    async def heavy():
        called["heavy"] += 1
        return {"ok": True}

    async def safe():
        called["safe"] += 1
        return {"ok": True}

    monkeypatch.setattr(S, "_build_registry", lambda: {
        "heavy": (("interval", 1), heavy),
        "safe": (("interval", 1), safe),
    })
    assert S.scheduler_control("disable", jobs="heavy")["ok"] is True
    ran = asyncio.run(S.run_due_jobs())
    assert "heavy" not in ran
    assert ran["safe"]["status"] == "ok"
    assert called == {"heavy": 0, "safe": 1}


def test_sensitive_scheduler_control_and_async_run_due_confirmation():
    _fresh()
    from mongoose.services import sensitive_tools as ST
    import mongoose.tools as T

    # scheduler_control is a gated mutation, not a read tool.
    assert "scheduler_control" in T.SENSITIVE_TOOLS
    assert "scheduler_control" not in T.READ_TOOLS
    assert "scheduler_control" in T.TOOLS

    p = ST.propose_sensitive_action("scheduler_control", {"action": "disable", "jobs": ["benchmark"]})
    res = asyncio.run(ST.confirm_sensitive_action_async(p["confirm_token"]))
    assert res["ok"] is True and res["result"]["ok"] is True

    # Regression: the chat confirmation path is already async, so confirming run_due_jobs must not
    # call asyncio.run() inside the running event loop.
    p2 = ST.propose_sensitive_action("run_due_jobs", {})
    res2 = asyncio.run(ST.confirm_sensitive_action_async(p2["confirm_token"]))
    assert res2["ok"] is True
