"""ddns.reachability_diagnosis(): distinguishes the two independent, actionable root causes
of a DDNS address refusing a connection, since "ERR_CONNECTION_REFUSED" alone tells an owner
nothing they can act on. DNS staleness (the updater fell behind) and hairpin/no-portforward
(a router limitation) need different explanations and different next steps, and conflating
them into one generic failure is exactly how a real, fixable problem reads as an
unexplained dead end."""
from __future__ import annotations

import unittest.mock as mock
import warnings

warnings.filterwarnings("ignore")


def test_reachable_when_dns_correct_and_self_connect_succeeds():
    from mongoose import ddns
    env = {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname", "MONGOOSE_PORT": "8765"}
    with mock.patch.object(ddns, "public_listener_acceptance", return_value={"ok": True, "listener_accepting": True}), \
         mock.patch("socket.gethostbyname", return_value="203.0.113.5"), \
         mock.patch.object(ddns, "detect_public_ip", return_value="203.0.113.5"), \
         mock.patch("socket.socket") as mock_sock:
        mock_sock.return_value.__enter__.return_value.connect_ex.return_value = 0
        d = ddns.reachability_diagnosis(env)
    assert d["ok"] is True and d["reason"] == "reachable"


def test_dns_stale_detected_independently_of_the_hairpin_check():
    from mongoose import ddns
    env = {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname", "MONGOOSE_PORT": "8765"}
    with mock.patch.object(ddns, "public_listener_acceptance", return_value={"ok": True, "listener_accepting": True}), \
         mock.patch("socket.gethostbyname", return_value="198.51.100.9"), \
         mock.patch.object(ddns, "detect_public_ip", return_value="203.0.113.5"):
        d = ddns.reachability_diagnosis(env)
    assert d["ok"] is False and d["reason"] == "dns_stale"
    assert d["dns_matches"] is False


def test_hairpin_or_no_portforward_distinguished_from_dns_stale():
    from mongoose import ddns
    env = {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname", "MONGOOSE_PORT": "8765"}
    with mock.patch.object(ddns, "public_listener_acceptance", return_value={"ok": True, "listener_accepting": True}), \
         mock.patch("socket.gethostbyname", return_value="203.0.113.5"), \
         mock.patch.object(ddns, "detect_public_ip", return_value="203.0.113.5"), \
         mock.patch("socket.socket") as mock_sock:
        mock_sock.return_value.__enter__.return_value.connect_ex.return_value = 111
        d = ddns.reachability_diagnosis(env)
    assert d["ok"] is False and d["reason"] == "hairpin_or_no_portforward"
    assert d["dns_matches"] is True


def test_listener_not_accepting_is_distinguished_from_hairpin(monkeypatch):
    """The real gap this fix closes: a self-connect to the PUBLIC hostname:port fails
    identically whether the router can't loop back, the port-forward is missing, OR
    Mongoose's own process simply isn't listening. Those need different fixes (a restart vs
    a router change), and blaming the router when the real cause is Mongoose's own stale
    process sends the owner on the wrong errand. This must be checked, and correctly named,
    BEFORE any DNS/router-side check ever runs."""
    from mongoose import ddns
    env = {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname", "MONGOOSE_PORT": "8765"}
    with mock.patch.object(ddns, "public_listener_acceptance",
                          return_value={"ok": False, "listener_accepting": False,
                                       "reason": "bound_loopback_only"}):
        d = ddns.reachability_diagnosis(env)
    assert d["ok"] is False and d["reason"] == "listener_not_accepting"
    assert d["listener_accepting"] is False
    # And confirm it never even reaches the DNS/hairpin checks when the listener already
    # failed: dns_ip should still be unset, since there was no reason to resolve anything yet.
    assert d["dns_ip"] is None


def test_not_configured_returns_cleanly_with_no_probing():
    from mongoose import ddns
    d = ddns.reachability_diagnosis({})
    assert d["ok"] is False and d["reason"] == "not_configured"


def test_dns_does_not_resolve_at_all():
    from mongoose import ddns
    env = {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "nonexistent-xyz", "MONGOOSE_PORT": "8765"}
    with mock.patch.object(ddns, "public_listener_acceptance", return_value={"ok": True, "listener_accepting": True}), \
         mock.patch("socket.gethostbyname", side_effect=OSError("not found")):
        d = ddns.reachability_diagnosis(env)
    assert d["ok"] is False and d["reason"] == "dns_does_not_resolve"


def test_setup_state_remote_away_uses_the_real_diagnosis_not_config_existence(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    from mongoose.db import init_db
    init_db()
    import mongoose.acme_tls as acme
    monkeypatch.setattr(acme, "certificate_status", lambda: {"present": True})
    from mongoose import ddns
    with mock.patch.object(ddns, "reachability_diagnosis",
                           return_value={"ok": False, "reason": "hairpin_or_no_portforward"}):
        from mongoose.setup_state import setup_state
        s = setup_state(only=["remote_away"])["surfaces"]["remote_away"]
    assert s["ok"] is False and s["status"] == "not_reachable_from_here"
    assert "router" in s["detail"]


def test_guest_invites_does_not_hard_block_on_hairpin_ambiguity(monkeypatch, tmp_path):
    """Hairpin specifically does not affect a genuinely external guest; treating the
    self-test's ambiguous result as a hard block would frustrate a probably-working invite."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "g.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    from mongoose.db import init_db
    init_db()
    import mongoose.acme_tls as acme
    monkeypatch.setattr(acme, "certificate_status", lambda: {"present": True})
    from mongoose import ddns
    with mock.patch.object(ddns, "reachability_diagnosis",
                           return_value={"ok": False, "reason": "hairpin_or_no_portforward"}):
        from mongoose.setup_state import setup_state
        g = setup_state(only=["guest_invites"])["surfaces"]["guest_invites"]
    assert g["ok"] is True and g["status"] == "ready_unverified"


def test_guest_invites_still_blocks_on_genuine_dns_staleness(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "g2.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    from mongoose.db import init_db
    init_db()
    import mongoose.acme_tls as acme
    monkeypatch.setattr(acme, "certificate_status", lambda: {"present": True})
    from mongoose import ddns
    with mock.patch.object(ddns, "reachability_diagnosis", return_value={"ok": False, "reason": "dns_stale"}):
        from mongoose.setup_state import setup_state
        g = setup_state(only=["guest_invites"])["surfaces"]["guest_invites"]
    assert g["ok"] is False and g["status"] == "blocked"
