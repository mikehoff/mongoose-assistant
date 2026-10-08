"""The server watchdog must not get into a tight kill/respawn storm, and must stand down when paused.

Regression guard for the supervisor/respawn audit. watch_once is a pure state machine, so we stub the
OS-touching probe/restart/pidfile and drive it: under a server that NEVER recovers, restarts must be
bounded by the circuit breaker (not one-per-tick forever); and while a pause marker is set (deliberate
stop or in-progress deploy) the watchdog must never restart.
"""
from __future__ import annotations


from mongoose.services import server_watchdog as W


def test_storm_is_bounded_by_circuit_breaker(monkeypatch):
    monkeypatch.setattr(W, "read_pidfile", lambda: (1234, 8765))
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=2.0: False)  # never recovers
    monkeypatch.setattr(W, "is_paused", lambda: False)
    calls = {"n": 0}
    monkeypatch.setattr(W, "restart_server", lambda port, repo_root=None: calls.__setitem__("n", calls["n"] + 1) or {"ok": True})

    state = {"consecutive_failures": 0, "last_restart": 0.0, "restarts": 0}
    events = []
    for _ in range(40):
        state = W.watch_once(state, fail_threshold=3, cooldown_seconds=0.0,
                             max_failed_restarts=5, breaker_backoff_seconds=600.0)
        events.append(state.get("event"))

    # restarts capped at the breaker limit, NOT 40
    assert calls["n"] <= 6, f"watchdog restarted {calls['n']} times — circuit breaker failed"
    assert "breaker_opened" in events, "breaker never opened under permanent failure"
    assert events.count("breaker_open") > 0, "breaker did not back off after opening"


def test_paused_watchdog_never_restarts(monkeypatch):
    monkeypatch.setattr(W, "read_pidfile", lambda: (1234, 8765))
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=2.0: False)
    monkeypatch.setattr(W, "is_paused", lambda: True)  # deliberate stop / deploy in progress
    calls = {"n": 0}
    monkeypatch.setattr(W, "restart_server", lambda port, repo_root=None: calls.__setitem__("n", calls["n"] + 1) or {"ok": True})

    state = {"consecutive_failures": 5}
    for _ in range(10):
        state = W.watch_once(state)
    assert calls["n"] == 0, "watchdog restarted while paused — would fight a deliberate stop/deploy"


def test_recovery_closes_breaker(monkeypatch):
    monkeypatch.setattr(W, "read_pidfile", lambda: (1234, 8765))
    monkeypatch.setattr(W, "is_paused", lambda: False)
    monkeypatch.setattr(W, "restart_server", lambda port, repo_root=None: {"ok": True})
    # server is down, then recovers
    live_state = {"v": False}
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=2.0: live_state["v"])
    state = {"consecutive_failures": 0, "last_restart": 0.0}
    for _ in range(20):
        state = W.watch_once(state, fail_threshold=3, cooldown_seconds=0.0, max_failed_restarts=5)
    # now the server comes back
    live_state["v"] = True
    state = W.watch_once(state, fail_threshold=3, cooldown_seconds=0.0)
    assert state["event"] in ("live", "recovered_breaker_closed")
    assert state.get("failed_restarts", 0) == 0, "breaker/failure tally not reset on recovery"
