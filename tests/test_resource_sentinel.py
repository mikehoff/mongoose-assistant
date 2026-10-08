"""Resource sentinel , proactive OS-level pressure detection + safe relief.

The sentinel closes the gap where nothing measured actual memory pressure or a single operation looping
too long (short of a total freeze) and acted on it. These tests pin the behaviours that make it safe to
run unattended: it reacts to SUSTAINED pressure not transient spikes, rate-limits its relief so it cannot
thrash the scheduler, relieves using only the safe deferrable-jobs path (never a bulk kill, never a
safety job), and degrades to a clean no-op when psutil is unavailable.

DB isolation is automatic via conftest.
"""
from __future__ import annotations

import builtins



def _fresh_sentinel(monkeypatch, *, sustain=3, cooldown=9999, lag_ceil=5.0):
    monkeypatch.setenv("MONGOOSE_SENTINEL_SUSTAIN_TICKS", str(sustain))
    monkeypatch.setenv("MONGOOSE_SENTINEL_RELIEF_COOLDOWN", str(cooldown))
    monkeypatch.setenv("MONGOOSE_SENTINEL_LOOP_LAG_CEIL", str(lag_ceil))
    # reimport so module-level threshold constants pick up the env
    import importlib
    import mongoose.services.resource_sentinel as RS
    importlib.reload(RS)
    return RS


def test_single_spike_does_not_trigger_relief(monkeypatch):
    RS = _fresh_sentinel(monkeypatch)
    s = RS.ResourceSentinel()
    st = s.tick(loop_lag_ms=9000)  # one high-lag tick = pressure, but not sustained
    assert st["action"] == "pressure_observed"
    assert st["consecutive_pressure_ticks"] == 1


def test_sustained_pressure_triggers_safe_relief(monkeypatch):
    RS = _fresh_sentinel(monkeypatch, sustain=3)
    s = RS.ResourceSentinel()
    s.tick(loop_lag_ms=9000)
    s.tick(loop_lag_ms=9000)
    st = s.tick(loop_lag_ms=9000)  # third consecutive -> sustained
    assert st["action"] in ("relief", "relief_attempted")
    assert st["relief_count"] == 1


def test_relief_is_rate_limited(monkeypatch):
    RS = _fresh_sentinel(monkeypatch, sustain=3, cooldown=9999)
    s = RS.ResourceSentinel()
    for _ in range(3):
        s.tick(loop_lag_ms=9000)  # first relief
    # keep the pressure up; cooldown must prevent a second relief
    st = None
    for _ in range(3):
        st = s.tick(loop_lag_ms=9000)
    assert st["action"] == "pressure_cooldown"
    assert st["relief_count"] == 1  # still only one relief


def test_no_pressure_resets_counter(monkeypatch):
    RS = _fresh_sentinel(monkeypatch)
    s = RS.ResourceSentinel()
    s.tick(loop_lag_ms=9000)
    s.tick(loop_lag_ms=9000)
    st = s.tick(loop_lag_ms=0)  # pressure gone
    assert st["action"] == "observe"
    assert st["consecutive_pressure_ticks"] == 0


def test_degrades_gracefully_without_psutil(monkeypatch):
    RS = _fresh_sentinel(monkeypatch)
    _orig = builtins.__import__

    def no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("simulated absence")
        return _orig(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_psutil)
    sig = RS.read_signals(loop_lag_ms=100)
    assert sig["measured"] is False
    assert sig["sys_available_fraction"] is None
    assert sig["proc_rss_fraction"] is None
    # a tick with no psutil and low lag is a clean no-op
    s = RS.ResourceSentinel()
    st = s.tick(loop_lag_ms=100)
    assert st["action"] == "observe"


