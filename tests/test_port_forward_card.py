"""Mongoose's friendly 'set up your router' card , the delightful, filled-in alternative to a bare
port-forward table. It should produce the exact router fields with the real LAN IP + port for a DDNS
provider that needs forwarding, and a happy 'nothing to do' for Tailscale/VPN or local-only."""
from __future__ import annotations

import mongoose.ddns as ddns


def test_ddns_provider_produces_filled_router_fields(monkeypatch):
    monkeypatch.setattr(ddns, "status", lambda env=None: {
        "provider": "duckdns", "provider_name": "DuckDNS", "kind": "ddns",
        "configured": True, "enabled": True, "needs_portforward": True, "last_ip": "81.2.3.4"})
    monkeypatch.setattr(ddns, "_discover_lan_ip", lambda: "192.168.1.30")
    monkeypatch.setattr(ddns, "_server_port", lambda: 8765)
    card = ddns.port_forward_card()
    assert card["needed"] is True
    f = card["fields"]
    assert f["service_name"] == "mongoose"
    assert f["server_ip_address"] == "192.168.1.30"
    assert f["start_port"] == 8765 and f["end_port"] == 8765
    assert f["translation_start_port"] == 8765 and f["translation_end_port"] == 8765
    assert f["protocol"] == "TCP" and f["status"] == "Enable"
    # warm, plain-language, names the rule and the action
    assert "port-forward" in card["speech"].lower()
    assert "8765" in card["speech"] and "192.168.1.30" in card["speech"]
    assert any("mongoose" in line.lower() for line in card["lines"])


def test_vpn_is_happy_no_forward(monkeypatch):
    monkeypatch.setattr(ddns, "status", lambda env=None: {
        "provider": "tailscale", "provider_name": "Tailscale", "kind": "vpn",
        "configured": True, "enabled": True, "needs_portforward": False, "last_ip": None})
    card = ddns.port_forward_card()
    assert card["needed"] is False
    assert card["fields"] is None
    assert "nothing to change" in card["speech"].lower() or "all set" in card["speech"].lower()


def test_local_only_says_nothing_needed(monkeypatch):
    monkeypatch.setattr(ddns, "status", lambda env=None: {
        "provider": "", "provider_name": "", "kind": "",
        "configured": False, "enabled": False, "needs_portforward": False, "last_ip": None})
    card = ddns.port_forward_card()
    assert card["needed"] is False
    assert "router" in card["speech"].lower()


def test_missing_lan_ip_still_gives_card(monkeypatch):
    monkeypatch.setattr(ddns, "status", lambda env=None: {
        "provider": "dynu", "provider_name": "Dynu", "kind": "ddns",
        "configured": True, "enabled": True, "needs_portforward": True, "last_ip": None})
    monkeypatch.setattr(ddns, "_discover_lan_ip", lambda: None)   # couldn't discover
    card = ddns.port_forward_card()
    assert card["needed"] is True
    # still gives a usable card, with a gentle note about finding the IP
    assert card["fields"]["server_ip_address"]
    assert "LAN IP" in card["fields"]["server_ip_address"] or card["fields"]["server_ip_address"]


def test_card_never_raises(monkeypatch):
    monkeypatch.setattr(ddns, "status", lambda env=None: (_ for _ in ()).throw(RuntimeError("boom")))
    # port_forward_card must now swallow a status() failure and return a safe shape, so the device
    # endpoint never 500s.
    card = ddns.port_forward_card()
    assert card["needed"] is False
    assert card["fields"] is None
    assert "speech" in card
