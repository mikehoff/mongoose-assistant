"""The updater proved code/disk coherence and that /health answers on loopback, but never
actually checked whether the owner's phone could reach the LAN-facing socket or a
configured DDNS route -- exactly the gap an external audit found: "mongoose update can
install the patched code, but unless the updater actively... proves LAN listener
acceptance, and proves or prepares the DDNS route, it can still leave you with the same
ERR_CONNECTION_REFUSED class of failure." owner_network_postflight() closes that gap by
wiring the already-tested diagnostic primitives (ensure_owner_baseline,
public_listener_acceptance, reachability_diagnosis) into the update path, where none of
them were being called before."""
from __future__ import annotations

import warnings
import unittest.mock as mock

warnings.filterwarnings("ignore")


def test_ok_when_everything_genuinely_accepting_and_no_ddns(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    from mongoose import updater
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"ok": True, "listener_accepting": True,
                                "loopback_accepting": True, "lan_accepting": True}):
        r = updater.owner_network_postflight()
    assert r["ok"] is True
    assert r["ddns_configured"] is False
    assert any("accepting on both loopback" in l for l in r["lines"])


def test_flags_the_exact_loopback_only_failure_class(monkeypatch, tmp_path):
    """The precise scenario the audit named: loopback answers, LAN-facing does not."""
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    from mongoose import updater
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"ok": False, "listener_accepting": False,
                                "loopback_accepting": True, "lan_accepting": False,
                                "reason": "listener_loopback_only"}):
        r = updater.owner_network_postflight()
    assert r["ok"] is False
    assert any("FAILING" in l and "not loopback" in l for l in r["lines"])


def test_flags_a_configured_but_unreachable_ddns_route(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    from mongoose import updater
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"ok": True, "listener_accepting": True,
                                "loopback_accepting": True, "lan_accepting": True}), \
         mock.patch("mongoose.ddns.reachability_diagnosis",
                   return_value={"ok": False, "reason": "hairpin_or_no_portforward",
                                "hostname": "yourname.duckdns.org"}):
        r = updater.owner_network_postflight()
    assert r["ok"] is False
    assert r["ddns_configured"] is True
    assert any("hairpin_or_no_portforward" in l for l in r["lines"])


def test_passes_when_ddns_configured_and_genuinely_reachable(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    from mongoose import updater
    with mock.patch("mongoose.ddns.public_listener_acceptance",
                   return_value={"ok": True, "listener_accepting": True,
                                "loopback_accepting": True, "lan_accepting": True}), \
         mock.patch("mongoose.ddns.reachability_diagnosis",
                   return_value={"ok": True, "reason": "reachable",
                                "hostname": "yourname.duckdns.org"}):
        r = updater.owner_network_postflight()
    assert r["ok"] is True
    assert r["ddns_configured"] is True


def test_never_crashes_the_update_if_the_postflight_itself_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    from mongoose import updater
    with mock.patch("mongoose.ddns.public_listener_acceptance", side_effect=RuntimeError("boom")):
        r = updater.owner_network_postflight()
    assert r["ok"] is False
    assert "ddns_configured" in r
