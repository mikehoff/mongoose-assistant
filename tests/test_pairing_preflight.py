"""Pairing preflight + URL-scheme sanity (from the QA failure report).

The onboarding bug: the install/QR link advertised https:// when the server was HTTP-only (so the phone
aborted before the app saw the request), and LAN/public URLs were offered without context or reachability
status. These tests pin the fixes the report asked for: scheme matches actual TLS state, LAN URL is
suppressed when bound to localhost, preflight reports degraded state instead of throwing, public URL is
marked unverified, and same-Wi-Fi vs cellular are distinct routes.
"""
from __future__ import annotations

import mongoose.ddns as ddns
import mongoose.services.pairing_preflight as PF


def _http_env():
    return {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname", "MONGOOSE_PORT": "8765"}


# F1: scheme is http when HTTPS disabled
def test_public_url_is_http_when_tls_disabled(monkeypatch):
    monkeypatch.delenv("MONGOOSE_TLS_CERTFILE", raising=False)
    monkeypatch.delenv("MONGOOSE_TLS_KEYFILE", raising=False)
    url = ddns.public_url(_http_env())
    assert url.startswith("http://"), f"HTTP-only server must advertise http://, got {url}"
    assert not url.startswith("https://")


# F2: https only when TLS actually enabled (cert+key exist)
def test_public_url_is_https_only_when_tls_enabled(tmp_path):
    cert = tmp_path / "c.pem"; cert.write_text("x")
    key = tmp_path / "k.pem"; key.write_text("x")
    env = dict(_http_env(), MONGOOSE_TLS_CERTFILE=str(cert), MONGOOSE_TLS_KEYFILE=str(key))
    url = ddns.public_url(env)
    assert url.startswith("https://"), f"TLS-enabled server should advertise https://, got {url}"


def test_tls_paths_set_but_files_missing_is_not_https(monkeypatch):
    env = dict(_http_env(), MONGOOSE_TLS_CERTFILE="/no/cert.pem", MONGOOSE_TLS_KEYFILE="/no/key.pem")
    assert ddns.tls_is_enabled(env) is False
    assert ddns.public_url(env).startswith("http://")


# F3: LAN URL suppressed when bound to localhost only
def test_lan_url_suppressed_when_loopback_only(monkeypatch):
    monkeypatch.setattr(PF, "_configured_bind_host", lambda: "127.0.0.1")
    monkeypatch.setattr(PF, "_bound_addresses", lambda port: ["127.0.0.1:8765"])
    monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: False)
    monkeypatch.setattr(PF, "_ddns_host", lambda: None)
    pf = PF.preflight(verify_local=False)
    assert pf["server"]["loopback_only"] is True
    # no same-Wi-Fi URL should be offered
    assert not any("same Wi-Fi" in u["label"] for u in pf["urls"])
    # and there must be a clear warning saying so
    assert any("127.0.0.1" in w for w in pf["warnings"])


# F6: preflight reports degraded instead of throwing
def test_preflight_never_throws(monkeypatch):
    monkeypatch.setattr(PF, "_lan_ip", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    # Even with an exploding probe, preflight must return a dict, not raise.
    try:
        pf = PF.preflight(verify_local=False)
        assert isinstance(pf, dict)
    except Exception as exc:
        raise AssertionError(f"preflight must not raise, but raised {exc}")


# F8: public URL marked unverified
def test_public_url_marked_unverified(monkeypatch):
    monkeypatch.setattr(PF, "_configured_bind_host", lambda: "0.0.0.0")
    monkeypatch.setattr(PF, "_bound_addresses", lambda port: ["0.0.0.0:8765"])
    monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: False)
    monkeypatch.setattr(PF, "_ddns_host", lambda: "yourname.duckdns.org")
    pf = PF.preflight(verify_local=False)
    pub = next(u for u in pf["urls"] if "away" in u["label"].lower())
    assert pub["status"] == "not_verified"


# F9: same-Wi-Fi and cellular/public are distinct routes
def test_same_wifi_and_public_are_distinct_routes(monkeypatch):
    monkeypatch.setattr(PF, "_configured_bind_host", lambda: "0.0.0.0")
    monkeypatch.setattr(PF, "_bound_addresses", lambda port: ["0.0.0.0:8765"])
    monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: False)
    monkeypatch.setattr(PF, "_ddns_host", lambda: "yourname.duckdns.org")
    pf = PF.preflight(verify_local=False)
    labels = [u["label"] for u in pf["urls"]]
    assert any("same Wi-Fi" in l for l in labels)
    assert any("away" in l.lower() or "cellular" in l.lower() for l in labels)
    # and they are different URLs
    wifi = next(u["url"] for u in pf["urls"] if "same Wi-Fi" in u["label"])
    pub = next(u["url"] for u in pf["urls"] if "away" in u["label"].lower())
    assert wifi != pub


# F7: HTTP-only pairing carries the HTTPS-upgrade warning
def test_http_only_warns_about_https_upgrade(monkeypatch):
    monkeypatch.setattr(PF, "_configured_bind_host", lambda: "0.0.0.0")
    monkeypatch.setattr(PF, "_bound_addresses", lambda port: ["0.0.0.0:8765"])
    monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: False)
    monkeypatch.setattr(PF, "_ddns_host", lambda: None)
    pf = PF.preflight(verify_local=False)
    assert any("http-only" in w.lower() or "https-first" in w.lower() or "https" in w.lower()
               for w in pf["warnings"])