def test_relief_only_pauses_deferrable_jobs_never_safety(monkeypatch):
    """The sentinel's relief must use the safe free_memory path: deferrable/background jobs only, with
    safety/continuity jobs (alerts, backups, DDNS, readiness) left running. Never a bulk kill."""
    from mongoose.services.scheduler import scheduler_control, _DEFERRABLE_FOR_MEMORY
    scheduler_control("free_memory", actor="resource_sentinel", reason="test")
    after = scheduler_control("status")
    paused = set(after["disabled"])
    assert paused, "relief paused nothing"
    assert paused.issubset(set(_DEFERRABLE_FOR_MEMORY)), "relief paused a non-deferrable (possibly safety) job"


def test_relief_records_an_incident(monkeypatch):
    RS = _fresh_sentinel(monkeypatch, sustain=2)
    from mongoose.db import connect
    s = RS.ResourceSentinel()
    s.tick(loop_lag_ms=9000)
    s.tick(loop_lag_ms=9000)  # sustained -> relief
    with connect() as con:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='self_heal_incidents'").fetchone()
        n = con.execute(
            "SELECT COUNT(*) FROM self_heal_incidents WHERE subsystem='resource_sentinel'"
        ).fetchone()[0] if exists else 0
    assert n >= 1


def test_status_is_persisted_and_readable(monkeypatch):
    RS = _fresh_sentinel(monkeypatch)
    s = RS.ResourceSentinel()
    s.tick(loop_lag_ms=0)
    st = RS.current_status()
    assert st is not None
    assert "signals" in st and "action" in st


# ── benchmark must not be crippled by the resource guards in its protected window ──────────────────
# A benchmark legitimately raises RSS/loop pressure; that pressure is the intended use of the quiet
# 5-8am slot. The dispatch-gate and auto-relief must NOT skip or pause a window-protected job while it
# is inside its window, or the benchmark would defeat itself out of the very slot reserved for it. The
# per-job timeout and process watchdog remain the runaway protections.

def test_benchmark_in_window_is_exempt_from_pressure_gate():
    from datetime import datetime, timezone
    from mongoose.services import scheduler as S
    red = {"level": "red", "warnings": ["RSS high", "memory high"]}
    spec = ("window", 5, 8)
    at6 = datetime(2026, 6, 30, 6, 0, 0, tzinfo=timezone.utc)
    at14 = datetime(2026, 6, 30, 14, 0, 0, tzinfo=timezone.utc)
    assert S._skip_deferrable_under_pressure("benchmark", red, spec=spec, now=at6) is False
    # outside the window the normal protection still applies
    assert S._skip_deferrable_under_pressure("benchmark", red, spec=spec, now=at14) is True


def test_non_window_deferrable_still_gated_under_pressure():
    from datetime import datetime, timezone
    from mongoose.services import scheduler as S
    red = {"level": "red", "warnings": ["RSS high"]}
    at6 = datetime(2026, 6, 30, 6, 0, 0, tzinfo=timezone.utc)
    assert S._skip_deferrable_under_pressure("reindex_files", red, spec=("interval", 360), now=at6) is True


def test_free_memory_does_not_pause_benchmark_in_window(monkeypatch):
    from mongoose.services import scheduler as S
    import mongoose.services.scheduler as sched
    monkeypatch.setattr(sched, "_window_in_hours", lambda s, e, now=None: (s == 5 and e == 8))
    names = S._normalise_job_names(None, action="free_memory")
    assert "benchmark" not in names          # exempt while in its window
    assert "reindex_files" in names          # other deferrables still relieved
    # explicitly naming benchmark still pauses it (owner override)
    assert "benchmark" in S._normalise_job_names("benchmark", action="free_memory")


def test_free_memory_pauses_benchmark_outside_window(monkeypatch):
    from mongoose.services import scheduler as S
    import mongoose.services.scheduler as sched
    monkeypatch.setattr(sched, "_window_in_hours", lambda s, e, now=None: False)
    names = S._normalise_job_names(None, action="free_memory")
    assert "benchmark" in names              # normal relief when it is not benchmark's slot
