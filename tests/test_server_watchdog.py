"""The external server watchdog , recovers a WEDGED server (frozen event loop) that the in-process
watchdog can't, because the rescuer is a separate process. These tests pin the safety-critical logic:
no restart on a transient blip, restart after N consecutive failures, cooldown prevents a tight
kill/respawn loop, and it NEVER kills a pid it can't prove is a Mongoose server."""
from __future__ import annotations

import mongoose.services.server_watchdog as W


def _patch_pidfile(monkeypatch, pid, port):
    monkeypatch.setattr(W, "read_pidfile", lambda: (pid, port))


def test_live_resets_failure_counter(monkeypatch):
    _patch_pidfile(monkeypatch, 4321, 8765)
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=5.0: True)
    state = {"consecutive_failures": 2, "last_restart": 0.0}
    out = W.watch_once(state)
    assert out["event"] == "live"
    assert state["consecutive_failures"] == 0


def test_single_failure_does_not_restart(monkeypatch):
    _patch_pidfile(monkeypatch, 4321, 8765)
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=5.0: False)
    restarted = {"n": 0}
    monkeypatch.setattr(W, "restart_server", lambda port, repo_root=None: restarted.__setitem__("n", 1))
    state = {"consecutive_failures": 0, "last_restart": 0.0}
    out = W.watch_once(state, fail_threshold=3)
    assert out["event"] == "probe_failed"
    assert state["consecutive_failures"] == 1
    assert restarted["n"] == 0          # one blip must NOT bounce the server


def test_restarts_after_threshold(monkeypatch):
    _patch_pidfile(monkeypatch, 4321, 8765)
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=5.0: False)
    calls = {"n": 0}
    monkeypatch.setattr(W, "restart_server",
                        lambda port, repo_root=None: (calls.__setitem__("n", calls["n"] + 1)
                                                      or {"ok": True, "action": "restarted"}))
    state = {"consecutive_failures": 0, "last_restart": 0.0}
    # 3 consecutive failures -> restart on the 3rd
    W.watch_once(state, fail_threshold=3, cooldown_seconds=0)
    W.watch_once(state, fail_threshold=3, cooldown_seconds=0)
    out = W.watch_once(state, fail_threshold=3, cooldown_seconds=0)
    assert out["event"] == "restarted"
    assert calls["n"] == 1
    assert state["consecutive_failures"] == 0      # counter reset after restart
    assert state["restarts"] == 1


def test_cooldown_prevents_tight_restart_loop(monkeypatch):
    import time
    _patch_pidfile(monkeypatch, 4321, 8765)
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=5.0: False)
    calls = {"n": 0}
    monkeypatch.setattr(W, "restart_server",
                        lambda port, repo_root=None: (calls.__setitem__("n", calls["n"] + 1)
                                                      or {"ok": True}))
    # Pretend we JUST restarted (within cooldown), so even past the failure threshold we must NOT restart.
    state = {"consecutive_failures": 2, "last_restart": time.time(), "restarts": 1}
    out = W.watch_once(state, fail_threshold=3, cooldown_seconds=60)
    assert out["event"] == "probe_failed"
    assert calls["n"] == 0              # cooldown blocks the second restart


def test_no_pidfile_is_a_noop(monkeypatch):
    _patch_pidfile(monkeypatch, None, None)
    state = {"consecutive_failures": 5, "last_restart": 0.0}
    out = W.watch_once(state)
    assert out["event"] == "no_pidfile"
    assert state["consecutive_failures"] == 0


def test_restart_refuses_to_kill_non_mongoose_pid(monkeypatch):
    # pidfile points at a pid that is running but NOT a mongoose server , restart_server must refuse.
    _patch_pidfile(monkeypatch, 9999, 8765)
    monkeypatch.setattr(W, "_pid_is_running", lambda pid: True)   # it IS running...
    monkeypatch.setattr(W, "_pid_is_mongoose_server", lambda pid: False)  # ...but not ours
    killed = {"n": 0}
    monkeypatch.setattr(W, "_kill_pid", lambda pid, **kw: killed.__setitem__("n", 1))
    # Safety net: never let this test spawn a real server even if the refuse-path regresses.
    monkeypatch.setattr(W.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("restart_server must NOT spawn when refusing a foreign pid")))
    report = W.restart_server(8765)
    assert report["ok"] is False
    assert "refusing to kill" in report["detail"]
    assert killed["n"] == 0


