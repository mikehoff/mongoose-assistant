"""Tests for the Mongoose Cockpit: the /ui page and its backing endpoints. The
cockpit is a view layer, so these assert it serves, exposes the expected tabs, and
that the JSON endpoints it polls return the right shapes and only surface REAL
approvals (no invented autonomy)."""
from fastapi.testclient import TestClient

from mongoose.app import app

client = TestClient(app)


def test_ui_serves_cockpit_page():
    r = client.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    assert "Mongoose Cockpit" in body
    # all seven tabs present
    for tab in ["Overview", "Setup & Repair", "Activity", "Approvals", "Search",
                "Models", "Scheduler"]:
        assert tab in body


def test_ui_polls_real_endpoints_only():
    """The page should reference existing endpoints, not invented ones."""
    body = client.get("/ui").text
    for ep in ["/doctor", "/cockpit/models", "/cockpit/scheduler", "/cockpit/approvals",
               "/find", "/bus/list"]:
        assert ep in body


def test_cockpit_models_endpoint():
    r = client.get("/cockpit/models")
    assert r.status_code == 200
    body = r.json()
    assert "owner" in body and "worker" in body and "pair_resident" in body


def test_cockpit_scheduler_endpoint():
    r = client.get("/cockpit/scheduler")
    assert r.status_code == 200
    assert "jobs" in r.json()


def test_cockpit_approvals_only_real(monkeypatch):
    """Approvals must reflect genuine queued actions (WhatsApp drafts), never invented
    placeholders."""
    import mongoose.services.whatsapp as wa
    monkeypatch.setattr(wa, "list_drafts", lambda status="pending": [
        {"id": "d1", "contact": "Sam", "body": "checking in", "status": "pending"}])
    r = client.get("/cockpit/approvals")
    assert r.status_code == 200
    items = r.json()["approvals"]
    assert len(items) == 1
    a = items[0]
    assert a["kind"] == "whatsapp_draft"
    # the action URLs point at the REAL approve/cancel endpoints
    assert a["approve"] == "/whatsapp/outbox/d1/approve"
    assert a["cancel"] == "/whatsapp/outbox/d1/cancel"


def test_cockpit_approvals_empty_when_none(monkeypatch):
    import mongoose.services.whatsapp as wa
    monkeypatch.setattr(wa, "list_drafts", lambda status="pending": [])
    r = client.get("/cockpit/approvals")
    assert r.json()["approvals"] == []


def test_install_missing_plan_is_open():
    """Plan mode (preview) is read-only and needs no nonce."""
    r = client.get("/setup/install-missing?plan=1")
    assert r.status_code == 200
    body = r.json()
    assert "plan" in body and "python" in body


def test_install_missing_requires_nonce():
    """The mutating POST is rejected without the cockpit nonce (forgery guard)."""
    r = client.post("/setup/install-missing")
    assert r.status_code == 403


