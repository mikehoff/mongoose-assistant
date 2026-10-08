"""The pairing URL given to a phone must be reachable from the phone: when the cockpit is opened on
the Mac (loopback host), the pair URL must use the Mac's LAN IP, not 127.0.0.1."""
from unittest.mock import MagicMock


def test_loopback_host_becomes_lan_ip(monkeypatch):
    import mongoose.app as A
    monkeypatch.setattr("mongoose.discovery._primary_ip", lambda: "192.168.1.42")
    req = MagicMock()
    req.headers = {"host": "127.0.0.1:8765"}
    host = A._phone_reachable_host(req)
    assert host == "192.168.1.42:8765"
    assert "127.0.0.1" not in host


def test_localhost_host_becomes_lan_ip(monkeypatch):
    import mongoose.app as A
    monkeypatch.setattr("mongoose.discovery._primary_ip", lambda: "192.168.1.42")
    req = MagicMock()
    req.headers = {"host": "localhost:8765"}
    assert A._phone_reachable_host(req) == "192.168.1.42:8765"


def test_real_lan_host_is_kept(monkeypatch):
    import mongoose.app as A
    req = MagicMock()
    req.headers = {"host": "192.168.1.99:8765"}
    # already a usable LAN address; must not be rewritten
    assert A._phone_reachable_host(req) == "192.168.1.99:8765"


def test_falls_back_gracefully_when_no_lan_ip(monkeypatch):
    import mongoose.app as A
    monkeypatch.setattr("mongoose.discovery._primary_ip", lambda: None)
    req = MagicMock()
    req.headers = {"host": "127.0.0.1:8765"}
    # no LAN IP detectable -> keep the loopback host rather than crash
    assert A._phone_reachable_host(req) == "127.0.0.1:8765"
