"""The remote-access (DDNS + TLS) readiness collector. Previously DDNS and cert state lived only in
their own modules and never reached the unified readiness picture; this guards that they do, and that
the items are well-formed and honest in both configured and unconfigured states."""
from __future__ import annotations

import mongoose.services.readiness as R


def test_collector_present_in_collect_all():
    # The collector must be wired into collect_all, else the gap reopens silently.
    assert R.collect_remote_access_readiness in R.collect_all.__globals__["collect_all"].__code__.co_consts \
        or "collect_remote_access_readiness" in R.collect_all.__code__.co_names


def test_unconfigured_yields_two_optional_items(monkeypatch):
    monkeypatch.setattr(R, "_now_iso", lambda: "2026-01-01T00:00:00Z")
    # No DDNS provider, no cert.
    import mongoose.ddns as ddns
    import mongoose.acme_tls as tls
    monkeypatch.setattr(ddns, "status", lambda env=None: {
        "provider": "", "provider_name": "", "kind": "", "configured": False,
        "needs_portforward": False, "last_ip": None, "enabled": False})
    monkeypatch.setattr(tls, "certificate_status", lambda: {"present": False, "host": "me.duckdns.org"})
    items = R.collect_remote_access_readiness()
    ids = {i.id for i in items}
    assert ids == {"ddns", "tls_cert"}
    for it in items:
        assert it.domain == "remote"
        assert it.status == R.NOT_CONFIGURED
        assert it.severity == "none"          # absent optional features are not "problems"
        assert it.next_action and it.next_action.get("command")  # a way to set it up


def test_configured_enabled_ddns_is_ready(monkeypatch):
    monkeypatch.setattr(R, "_now_iso", lambda: "2026-01-01T00:00:00Z")
    import mongoose.ddns as ddns
    import mongoose.acme_tls as tls
    monkeypatch.setattr(ddns, "status", lambda env=None: {
        "provider": "duckdns", "provider_name": "DuckDNS", "kind": "ddns", "configured": True,
        "needs_portforward": True, "last_ip": "203.0.113.5", "enabled": True})
    monkeypatch.setattr(tls, "certificate_status", lambda: {"present": False, "host": "me.duckdns.org"})
    d = next(i for i in R.collect_remote_access_readiness() if i.id == "ddns")
    assert d.status == R.READY and d.severity == "ok"
    assert "203.0.113.5" in d.why


def test_expired_cert_is_critical(monkeypatch):
    monkeypatch.setattr(R, "_now_iso", lambda: "2026-06-26T00:00:00Z")
    import mongoose.ddns as ddns
    import mongoose.acme_tls as tls
    monkeypatch.setattr(ddns, "status", lambda env=None: {
        "provider": "", "provider_name": "", "kind": "", "configured": False,
        "needs_portforward": False, "last_ip": None, "enabled": False})
    # Cert that expired in the past.
    monkeypatch.setattr(tls, "certificate_status", lambda: {
        "present": True, "host": "me.duckdns.org", "expires": "Jan  1 12:00:00 2020 GMT",
        "certfile": "/x/fullchain.pem"})
    cert = next(i for i in R.collect_remote_access_readiness() if i.id == "tls_cert")
    assert cert.status == R.FAILING and cert.severity == "critical"
    assert "EXPIRED" in cert.why


def test_cert_expiring_soon_is_degraded(monkeypatch):
    monkeypatch.setattr(R, "_now_iso", lambda: "2026-06-26T00:00:00Z")
    import mongoose.ddns as ddns
    import mongoose.acme_tls as tls
    from datetime import datetime, timezone, timedelta
    soon = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%b %d %H:%M:%S %Y GMT")
    monkeypatch.setattr(ddns, "status", lambda env=None: {
        "provider": "", "provider_name": "", "kind": "", "configured": False,
        "needs_portforward": False, "last_ip": None, "enabled": False})
    monkeypatch.setattr(tls, "certificate_status", lambda: {
        "present": True, "host": "h", "expires": soon, "certfile": "/x"})
    cert = next(i for i in R.collect_remote_access_readiness() if i.id == "tls_cert")
    assert cert.status == R.DEGRADED and cert.severity == "warning"


def test_collector_never_raises_on_broken_modules(monkeypatch):
    import mongoose.ddns as ddns
    import mongoose.acme_tls as tls
    monkeypatch.setattr(ddns, "status", lambda env=None: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(tls, "certificate_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    items = R.collect_remote_access_readiness()  # must not raise
    assert {i.id for i in items} == {"ddns", "tls_cert"}
    assert all(i.status == R.UNKNOWN for i in items)
