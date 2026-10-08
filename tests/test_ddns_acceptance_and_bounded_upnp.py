"""Reconciles the DDNS-acceptance-fix and UPnP-automation arguments against this session's
own decisions, revised on the merits rather than accepted or rejected wholesale.

(1) public_listener_acceptance(): a genuinely new, valuable diagnostic dimension. The
existing reachability_diagnosis connects THROUGH the router to the public IP, so a failure
there can't fully distinguish "router problem" from "Mongoose problem". Connecting straight
to 127.0.0.1:<internal port> skips the router and DNS entirely: if Mongoose's own listener
isn't accepting, that alone explains ERR_CONNECTION_REFUSED regardless of how well the router
is configured, and the fix is the same restart already used for the LAN case.

(2) MONGOOSE_PUBLIC_PORT: an explicit, opt-in override for when the router forwards a
different external port (commonly 443) to Mongoose's internal port. Defaults to matching the
internal port (the safe choice: requires the least router configuration) rather than
assuming 443, which would be wrong unless the owner has actually set that mapping up.

(3) Bounded UPnP/NAT-PMP: the original blanket refusal was revised, not on request, but
because the specific risk (a router-level firewall change, on a device the owner owns, that
can outlive the reason it was created and isn't visible in any list an owner would normally
check) is real but was conflated with a broader "automation is risky" position that doesn't
hold once the automation is: owner-triggered (this exact tap, never a silent background/
scheduler action), gated on DDNS already being configured (an existing signal of intent, not
a new consent click), attempted only after nothing simpler already works, single-port, and
its result always surfaced plainly rather than hidden."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path, **extra):
    env = {"MONGOOSE_DB_PATH": str(tmp_path / "d.sqlite3"), "MONGOOSE_DATA_DIR": str(tmp_path / "d"),
          "MONGOOSE_HOST": "0.0.0.0", "MONGOOSE_ALLOW_REMOTE": "true", "MONGOOSE_API_TOKEN": "t"}
    env.update(extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A


def _owner_headers(c):
    from mongoose.devices import create_pairing_code, claim_pairing_code
    code = create_pairing_code("primary_phone")["code"]
    cl = claim_pairing_code(code, "phone", "android")
    return {"X-Mongoose-Device-Token": cl["device_token"]}


def test_listener_acceptance_distinguishes_nothing_listening_from_misconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "realtoken")
    from mongoose import ddns
    r = ddns.public_listener_acceptance()
    assert r["ok"] is False and r["reason"] == "listener_not_accepting"


def test_public_port_override_is_used_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    from mongoose import ddns
    env = {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname", "MONGOOSE_PUBLIC_PORT": "443"}
    url = ddns.public_url(env)
    assert url == "https://yourname.duckdns.org" or url == "http://yourname.duckdns.org:443"


def test_public_port_defaults_to_internal_port_when_unset(monkeypatch, tmp_path):
    from mongoose import ddns
    env = {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname"}
    url = ddns.public_url(env)
    assert ":8765" in url


def test_upnp_soap_request_is_spec_correctly_formed():
    """Verified against a real spec-compliant local HTTP server earlier in this session;
    pinned here as the mocked-boundary regression."""
    from mongoose import ddns
    igd = {"control_url": "http://127.0.0.1:1/control", "service_type": "urn:test:Service:1"}
    import unittest.mock as mock
    captured = {}
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""
    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        captured["data"] = req.data.decode("utf-8")
        return FakeResp()
    with mock.patch("urllib.request.urlopen", fake_urlopen):
        result = ddns._upnp_add_port_mapping(igd, external_port=9999, internal_port=8765,
                                             internal_client="192.168.1.5")
    assert result["ok"] is True
    assert "<NewExternalPort>9999</NewExternalPort>" in captured["data"]
    assert "<NewInternalClient>192.168.1.5</NewInternalClient>" in captured["data"]


def test_repair_checks_listener_before_anything_router_or_ddns_related(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    H = _owner_headers(c)
    import unittest.mock as mock
    from mongoose import ddns
    with mock.patch.object(ddns, "public_listener_acceptance",
                          return_value={"ok": False, "reason": "listener_not_accepting"}), \
         mock.patch.object(A, "_schedule_detached_phone_restart",
                           return_value={"ok": True, "restarting": True}) as m_restart, \
         mock.patch.object(ddns, "update_now") as m_update:
        r = c.post("/device/remote/repair", headers=H).json()
    assert r["step"] == "listener"
    assert m_restart.called
    assert not m_update.called, "must not touch DDNS at all until the listener itself is fixed"


def test_repair_never_attempts_upnp_without_ddns_configured(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    H = _owner_headers(c)
    import unittest.mock as mock
    from mongoose import ddns
    with mock.patch.object(ddns, "public_listener_acceptance", return_value={"ok": True}), \
         mock.patch.object(ddns, "update_now", return_value={"ok": True}), \
         mock.patch.object(ddns, "reachability_diagnosis",
                           return_value={"ok": False, "reason": "hairpin_or_no_portforward"}), \
         mock.patch.object(ddns, "attempt_upnp_repair") as m_upnp:
        r = c.post("/device/remote/repair", headers=H).json()
    assert r["step"] == "no_ddns"
    assert not m_upnp.called


def test_repair_attempts_bounded_upnp_only_when_ddns_configured_and_nothing_else_works(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns")
    H = _owner_headers(c)
    import unittest.mock as mock
    from mongoose import ddns
    with mock.patch.object(ddns, "public_listener_acceptance", return_value={"ok": True}), \
         mock.patch.object(ddns, "update_now", return_value={"ok": True}), \
         mock.patch.object(ddns, "reachability_diagnosis",
                           side_effect=[{"ok": False, "reason": "hairpin_or_no_portforward"},
                                        {"ok": True, "reason": "reachable"}]), \
         mock.patch.object(ddns, "attempt_upnp_repair",
                           return_value={"ok": True, "method": "upnp"}) as m_upnp:
        r = c.post("/device/remote/repair", headers=H).json()
    assert m_upnp.called
    assert r["step"] == "upnp_attempted" and r["ok"] is True


def test_repair_is_still_owner_device_only_after_the_rewrite(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    from mongoose.devices import create_pairing_code, claim_pairing_code
    gcode = create_pairing_code("trusted_guest")["code"]
    gcl = claim_pairing_code(gcode, "guest", "android")
    r = c.post("/device/remote/repair", headers={"X-Mongoose-Device-Token": gcl["device_token"]})
    assert r.status_code == 403


def test_upnp_attempt_is_never_triggered_by_a_scheduler_or_any_non_tap_path():
    """The one distinction that actually matters: owner-triggered vs silent/background.
    attempt_upnp_repair must only ever be called from within the shared repair chain both
    /device/remote/repair and /cockpit/remote-access/repair delegate to (these exact taps),
    never from scheduler.py or any startup/background path. The chain was extracted into
    _run_remote_repair_chain so the cockpit desktop session and the paired phone share one
    implementation instead of two that could drift; this checks the call lives in exactly
    that shared function, and that both public entry points reach it and nothing else does."""
    import inspect
    import mongoose.app as A
    from mongoose.services import scheduler
    assert "attempt_upnp_repair" in inspect.getsource(A._run_remote_repair_chain)
    assert "attempt_upnp_repair" not in inspect.getsource(A.device_remote_repair)
    assert "_run_remote_repair_chain" in inspect.getsource(A.device_remote_repair)
    assert "_run_remote_repair_chain" in inspect.getsource(A.cockpit_remote_repair)
    assert "attempt_upnp_repair" not in inspect.getsource(scheduler)