def test_install_missing_with_nonce_works(monkeypatch):
    """The mutating POST is accepted with the nonce and returns the right shape. We
    MOCK install_capability so this never performs a real pip install, a release-gate
    test must be deterministic and offline, not pull heavy optional packages."""
    import mongoose.services.capabilities as caps
    monkeypatch.setattr(caps, "install_capability",
                        lambda name: {"ok": True, "capability": name})
    # one pretend-missing capability so the loop runs without touching the network
    monkeypatch.setattr(caps, "detect_all",
                        lambda: [{"capability": "documents", "ready": False,
                                  "missing_pip": ["pypdfium2"]}])
    nonce = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = client.post("/setup/install-missing", headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200
    body = r.json()
    assert "installed" in body and "failed" in body
    assert body["installed"] == ["documents"]  # the mocked install was invoked


def test_cockpit_nonce_endpoint():
    r = client.get("/cockpit/nonce")
    assert r.status_code == 200 and r.json()["cockpit_nonce"]


def test_approve_requires_credential():
    """Draft approve/cancel reject a request with neither cockpit nonce nor token."""
    assert client.post("/whatsapp/outbox/1/approve").status_code == 403
    assert client.post("/whatsapp/outbox/1/cancel").status_code == 403


def test_approve_accepts_cockpit_nonce(monkeypatch):
    import mongoose.services.whatsapp as wa
    monkeypatch.setattr(wa, "approve_and_send", lambda did: {"id": did, "status": "sent"})
    nonce = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = client.post("/whatsapp/outbox/7/approve", headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200


def test_cockpit_escapes_untrusted_output():
    """The cockpit page must escape untrusted values (XSS guard): it ships an esc()
    helper and uses it on rendered content."""
    body = client.get("/ui").text
    assert "function esc(" in body
    # the high-risk renders (approvals body, bus text, search snippet) go through esc
    assert "esc(a.body" in body or "esc((a.body" in body
    assert "esc(text)" in body



def test_cockpit_visual_identity():
    """Lock in the deliberate visual direction so a future edit can't silently revert
    to the templated dark-dashboard default: the warm-charcoal instrument look, the
    Fraunces display face, the brass accent, and the breathing 'alive' signature, now
    carried by the pixel-art mongoose logo pulsing from transparent in the header."""
    body = client.get("/ui").text
    assert "Fraunces" in body            # characterful display face (not system-sans)
    assert "IBM+Plex+Mono" in body or "IBM Plex Mono" in body  # data/log mono face
    assert "--gold" in body              # the single brass accent
    # the breathing alive heartbeat: the logo pulses from transparent (the new signature)
    assert 'id="brandLogo"' in body
    assert "logo-pulse" in body
    assert "@keyframes logoBreath" in body
    assert "@keyframes breathe" in body  # the ripple keyframe still drives status dots elsewhere
    assert "prefers-reduced-motion" in body  # motion is opt-out (quality floor)
    # the old templated blue accent is gone
    assert "#58a6ff" not in body


def test_no_external_callouts_in_cockpit_html():
    """Local-first guard: the Cockpit and login pages must not AUTO-FETCH from any external host (no
    font CDN, no analytics, no remote scripts, no external src/@import). Every fetched asset is
    served by Mongoose itself. User-clickable <a href> links to known setup domains (e.g. where to
    create an API key) are allowed: they are inert until the user chooses to click, and are not a
    silent callout. This guards against silently reintroducing a third-party FETCH on the primary
    surface."""
    import re
    from mongoose.cockpit import cockpit_html, login_html
    for name, html in [("cockpit", cockpit_html()), ("login", login_html())]:
        # Find FETCHING references only: src=, href= on <link>, url(...) in CSS, @import. A plain
        # anchor href is navigation, not a fetch, so it's excluded from this check.
        fetching = []
        fetching += re.findall(r'src\s*=\s*["\'](https?://[^"\']+)', html)
        fetching += re.findall(r'<link[^>]+href\s*=\s*["\'](https?://[^"\']+)', html)
        fetching += re.findall(r'url\(\s*["\']?(https?://[^)"\']+)', html)
        fetching += re.findall(r'@import\s+["\'](https?://[^"\']+)', html)
        external = [u for u in fetching
                    if not u.startswith("http://www.w3.org")
                    and "apple.com/DTDs" not in u
                    and not u.startswith("http://127.0.0.1")
                    and not u.startswith("http://localhost")]
        assert external == [], f"{name} has external FETCHING callouts: {external}"
        # specifically: no Google Fonts (fetched), no analytics
        assert "fonts.googleapis" not in html and "gstatic" not in html
        assert "google-analytics" not in html and "googletagmanager" not in html
    # fonts are referenced locally
    assert "/static/fonts/" in cockpit_html()
    # And anchor links, if any, must open in a new tab with noopener (so they can't hijack the page)
    anchors = re.findall(r'<a\s+href="https?://[^"]+"[^>]*>', cockpit_html())
    for a in anchors:
        assert "target=\"_blank\"" in a and "noopener" in a, f"external anchor missing safe attrs: {a}"


def test_bundled_fonts_present_and_served():
    """The woff2 fonts must be bundled and served locally with the right mime type."""
    # the font the design depends on most (body text)
    r = client.get("/static/fonts/inter-latin-400-normal.woff2")
    assert r.status_code == 200
    assert r.headers.get("content-type") == "font/woff2"
    assert len(r.content) > 1000  # a real font file
    # traversal-safe
    assert client.get("/static/fonts/..%2F..%2F.env").status_code == 404
    assert client.get("/static/fonts/nonexistent.woff2").status_code == 404


def test_cockpit_device_revoke_does_not_embed_untrusted_name_in_onclick():
    """Device names are attacker-influenced (paired from a phone). They must never be
    interpolated into inline JS handlers, where HTML entity decoding can turn &#39; back
    into a quote and break out of the string literal."""
    body = client.get("/ui").text
    assert "revokeDevice('${esc(dev.device_id)}','${esc(dev.name)}')" not in body
    assert "data-device-id" in body
    assert "revokeDevice(this.dataset.deviceId)" in body


def test_cockpit_demo_error_path_escapes_exception_text():
    body = client.get("/ui").text
    assert "Could not run the demo: '+e+" not in body
    assert "Could not run the demo: '+esc(e)+" in body
