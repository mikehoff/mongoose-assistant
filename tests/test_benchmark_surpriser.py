"""Tests for the second-lane benchmark surpriser and the benchmark window.

The second lane writes unprompted benchmark revelations as GENUINE Mongoose messages into the permanent
conversation (assistant role, never as if the user typed them), de-dupes, rate-limits, and pushes to
phone only when notable. The benchmark job runs only in the 5am-8am window.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "data", "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = os.path.join(d, "data")
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    from mongoose.db import init_db
    init_db()
    from mongoose.services.memory import ensure_conversation, DEFAULT_CONVERSATION_ID
    ensure_conversation(DEFAULT_CONVERSATION_ID)
    return d


def _record_a_run():
    from mongoose.db import set_app_state, utc_now_iso
    set_app_state("benchmark_last_run", utc_now_iso())
    set_app_state("benchmark_last_summary", json.dumps({
        "ran": True, "at": utc_now_iso(),
        "models": ["gemma4:26b-a4b-it-q4_K_M", "qwen2.5-coder:14b-instruct-q4_K_M"],
        "result_count": 12}))


def test_revelation_is_written_as_mongoose_message_not_user():
    """The core requirement: the unprompted message appears as a Mongoose (assistant) message in the
    permanent conversation, never as if the user typed it."""
    _fresh()
    _record_a_run()
    from mongoose.services.benchmark_surpriser import run_benchmark_revelation_cycle
    from mongoose.services.memory import recent_messages, DEFAULT_CONVERSATION_ID
    r = asyncio.run(run_benchmark_revelation_cycle())
    assert r["surfaced"] is True
    msgs = recent_messages(DEFAULT_CONVERSATION_ID, limit=3)
    last = msgs[-1]
    assert last["role"] == "assistant"  # NOT "user"
    assert "benchmark" in last["content"].lower() or "model" in last["content"].lower()


def test_revelation_dedupes_and_rate_limits():
    _fresh()
    _record_a_run()
    from mongoose.services.benchmark_surpriser import run_benchmark_revelation_cycle
    r1 = asyncio.run(run_benchmark_revelation_cycle())
    assert r1["surfaced"] is True
    # immediate re-run: same run, must stay silent
    r2 = asyncio.run(run_benchmark_revelation_cycle())
    assert r2["surfaced"] is False


def test_silent_when_no_benchmark_run():
    _fresh()
    from mongoose.services.benchmark_surpriser import run_benchmark_revelation_cycle
    r = asyncio.run(run_benchmark_revelation_cycle())
    assert r["surfaced"] is False


def test_visibility_view_reports_window():
    _fresh()
    _record_a_run()
    from mongoose.services.benchmark_surpriser import latest_benchmark_visibility
    v = latest_benchmark_visibility()
    assert v["last_run"] is not None
    assert "05:00" in v["window"]


def test_benchmark_window_gates_to_morning():
    _fresh()
    from mongoose.services.scheduler import _window_due
    from mongoose.settings import get_settings
    tz = ZoneInfo(get_settings().timezone)
    assert _window_due("benchmark", 5, 8, now=datetime(2026, 6, 22, 6, 0, tzinfo=tz)) is True
    assert _window_due("benchmark", 5, 8, now=datetime(2026, 6, 22, 14, 0, tzinfo=tz)) is False
    assert _window_due("benchmark", 5, 8, now=datetime(2026, 6, 22, 4, 0, tzinfo=tz)) is False


def test_benchmark_job_off_by_default():
    _fresh()
    from mongoose.services.scheduler import _job_benchmark
    r = asyncio.run(_job_benchmark())
    assert "disabled" in r.get("skipped", "")