# Cert-host mismatch is surfaced, not hidden
def test_cert_host_mismatch_warned(monkeypatch, tmp_path):
    monkeypatch.setattr(PF, "_configured_bind_host", lambda: "0.0.0.0")
    monkeypatch.setattr(PF, "_bound_addresses", lambda port: ["0.0.0.0:8765"])
    monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: True)
    monkeypatch.setattr(PF, "_ddns_host", lambda: "yourname.duckdns.org")
    monkeypatch.setattr(PF, "_cert_host", lambda: "someone-else.duckdns.org")
    pf = PF.preflight(verify_local=False)
    assert any("mismatch" in w.lower() or "warn" in w.lower() for w in pf["warnings"])


# ── macOS firewall detection (Dispatch: inbound connections dropped by the firewall) ────────────────

def test_firewall_state_graceful_off_macos(monkeypatch):
    import mongoose.services.pairing_preflight as PF
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    fw = PF._macos_firewall_state()
    assert fw["enabled"] is None          # unknown / not applicable off macOS
    assert "not macOS" in fw["detail"]


def test_preflight_includes_firewall_fields(monkeypatch):
    import mongoose.services.pairing_preflight as PF
    monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(PF, "_bound_addresses", lambda port: ["0.0.0.0:8765"])
    monkeypatch.setattr(PF, "_configured_bind_host", lambda: "0.0.0.0")
    monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: False)
    monkeypatch.setattr(PF, "_ddns_host", lambda: None)
    pf = PF.preflight(verify_local=False)
    assert "firewall" in pf and "firewall_fix" in pf


def test_firewall_on_and_blocking_yields_warning_and_fix(monkeypatch):
    import mongoose.services.pairing_preflight as PF
    monkeypatch.setattr(PF, "_macos_firewall_state", lambda: {
        "platform": "Darwin", "enabled": True, "stealth": False,
        "blocked_binary": True, "binary": "/usr/bin/python3", "detail": "macOS firewall is ON"})
    monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(PF, "_bound_addresses", lambda port: ["0.0.0.0:8765"])
    monkeypatch.setattr(PF, "_configured_bind_host", lambda: "0.0.0.0")
    monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: False)
    monkeypatch.setattr(PF, "_ddns_host", lambda: None)
    pf = PF.preflight(verify_local=False)
    assert any("firewall" in w.lower() and "blocking" in w.lower() for w in pf["warnings"])
    assert pf["firewall_fix"] and "socketfilterfw" in pf["firewall_fix"]
    assert "--unblockapp" in pf["firewall_fix"]


def test_firewall_off_produces_no_firewall_warning(monkeypatch):
    import mongoose.services.pairing_preflight as PF
    monkeypatch.setattr(PF, "_macos_firewall_state", lambda: {
        "platform": "Darwin", "enabled": False, "stealth": False,
        "blocked_binary": None, "binary": "/usr/bin/python3", "detail": "macOS firewall is off"})
    monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(PF, "_bound_addresses", lambda port: ["0.0.0.0:8765"])
    monkeypatch.setattr(PF, "_configured_bind_host", lambda: "0.0.0.0")
    monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: False)
    monkeypatch.setattr(PF, "_ddns_host", lambda: None)
    pf = PF.preflight(verify_local=False)
    assert pf["firewall_fix"] is None
    assert not any("firewall is ON" in w for w in pf["warnings"])


def test_firewall_fix_is_shell_safe_for_paths_with_quotes(monkeypatch):
    """A Mac username with an apostrophe (e.g. /Users/O'Brien/python3) , or any path with shell
    metacharacters , must not produce a broken or injectable copy-paste command. The fix string is
    pasted by the user into a shell, so the binary path must be shlex.quoted."""
    import shlex
    import mongoose.services.pairing_preflight as PF
    for binary in ["/Users/O'Brien/python3", "/tmp/p'; rm -rf ~ #", "/Users/My Name/.venv/bin/python"]:
        monkeypatch.setattr(PF, "_macos_firewall_state", lambda b=binary: {
            "platform": "Darwin", "enabled": True, "stealth": False,
            "blocked_binary": True, "binary": b, "detail": "on"})
        monkeypatch.setattr(PF, "_lan_ip", lambda: "192.168.1.5")
        monkeypatch.setattr(PF, "_bound_addresses", lambda p: ["0.0.0.0:8765"])
        monkeypatch.setattr(PF, "_configured_bind_host", lambda: "0.0.0.0")
        monkeypatch.setattr(PF, "_tls_enabled", lambda env=None: False)
        monkeypatch.setattr(PF, "_ddns_host", lambda: None)
        fix = PF.preflight(verify_local=False)["firewall_fix"]
        assert fix and shlex.quote(binary) in fix, f"binary path not shlex-quoted in: {fix}"
        # Real safety check: split the FIRST command (up to &&) into shell tokens. The binary must be a
        # single token; 'rm' must never appear as its own token (which would mean injection succeeded).
        first_cmd = fix.split("&&")[0]
        tokens = shlex.split(first_cmd)
        assert "rm" not in tokens, f"injection: 'rm' became a separate shell token in: {first_cmd}"
        assert binary in tokens, f"binary path not preserved as a single token in: {tokens}"
