"""mongoose ddns diagnose: the reachability_diagnosis()/public_listener_acceptance() logic
built earlier tonight was never reachable from any command a person could actually run --
only from the live web app's own routes. When asked to help diagnose a real, live
ERR_CONNECTION_REFUSED, the only command that existed for this (a suggested `mongoose ddns
diagnose`) turned out not to exist at all; only `setup`/`update`/`status` were registered,
and `status` itself never called any of this diagnostic logic. This is the fix: a real,
registered subcommand wired to the actual diagnosis, so the next time someone is mid-
incident, there is a real command to hand them instead of a raw Python one-liner."""
from __future__ import annotations

import warnings
import unittest.mock as mock

warnings.filterwarnings("ignore")


def _run_diagnose(monkeypatch, tmp_path, **extra_env):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    import sys
    monkeypatch.setattr(sys, "argv", ["mongoose", "ddns", "diagnose"])
    from mongoose.cli import main
    return main()


def test_diagnose_is_a_real_registered_subcommand():
    import inspect
    import mongoose.cli as cli_mod
    src = inspect.getsource(cli_mod)
    assert 'ddns_sub.add_parser("diagnose"' in src


def test_diagnose_with_no_ddns_configured(monkeypatch, tmp_path, capsys):
    rc = _run_diagnose(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert "mongoose ddns setup" in out


def test_diagnose_flags_loopback_only_listener(monkeypatch, tmp_path, capsys):
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"loopback_accepting": True, "lan_accepting": False,
                                "listener_accepting": False}):
        rc = _run_diagnose(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                          DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAILING" in out
    assert "not your router" in out


def test_diagnose_flags_dns_stale(monkeypatch, tmp_path, capsys):
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"loopback_accepting": True, "lan_accepting": True,
                                "listener_accepting": True}), \
         mock.patch("mongoose.ddns.reachability_diagnosis",
                   return_value={"ok": False, "reason": "dns_stale",
                                "hostname": "yourname.duckdns.org", "dns_ip": "9.9.9.9",
                                "actual_public_ip": "1.2.3.4"}):
        rc = _run_diagnose(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                          DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    out = capsys.readouterr().out
    assert rc == 1
    assert "STALE" in out
    assert "ddns update --force" in out


def test_diagnose_flags_hairpin_or_no_portforward(monkeypatch, tmp_path, capsys):
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"loopback_accepting": True, "lan_accepting": True,
                                "listener_accepting": True}), \
         mock.patch("mongoose.ddns.reachability_diagnosis",
                   return_value={"ok": False, "reason": "hairpin_or_no_portforward"}):
        rc = _run_diagnose(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                          DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT REACHABLE" in out
    assert "MONGOOSE_PUBLIC_PORT" in out


def test_diagnose_reports_reachable_honestly(monkeypatch, tmp_path, capsys):
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"loopback_accepting": True, "lan_accepting": True,
                                "listener_accepting": True}), \
         mock.patch("mongoose.ddns.reachability_diagnosis",
                   return_value={"ok": True, "reason": "reachable"}):
        rc = _run_diagnose(monkeypatch, tmp_path, DDNS_PROVIDER="duckdns",
                          DDNS_DUCKDNS_DOMAIN="yourname", DDNS_DUCKDNS_TOKEN="t")
    out = capsys.readouterr().out
    assert rc == 0
    assert "reachable" in out.lower()
    assert "cannot fully prove" in out
