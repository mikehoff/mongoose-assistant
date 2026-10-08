"""Field bug: enable-phone decided whether to restart by reading CONFIG (.env), not by checking
whether the RUNNING process was actually listening on the LAN. A process started before .env
said MONGOOSE_HOST=0.0.0.0 stays genuinely loopback-only until restarted, but the old check
trusted config alone, returned {"already": true}, and never restarted anything -- silently
leaving a phone's QR code pointing at a LAN address nothing was listening on
(ERR_CONNECTION_REFUSED). Fix: a real TCP probe against the machine's own LAN IP:port proves
liveness before ever skipping the restart."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
import warnings

warnings.filterwarnings("ignore")


def test_config_claims_enabled_but_socket_dead_forces_restart(monkeypatch):
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "testtok123")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    import mongoose.app as A
    result = asyncio.run(A._enable_phone_and_schedule_restart({}))
    assert result.get("already") is not True, (
        "config-only trust returned; this is the exact ERR_CONNECTION_REFUSED trap"
    )
    assert result.get("restarting") is True


def test_genuinely_live_socket_skips_unneeded_restart(monkeypatch):
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "testtok123")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    import mongoose.app as A
    import mongoose.discovery as D
    from mongoose.settings import get_settings

    monkeypatch.setattr(D, "_primary_ip", lambda: "127.0.0.1")
    port = get_settings().port
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except socket.timeout:
                continue
            except Exception:
                break

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    time.sleep(0.1)
    try:
        result = asyncio.run(A._enable_phone_and_schedule_restart({}))
        assert result.get("already") is True
        assert result.get("restarting") is not True
    finally:
        stop.set()
        srv.close()
        t.join(timeout=1)


def test_force_always_restarts_regardless_of_live_state(monkeypatch):
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "testtok123")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    import mongoose.app as A
    result = asyncio.run(A._enable_phone_and_schedule_restart({"force": True}))
    assert result.get("restarting") is True
