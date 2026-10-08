"""Tests for the two-lane diagnostics + metrics system. Load-bearing properties: delta-saving only
records meaningful changes (not noise); the report-ready trigger fires on weight OR age; exporting
produces both formats and resets so collection carries on; the scheduler notify fires once per
window."""
import asyncio
import os
import tempfile
from datetime import datetime, timezone, timedelta


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def test_delta_saving_filters_noise():
    _fresh()
    from mongoose.services import diagnostics as D
    assert D.record_metric("llm.tps", 40.0)["saved"] is True       # first value saves
    assert D.record_metric("llm.tps", 40.5)["saved"] is False      # ~1% < 5% -> not saved
    assert D.record_metric("llm.tps", 50.0)["saved"] is True       # big move -> saved


def test_two_lanes_tracked_separately():
    _fresh()
    from mongoose.services import diagnostics as D
    D.record_inference("llm.chat", "fast event")    # fast lane
    D.record_benchmark("model.x", "slow event")     # slow lane
    st = D.report_status()
    assert st["fast_lane"] == 1
    assert st["slow_lane"] == 1


def test_kinds_have_different_weights():
    _fresh()
    from mongoose.services import diagnostics as D
    b = D.record_bug("x", "a bug")
    i = D.record_inference("x", "an inference")
    assert b["weight"] > i["weight"]   # a bug weighs more than an inference sample


def test_report_ready_on_weight_threshold():
    _fresh()
    from mongoose.services import diagnostics as D
    # 5 bugs * 5.0 = 25.0 >= threshold
    for n in range(5):
        D.record_bug(f"s{n}", f"bug {n}")
    st = D.report_status()
    assert st["ready"] is True
    assert st["reason"] == "weight_threshold"


def test_report_ready_on_max_age():
    _fresh()
    from mongoose.services import diagnostics as D
    now = datetime.now(timezone.utc)
    D.record_inference("x", "old", at=now - timedelta(hours=30))
    st = D.report_status(now=now)
    assert st["ready"] is True
    assert st["reason"] == "max_age"


def test_not_ready_when_below_both():
    _fresh()
    from mongoose.services import diagnostics as D
    D.record_inference("x", "fresh light event")   # weight 1.0, age 0
    st = D.report_status()
    assert st["ready"] is False
    assert st["reason"] == "accumulating"


def test_export_produces_both_formats_and_resets():
    _fresh()
    from mongoose.services import diagnostics as D
    D.record_bug("rtm", "classifier raised")
    D.record_inference("llm", "1.2s")
    exp = D.export_report()
    assert os.path.exists(exp["md_path"])
    assert os.path.exists(exp["json_path"])
    assert exp["event_count"] == 2
    assert exp["reset"] is True
    # after export, the accumulator is clear , collection carries on from a clean slate
    st = D.report_status()
    assert st["event_count"] == 0
    # and new events still land
    D.record_bug("x", "new")
    assert D.report_status()["event_count"] == 1


def test_export_no_reset_keeps_events_pending():
    _fresh()
    from mongoose.services import diagnostics as D
    D.record_bug("x", "a bug")
    D.export_report(mark_reported=False)
    assert D.report_status()["event_count"] == 1   # still pending


def test_json_payload_has_breakdowns():
    _fresh()
    from mongoose.services import diagnostics as D
    D.record_bug("rtm", "x")
    D.record_benchmark("model", "y")
    exp = D.export_report()
    p = exp["payload"]
    assert "by_kind" in p and "by_scope" in p and "by_lane" in p
    assert p["event_count"] == 2


def test_scheduler_notify_fires_once_per_window():
    _fresh()
    from mongoose.services import diagnostics as D
    from mongoose.services.scheduler import _job_diagnostics_notify
    # not ready -> no notify
    assert asyncio.run(_job_diagnostics_notify())["notified"] is False
    # cross threshold
    for n in range(6):
        D.record_bug(f"s{n}", f"bug {n}")
    assert asyncio.run(_job_diagnostics_notify())["notified"] is True
    # de-dupe within the same window
    assert asyncio.run(_job_diagnostics_notify())["notified"] is False
    # after export (which resets the notified marker), a new window can notify again
    D.export_report()
    for n in range(6):
        D.record_bug(f"t{n}", f"bug {n}")
    assert asyncio.run(_job_diagnostics_notify())["notified"] is True


def test_unknown_kind_degrades_not_lost():
    _fresh()
    from mongoose.services import diagnostics as D
    ev = D.record_event("wibble", scope="x", summary="weird")
    assert ev["kind"] == "issue"   # unknown kind -> 'issue', not dropped
