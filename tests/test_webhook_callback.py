"""Webhook: example-court.org reports back to Mongoose via DDNS address."""
import hashlib
import hmac
import json
import os
import tempfile


def _fresh(extra=None):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    for k in ("MONGOOSE_WEBHOOK_SECRET", "DDNS_PROVIDER", "DDNS_DUCKDNS_DOMAIN"):
        os.environ.pop(k, None)
    for k, v in (extra or {}).items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _client(extra=None):
    _fresh(extra)
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── ddns.public_url ──────────────────────────────────────────────────────────

def test_public_url_not_configured_returns_none():
    _fresh()
    from mongoose.ddns import public_url
    assert public_url({}) is None


def test_public_url_duckdns():
    from mongoose.ddns import public_url
    # HTTP-only server (no TLS cert/key) -> http:// , the scheme must match what the server actually
    # speaks, else a phone/webhook is told https:// for a server that only does http and the connection
    # aborts. (This is the pairing-failure fix; see test_pairing_preflight.py.)
    url = public_url({"DDNS_PROVIDER": "duckdns",
                      "DDNS_DUCKDNS_DOMAIN": "mymac",
                      "DDNS_DUCKDNS_TOKEN": "tok"})
    assert url == "http://mymac.duckdns.org:8765"


def test_public_url_duckdns_https_when_tls_configured(tmp_path):
    from mongoose.ddns import public_url
    cert = tmp_path / "c.pem"; cert.write_text("x")
    key = tmp_path / "k.pem"; key.write_text("x")
    url = public_url({"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "mymac",
                      "DDNS_DUCKDNS_TOKEN": "tok",
                      "MONGOOSE_TLS_CERTFILE": str(cert), "MONGOOSE_TLS_KEYFILE": str(key)})
    assert url == "https://mymac.duckdns.org:8765"


def test_public_url_duckdns_full_domain():
    from mongoose.ddns import public_url
    url = public_url({"DDNS_PROVIDER": "duckdns",
                      "DDNS_DUCKDNS_DOMAIN": "mymac.duckdns.org",
                      "DDNS_DUCKDNS_TOKEN": "tok"})
    assert url == "http://mymac.duckdns.org:8765"  # HTTP-only -> http:// (scheme-accurate)


def test_public_url_cloudflare():
    from mongoose.ddns import public_url
    url = public_url({"DDNS_PROVIDER": "cloudflare",
                      "DDNS_CLOUDFLARE_RECORD_NAME": "home.example-court.org",
                      "DDNS_CLOUDFLARE_TOKEN": "t", "DDNS_CLOUDFLARE_ZONE_ID": "z"})
    assert url is not None
    assert "example-court.org" in url


def test_public_url_tailscale_returns_none():
    """Tailscale is a private VPN — no internet-facing URL."""
    from mongoose.ddns import public_url
    url = public_url({"DDNS_PROVIDER": "tailscale"})
    assert url is None


# ── webhook service ──────────────────────────────────────────────────────────

def test_verify_signature_correct():
    from mongoose.services.webhook import verify_signature
    body = b'{"event":"rtm_query","message":"test"}'
    secret = "mysecret123"
    sig = _sign(body, secret)
    os.environ["MONGOOSE_WEBHOOK_SECRET"] = secret
    assert verify_signature(body, sig) is True


def test_verify_signature_wrong_secret():
    from mongoose.services.webhook import verify_signature
    body = b'{"event":"rtm_query"}'
    os.environ["MONGOOSE_WEBHOOK_SECRET"] = "correct_secret"
    wrong_sig = _sign(body, "wrong_secret")
    assert verify_signature(body, wrong_sig) is False


def test_verify_signature_tampered_body():
    from mongoose.services.webhook import verify_signature
    body = b'{"event":"rtm_query"}'
    secret = "mysecret"
    os.environ["MONGOOSE_WEBHOOK_SECRET"] = secret
    good_sig = _sign(body, secret)
    tampered = b'{"event":"admin_note"}'  # different body
    assert verify_signature(tampered, good_sig) is False


def test_generate_webhook_secret_length():
    from mongoose.services.webhook import generate_webhook_secret
    s = generate_webhook_secret()
    assert len(s) >= 40  # 32 bytes base64 = ~43 chars
    assert " " not in s   # URL-safe, no spaces


