"""Static + route-level accessibility regression tests, from the adversarial accessibility audit.
These don't run a real screen reader, but they catch the structural regressions the audit found:
zoom locks, invisible-contrast tokens, mouse-only controls, unlabelled inputs, and missing
live regions. Plus one route test that the pairing diagnostic is reachable from the device."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from mongoose.cockpit import cockpit_html


def _all_surfaces() -> str:
    """Concatenate every HTML surface so we scan the whole UI, not just the cockpit."""
    import mongoose.cockpit as CK
    parts = [cockpit_html()]
    for name in ["PAIR_HTML", "DEVICE_LAUNCHPAD_HTML", "WELCOME_HTML", "INSTALL_HTML", "LOGIN_HTML"]:
        v = getattr(CK, name, "")
        if v:
            parts.append(v)
    return "\n".join(parts)


# ── Zoom must never be locked ────────────────────────────────────────────────────────────────

def test_no_zoom_lock_anywhere():
    """No surface may disable pinch-zoom (user-scalable=no / maximum-scale=1)."""
    html = _all_surfaces()
    assert "user-scalable=no" not in html, "a surface disables zoom (user-scalable=no)"
    assert "maximum-scale=1" not in html, "a surface caps zoom (maximum-scale=1)"


# ── Transcript text must not be invisible in dark theme ──────────────────────────────────────

def test_email_body_not_invisible_in_dark_theme():
    """.email-body must not use color:var(--ink) (near-black on a near-black bg = invisible)."""
    html = cockpit_html()
    # find the .email-body rule
    m = re.search(r"\.email-body\{[^}]*\}", html)
    assert m, ".email-body rule not found"
    rule = m.group(0)
    assert "color:var(--ink)" not in rule, ".email-body uses invisible --ink in dark theme"
    assert "color:var(--text)" in rule, ".email-body should use --text for readable contrast"


# ── Chat streams must be announced to screen readers ─────────────────────────────────────────

def test_chat_streams_are_live_regions():
    """Desktop and phone chat surfaces must be role=log with aria-live so replies are announced."""
    html = _all_surfaces()
    assert 'id="ircScreen" role="log"' in html, "desktop chat stream missing role=log"
    assert 'id="thread" role="log"' in html, "phone chat thread missing role=log"


# ── Login error and pairing message must be announced ────────────────────────────────────────

def test_login_and_pairing_feedback_are_announced():
    html = _all_surfaces()
    assert 'id="err" role="alert"' in html, "login error area missing role=alert"
    assert 'id="msg" role="status"' in html, "pairing message area missing role=status"


# ── Install/welcome progress must be a real progressbar ──────────────────────────────────────

def test_progress_bars_have_progressbar_role():
    html = _all_surfaces()
    # at least the install and welcome bars carry the role
    assert html.count('role="progressbar"') >= 2, "install/welcome progress bars missing role=progressbar"


# ── Key inputs must be labelled (aria-label or associated <label>) ───────────────────────────

def test_key_inputs_are_labelled():
    """Search, login, and chat inputs must carry an aria-label (placeholders are not labels)."""
    html = _all_surfaces()
    # The cockpit search, login token/username/password, and chat fields all carry aria-labels
    required = [
        'aria-label="Search files, email, voicemails and notifications"',
        'aria-label="Access token"',
        'aria-label="Username"',
        'aria-label="Password"',
        'aria-label="Type a message to Mongoose"',
    ]
    missing = [r for r in required if r not in html]
    assert not missing, f"inputs missing aria-label: {missing}"


# ── Setup wizard controls must be keyboard-operable ──────────────────────────────────────────

def test_wizard_controls_are_keyboard_operable():
    """Stage headers and tiles use clickable elements; they must carry role=button + keydown."""
    html = cockpit_html()
    # the stage-head onclick must be paired with a keydown handler and role=button
    assert 'class="stage-head"' in html
    # every stage-head occurrence should have an onkeydown nearby (we check the template once)
    assert "onkeydown=\"if(event.key==='Enter'||event.key===' ')" in html, \
        "wizard stage-head/tile missing keyboard handler"
    # wiz-close must be a real button
    assert '<button type="button" class="wiz-close"' in html, "wiz-close should be a real button"


# ── Reduced motion must be respected on every surface ────────────────────────────────────────

def test_reduced_motion_on_all_surfaces():
    """Every standalone HTML surface must carry a prefers-reduced-motion block."""
    import mongoose.cockpit as CK
    for name in ["PAIR_HTML", "DEVICE_LAUNCHPAD_HTML", "WELCOME_HTML", "INSTALL_HTML"]:
        v = getattr(CK, name, "")
        if v:
            assert "prefers-reduced-motion" in v, f"{name} missing reduced-motion handling"


# ── Pairing device type must not be hardcoded ────────────────────────────────────────────────

def test_pairing_device_type_not_hardcoded_android():
    html = _all_surfaces()
    assert 'device_type:"android"' not in html, "pairing hardcodes android device type"


# ── No button nested inside a link (invalid interactive markup) ──────────────────────────────

def test_no_button_inside_link():
    html = _all_surfaces()
    assert '<a href="/device/launchpad"><button>' not in html, "invalid button-in-link markup"


# ── Route: pairing preflight reachable from the device being paired ──────────────────────────

def test_pairing_preflight_reachable_from_lan(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    import mongoose.app as A
    c = TestClient(A.app, headers={"host": "192.168.1.50:8765"})
    r = c.get("/pairing/preflight", headers={"x-forwarded-for": "192.168.1.50"})
    assert r.status_code == 200, f"pairing preflight blocked from LAN ({r.status_code})"


# ── Network resilience: the phone must not freeze on a glitch ────────────────────────────────

def test_launchpad_api_has_timeout():
    """The phone launchpad's api() must use AbortController with a timeout so a network glitch
    can't hang the UI forever. This is the #1 'it freezes' complaint."""
    import mongoose.cockpit as CK
    html = getattr(CK, "DEVICE_LAUNCHPAD_HTML", "")
    assert "AbortController" in html, "launchpad api() must use AbortController"
    assert "timeoutMs" in html, "launchpad api() must have a timeout"
    # auto-reconnect banner + probe
    assert "connProbe" in html, "launchpad must auto-probe the connection to self-heal"
    assert "Reconnecting to Mongoose" in html, "launchpad must show a reconnect banner when offline"


def test_device_manifest_points_at_launchpad(monkeypatch, tmp_path):
    """A phone installing from the launchpad must get a manifest whose start_url is the launchpad,
    not the desktop cockpit."""
    import json
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.get("/device/manifest.webmanifest")
    assert r.status_code == 200
    m = json.loads(r.content)
    assert m["start_url"] == "/device/launchpad", "device manifest must open the launchpad"


def test_service_worker_caches_both_shells(monkeypatch, tmp_path):
    """The service worker must cache the device launchpad shell (not just the cockpit) and use
    a timeout so a half-open socket doesn't hang navigations."""
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    sw = c.get("/sw.js").text
    assert "/device/launchpad" in sw, "SW must cache the launchpad shell"
    assert "fetchWithTimeout" in sw, "SW must time out fetches so the page doesn't hang"
