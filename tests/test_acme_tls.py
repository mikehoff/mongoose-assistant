"""Let's Encrypt TLS for guest HTTPS: preflight honesty, DuckDNS TXT publishing, certbot hooks, and
the guest-pairing HTTPS gate. Network + certbot calls are mocked (issuance needs the public ACME API)."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("DDNS_PROVIDER", "DDNS_DUCKDNS_DOMAIN", "DDNS_DUCKDNS_TOKEN", "ACME_EMAIL",
              "DUCKDNS_DOMAIN", "DUCKDNS_TOKEN", "LETSENCRYPT_EMAIL"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()
    return d


def test_preflight_reports_all_missing():
    _fresh()
    from mongoose.acme_tls import preflight
    pf = preflight()
    assert pf["ok"] is False
    assert len(pf["problems"]) >= 3


def test_host_resolves_from_duckdns_config():
    _fresh(DDNS_PROVIDER="duckdns", DDNS_DUCKDNS_DOMAIN="myhome", DDNS_DUCKDNS_TOKEN="t", ACME_EMAIL="m@e.com")
    from mongoose.acme_tls import _duckdns_host, preflight
    assert _duckdns_host() == "myhome.duckdns.org"
    pf = preflight()
    assert pf["host"] == "myhome.duckdns.org"
    assert pf["have_token"] and pf["have_email"]


def test_host_none_for_other_provider():
    _fresh(DDNS_PROVIDER="cloudflare", DDNS_DUCKDNS_DOMAIN="x")
    from mongoose.acme_tls import _duckdns_host
    assert _duckdns_host() is None


def test_set_txt_calls_duckdns(monkeypatch):
    _fresh(DDNS_PROVIDER="duckdns", DDNS_DUCKDNS_DOMAIN="myhome", DDNS_DUCKDNS_TOKEN="tok")
    import mongoose.acme_tls as A
    calls = []

    class FakeResp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: calls.append(url) or FakeResp(b"OK"))
    assert A._set_duckdns_txt("valval") is True
    assert "txt=valval" in calls[0]
    assert "myhome" in calls[0]


def test_auth_hook_publishes(monkeypatch):
    _fresh(DDNS_PROVIDER="duckdns", DDNS_DUCKDNS_DOMAIN="myhome", DDNS_DUCKDNS_TOKEN="tok")
    import mongoose.acme_tls as A
    monkeypatch.setattr(A, "_set_duckdns_txt", lambda v: True)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    os.environ["CERTBOT_VALIDATION"] = "challenge123"
    assert A._auth_hook() == 0
    os.environ.pop("CERTBOT_VALIDATION", None)


def test_auth_hook_fails_without_validation():
    _fresh(DDNS_PROVIDER="duckdns", DDNS_DUCKDNS_DOMAIN="myhome", DDNS_DUCKDNS_TOKEN="tok")
    os.environ.pop("CERTBOT_VALIDATION", None)
    import mongoose.acme_tls as A
    assert A._auth_hook() == 1


def test_obtain_refuses_without_prereqs():
    _fresh()
    from mongoose.acme_tls import obtain_certificate
    r = obtain_certificate()
    assert r["ok"] is False
    assert "preflight" in r and r["preflight"]["problems"]


def test_guest_pairing_blocks_without_remote_access():
    """A trusted_guest code with no DDNS must carry a blocker so the cockpit can warn before
    handing out a link that won't work remotely."""
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/cockpit/devices/pair-code", headers={"X-Cockpit-Nonce": _COCKPIT_NONCE},
               json={"profile": "trusted_guest"})
    assert r.status_code == 200
    assert r.json().get("guest_blocker") == "no_remote_access"


def test_tls_status_endpoint():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/cockpit/tls/status", headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    assert "certificate" in r.json() and "preflight" in r.json()
