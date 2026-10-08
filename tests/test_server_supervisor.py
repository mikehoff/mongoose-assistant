"""Supervisor (external watchdog) liveness + the cockpit's ability to detect and start it.

These cover the reliability fix for the failure where /cockpit/restart killed the server and nothing
brought it back: a heartbeat so liveness is detectable, a server that spawns the watchdog so the two
supervise each other, and the restart relaunching from a DETACHED helper that survives the self-kill.
"""
from __future__ import annotations

import inspect
import subprocess
import time

from fastapi.testclient import TestClient


def _fresh_settings(monkeypatch, tmp_path):
    """Point the watchdog's data dir at an isolated tmp dir and drop the settings cache."""
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    return S


def test_heartbeat_makes_supervised_true(monkeypatch, tmp_path):
    _fresh_settings(monkeypatch, tmp_path)
    from mongoose.services import server_watchdog as W
    assert W.seconds_since_heartbeat() is None
    assert W.is_supervised() is False
    W.write_heartbeat()
    age = W.seconds_since_heartbeat()
    assert age is not None and age < 5
    assert W.is_supervised() is True


def test_stale_heartbeat_reads_as_not_supervised(monkeypatch, tmp_path):
    _fresh_settings(monkeypatch, tmp_path)
    from mongoose.services import server_watchdog as W
    W._markers_dir()
    W._heartbeat_marker().write_text(f"{int(time.time()) - 100000} 999999999")
    assert W.is_supervised() is False


def test_recent_heartbeat_but_dead_pid_reads_as_not_supervised(monkeypatch, tmp_path):
    _fresh_settings(monkeypatch, tmp_path)
    from mongoose.services import server_watchdog as W
    W._markers_dir()
    # fresh timestamp, but a pid that is certainly not alive
    W._heartbeat_marker().write_text(f"{int(time.time())} 2147483646")
    assert W.is_supervised() is False


def test_supervisor_status_shape(monkeypatch, tmp_path):
    _fresh_settings(monkeypatch, tmp_path)
    from mongoose.services import server_watchdog as W
    st = W.supervisor_status()
    assert {"running", "age_seconds", "ever_seen", "paused"}.issubset(st.keys())
    assert st["running"] is False and st["ever_seen"] is False
    W.write_heartbeat()
    st2 = W.supervisor_status()
    assert st2["running"] is True and st2["ever_seen"] is True


def test_spawn_detached_noop_when_supervised(monkeypatch, tmp_path):
    _fresh_settings(monkeypatch, tmp_path)
    from mongoose.services import server_watchdog as W
    W.write_heartbeat()
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    res = W.spawn_detached("test")
    assert res.get("already_running") is True
    assert calls == []  # must NOT spawn a process when one is already supervising


def test_spawn_detached_spawns_detached_watchdog_when_absent(monkeypatch, tmp_path):
    _fresh_settings(monkeypatch, tmp_path)
    from mongoose.services import server_watchdog as W

    class _FakeProc:
        pid = 4242

    captured = {}

    def _fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    res = W.spawn_detached("unit")
    assert res.get("ok") is True and res.get("spawned_pid") == 4242
    assert "server-watchdog" in captured["argv"]          # starts the external watchdog
    assert captured["kw"].get("start_new_session") is True  # detached, so it outlives the server


def test_run_forever_writes_a_heartbeat_before_looping(monkeypatch, tmp_path):
    _fresh_settings(monkeypatch, tmp_path)
    from mongoose.services import server_watchdog as W
    # Stop the loop after the first watch_once so the test returns; assert a heartbeat exists.
    def _one(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(W, "watch_once", _one)
    try:
        W.run_forever(interval_seconds=0.01)
    except KeyboardInterrupt:
        pass
    assert W.seconds_since_heartbeat() is not None  # stamped on start / first iteration


def test_cockpit_supervisor_endpoint_reports_status_and_start_is_nonce_gated(monkeypatch, tmp_path):
    _fresh_settings(monkeypatch, tmp_path)
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/cockpit/supervisor")
    assert r.status_code == 200
    assert r.json().get("running") is False
    # the start action mutates, so it must require the cockpit nonce even from loopback
    assert c.post("/cockpit/supervisor/start").status_code == 403


def test_restart_relaunches_from_detached_helper_stop_before_serve():
    """The restart must spawn a DETACHED helper that stops the old server BEFORE starting a new one ,
    rather than running stop inline (which killed the process before it could relaunch)."""
    import mongoose.app as A
    src = inspect.getsource(A.cockpit_restart)
    assert '"-c", helper' in src                 # relaunch runs in a separate python -c helper
    assert "start_new_session=True" in src       # detached, survives this server being killed
    assert src.index("'stop'") < src.index("'serve'")  # helper stops before it serves


def test_serve_ensures_watchdog_and_is_opt_outable():
    """serve spawns the watchdog (mutual supervision) and respects MONGOOSE_NO_WATCHDOG."""
    import mongoose.cli as C
    src = inspect.getsource(C)
    assert "spawn_detached" in src
    assert "MONGOOSE_NO_WATCHDOG" in src


def test_probe_live_tries_https_first_then_http(monkeypatch):
    """The server is HTTPS by default; probe_live must try https (verify off) before http, or the
    watchdog would read a healthy https server as dead and restart-loop it."""
    import mongoose.services.server_watchdog as W
    seen = []

    def _get(url, **kwargs):
        seen.append(url)
        assert kwargs.get("verify") is False  # loopback self-signed cert must be accepted
        raise RuntimeError("down")

    monkeypatch.setattr(W.httpx, "get", _get)
    assert W.probe_live(8765, timeout=0.1) is False
    assert seen and seen[0].startswith("https://")
    assert any(u.startswith("http://") for u in seen)
