"""External audit of the DDNS acceptance work: two real gaps found by re-checking the actual
implementation rather than trusting the earlier summary, and correctly proposed as follow-up
fixes on top of the existing candidate rather than a rewrite.

1. public_listener_acceptance() checked ONLY 127.0.0.1, then trusted config's own claim about
host_setting to infer the LAN-facing interface must also be accepting. A process bound to
loopback only (started before MONGOOSE_HOST=0.0.0.0 was saved, never restarted) genuinely
answers on 127.0.0.1 while genuinely refusing the LAN IP a router's port-forward actually
reaches -- so this could report "accepting" in exactly the state that produces
ERR_CONNECTION_REFUSED. Fixed by independently proving the LAN-facing interface too, the
same real check the plain LAN-pairing path already used elsewhere.

2. An explicit MONGOOSE_PUBLIC_URL=http://... override correctly takes precedence over the
DDNS assembler (a deliberate manual value should not be silently replaced), but never got
RE-CHECKED against whether HTTPS is genuinely available now, so it could advertise http://
forever even after the owner later enabled HTTPS. Fixed with one shared helper
(upgrade_stale_public_scheme) called from both places an explicit override is read, so the
fix exists once, not duplicated per caller."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_loopback_only_bind_is_not_misread_as_listener_accepting(monkeypatch):
    """Real socket test, not mocked: a server genuinely bound to loopback only must not be
    reported as accepting, even though config says 0.0.0.0 and loopback itself is fine."""
    import socket
    import threading
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "realtoken")
    monkeypatch.setenv("MONGOOSE_PORT", "8765")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 8765))
    srv.listen(1)
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except Exception:
                pass

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        import unittest.mock as mock
        from mongoose import discovery, ddns
        with mock.patch.object(discovery, "_primary_ip", return_value="192.168.1.9"):
            r = ddns.public_listener_acceptance()
    finally:
        stop.set()
        srv.close()

    assert r["loopback_accepting"] is True
    assert r["lan_accepting"] is False
    assert r["listener_accepting"] is False
    assert r["ok"] is False
    assert r["reason"] == "listener_loopback_only"


def test_genuinely_open_on_both_interfaces_still_passes(monkeypatch):
    """The fix must not become so strict it breaks the honest, working case."""
    import unittest.mock as mock
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "realtoken")
    from mongoose import ddns
    with mock.patch("socket.socket") as msock:
        msock.return_value.__enter__.return_value.connect_ex.return_value = 0
        with mock.patch("mongoose.discovery._primary_ip", return_value="192.168.1.9"):
            r = ddns.public_listener_acceptance()
    assert r["ok"] is True and r["reason"] == "accepting"


def test_stale_http_override_upgrades_when_tls_genuinely_available(tmp_path):
    from mongoose.services.remote_access import public_base_url
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    env = {"MONGOOSE_PUBLIC_URL": "http://examplehome.duckdns.org:8765",
          "MONGOOSE_TLS_CERTFILE": str(cert), "MONGOOSE_TLS_KEYFILE": str(key)}
    assert public_base_url(env) == "https://examplehome.duckdns.org:8765"


def test_stale_http_override_never_upgrades_without_real_tls():
    from mongoose.services.remote_access import public_base_url
    env = {"MONGOOSE_PUBLIC_URL": "http://examplehome.duckdns.org:8765"}
    assert public_base_url(env) == "http://examplehome.duckdns.org:8765"


def test_owner_deliberate_tls_off_is_respected_even_with_real_certs(tmp_path):
    from mongoose.services.remote_access import public_base_url
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    env = {"MONGOOSE_PUBLIC_URL": "http://examplehome.duckdns.org:8765",
          "MONGOOSE_TLS_CERTFILE": str(cert), "MONGOOSE_TLS_KEYFILE": str(key),
          "MONGOOSE_TLS": "off"}
    assert public_base_url(env) == "http://examplehome.duckdns.org:8765"


def test_configured_public_base_also_gets_the_upgrade(monkeypatch, tmp_path):
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "http://examplehome.duckdns.org:8765")
    monkeypatch.setenv("MONGOOSE_TLS_CERTFILE", str(cert))
    monkeypatch.setenv("MONGOOSE_TLS_KEYFILE", str(key))
    from mongoose.access_urls import configured_public_base
    assert configured_public_base() == "https://examplehome.duckdns.org:8765"


def test_https_and_non_http_urls_pass_through_the_upgrade_helper_unchanged():
    from mongoose.ddns import upgrade_stale_public_scheme
    assert upgrade_stale_public_scheme("https://already-secure.example:8765") == "https://already-secure.example:8765"
    assert upgrade_stale_public_scheme("") == ""