def test_restart_kills_owned_server_and_spawns(monkeypatch, tmp_path):
    _patch_pidfile(monkeypatch, 4321, 8765)
    monkeypatch.setattr(W, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(W, "_pid_is_mongoose_server", lambda pid: True)
    monkeypatch.setattr(W, "_kill_pid", lambda pid, **kw: True)
    monkeypatch.setattr(W, "_data_dir", lambda: tmp_path)

    class _FakeProc:
        pid = 5555
    spawned = {"argv": None}

    def fake_popen(argv, **kw):
        spawned["argv"] = argv
        return _FakeProc()
    monkeypatch.setattr(W.subprocess, "Popen", fake_popen)
    report = W.restart_server(8765, repo_root=tmp_path)
    assert report["ok"] is True and report["action"] == "restarted"
    assert report["new_pid"] == 5555
    assert "serve" in spawned["argv"] and "--port" in spawned["argv"]


# ── hardening: pause, circuit breaker, stale pid ────────────────────────────────────────────────────

def test_pause_marker_stops_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "_data_dir", lambda: tmp_path)
    _patch_pidfile(monkeypatch, 4321, 8765)
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=5.0: False)
    calls = {"n": 0}
    monkeypatch.setattr(W, "restart_server", lambda port, repo_root=None: calls.__setitem__("n", 1))
    W.pause("deploy in progress")
    state = {"consecutive_failures": 5, "last_restart": 0.0}
    out = W.watch_once(state, fail_threshold=3, cooldown_seconds=0)
    assert out["event"] == "paused"
    assert calls["n"] == 0          # a deliberate pause (stop/deploy) must NOT be fought
    W.resume()
    assert not W.is_paused()


def test_breaker_opens_after_repeated_failed_restarts(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "_data_dir", lambda: tmp_path)
    _patch_pidfile(monkeypatch, 4321, 8765)
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=5.0: False)   # never recovers
    monkeypatch.setattr(W, "restart_server", lambda port, repo_root=None: {"ok": True})
    state = {"consecutive_failures": 0, "last_restart": 0.0}
    # Drive many ticks with no cooldown; each failed restart that doesn't recover should climb the
    # failed_restarts tally until the breaker opens.
    opened = False
    for _ in range(40):
        out = W.watch_once(state, fail_threshold=1, cooldown_seconds=0,
                           max_failed_restarts=5, breaker_backoff_seconds=600)
        if out["event"] in ("breaker_opened", "breaker_open"):
            opened = True
            break
    assert opened, "circuit breaker should open after repeated restarts that don't recover the server"
    assert state.get("breaker_until", 0) > 0


def test_breaker_closes_when_server_recovers(monkeypatch, tmp_path):
    import time
    monkeypatch.setattr(W, "_data_dir", lambda: tmp_path)
    _patch_pidfile(monkeypatch, 4321, 8765)
    # Breaker is open and not yet expired, but the server is now live again.
    monkeypatch.setattr(W, "probe_live", lambda port, timeout=5.0: True)
    state = {"breaker_until": time.time() + 600, "failed_restarts": 5, "consecutive_failures": 2}
    out = W.watch_once(state)
    assert out["event"] == "recovered_breaker_closed"
    assert state["breaker_until"] == 0.0
    assert state["failed_restarts"] == 0


def test_stale_pid_respawns_without_killing(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "_data_dir", lambda: tmp_path)
    _patch_pidfile(monkeypatch, 4321, 8765)
    monkeypatch.setattr(W, "_pid_is_running", lambda pid: False)   # server already exited
    killed = {"n": 0}
    monkeypatch.setattr(W, "_kill_pid", lambda pid, **kw: killed.__setitem__("n", 1))

    class _P:
        pid = 7777
    monkeypatch.setattr(W.subprocess, "Popen", lambda argv, **kw: _P())
    report = W.restart_server(8765, repo_root=tmp_path)
    assert report["ok"] is True
    assert killed["n"] == 0          # nothing to kill , the pid was already dead
    assert report["new_pid"] == 7777