def test_public_webhook_url_no_ddns():
    _fresh()
    from mongoose.services.webhook import public_webhook_url
    assert public_webhook_url() is None


# ── /webhook/setup endpoint ──────────────────────────────────────────────────

def test_webhook_setup_public_no_ddns():
    c = _client()
    r = c.get("/webhook/setup")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["ddns_needed"] is True
    assert d["ingest_url"] is None


def test_webhook_setup_with_ddns_domain(monkeypatch):
    _fresh({"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "mymac",
            "DDNS_DUCKDNS_TOKEN": "tok"})
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/webhook/setup")
    d = r.json()
    assert d["ddns_needed"] is False
    assert d["ingest_url"] is not None
    assert "/webhook/ingest" in d["ingest_url"]
    assert "duckdns" in d["ingest_url"]


def test_webhook_generate_secret_requires_nonce():
    c = _client()
    assert c.post("/webhook/generate-secret", json={}).status_code == 403


# ── /webhook/ingest endpoint ─────────────────────────────────────────────────

def test_webhook_ingest_no_secret_configured():
    c = _client()
    r = c.post("/webhook/ingest", content=b'{"event":"rtm_query"}',
               headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "webhook_not_configured" in r.json()["error"]


def test_webhook_ingest_invalid_signature():
    c = _client({"MONGOOSE_WEBHOOK_SECRET": "topsecret"})
    body = json.dumps({"event": "rtm_query", "message": "test"}).encode()
    r = c.post("/webhook/ingest", content=body,
               headers={"Content-Type": "application/json",
                        "X-Mongoose-Signature": "sha256=invalid"})
    assert r.status_code == 401


def test_webhook_ingest_valid_signature():
    secret = "test_webhook_secret_abc"
    c = _client({"MONGOOSE_WEBHOOK_SECRET": secret})
    payload = {"event": "rtm_query", "name": "Test Leaseholder",
               "email": "test@example.com", "message": "Query about service charge."}
    body = json.dumps(payload).encode()
    sig = _sign(body, secret)
    r = c.post("/webhook/ingest", content=body,
               headers={"Content-Type": "application/json",
                        "X-Mongoose-Signature": sig})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["event"] == "rtm_query"


def test_webhook_ingest_publish_event():
    secret = "test_secret_456"
    c = _client({"MONGOOSE_WEBHOOK_SECRET": secret})
    payload = {"event": "page_published", "post_title": "Summer 2026 Service Charge Notice",
               "post_url": "https://example-court.org/service-charge-2026/",
               "source": "example-court.org"}
    body = json.dumps(payload).encode()
    r = c.post("/webhook/ingest", content=body,
               headers={"Content-Type": "application/json",
                        "X-Mongoose-Signature": _sign(body, secret)})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "published" in r.json()["title"].lower()


def test_webhook_ingest_invalid_json():
    secret = "test_secret"
    c = _client({"MONGOOSE_WEBHOOK_SECRET": secret})
    body = b"not valid json {"
    r = c.post("/webhook/ingest", content=body,
               headers={"Content-Type": "application/json",
                        "X-Mongoose-Signature": _sign(body, secret)})
    assert r.json()["ok"] is False
    assert "json" in r.json()["error"].lower()


# ── scaffold includes webhook fields ─────────────────────────────────────────

def test_scaffold_plugin_has_webhook_code():
    tmp = tempfile.mkdtemp()
    _fresh()
    os.environ["MONGOOSE_PLUGIN_DIR"] = os.path.join(tmp, "example-court-rtm")
    from mongoose.services.wp_plugin import scaffold_example_court_plugin
    r = scaffold_example_court_plugin("0.1.0")
    assert r["ok"] is True
    php = open(os.path.join(r["path"], "example-court-rtm.php")).read()
    assert "pc_rtm_send_to_mongoose" in php
    assert "hash_hmac" in php
    assert "webhook_url" in php
    assert "webhook_secret" in php
    assert "X-Mongoose-Signature" in php
    assert "rtm_contact" in php   # shortcode


def test_cockpit_has_webhook_card():
    c = _client()
    html = c.get("/ui").text
    assert "webhookBox" in html
    assert "generateWebhookSecret" in html
    assert "Webhook" in html
    assert "/webhook/setup" in html   # the setup card calls /webhook/setup to get the ingest URL dynamically
