"""Regression tests for the terminal+browser install JOURNEY, not just the install logic.

Three real dead-end classes this pins, all found in live installs:
  1. A page that calls a JS helper it never defines (the install page called esc() in renderSteps/
     showDone but never defined it -> ReferenceError -> the tick() loop's silent catch swallows it ->
     frozen at 100% with no steps, no buttons, no handoff).
  2. A page that reads a field the backend legitimately omits (the welcome page read onboarding.steps,
     but /cockpit/onboarding returns a COARSE view with no steps over the LAN address -> undefined
     .findIndex -> silent crash).
  3. A handoff that lands on a gated/privacy-coarsened route (owner setup opened on the LAN IP instead
     of localhost, and first-run pages linked to /ui which is remote-gated -> 403 dead end).
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "Install Mongoose.command"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_macos.sh"
PROBE = ROOT / "scripts" / "mongoose_local_probe.sh"


# --- Class 1: the install page must DEFINE every JS helper it calls --------------------------------
def test_install_page_defines_esc_it_calls_in_render_and_done():
    from mongoose.cockpit import install_html
    h = install_html()
    assert h.count("esc(") >= 1, "install page calls esc() ..."
    assert "function esc" in h, (
        "install JS calls esc() in renderSteps()/showDone() but never defines it; the first call throws "
        "ReferenceError, the tick() loop's catch swallows it, and the page freezes at 100% with no handoff")


def test_install_page_has_no_undefined_helper_calls():
    """Catch the whole class: every custom function the install script calls must be defined in it."""
    import re
    from mongoose.cockpit import install_html
    h = install_html()
    start = h.find("const STEPICON=")
    script = h[start:h.find("</script>", start)]
    defined = set(re.findall(r"function\s+([A-Za-z_]\w*)", script))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_]\w*)\s*=", script))
    # the helpers the page relies on (the ones whose absence dead-ends the page)
    for fn in ("esc", "j", "loadPairingCode", "renderSteps", "showDone", "tick",
               "checkFresh", "pollPaired", "enablePhone", "localUiUrl", "localWelcomeUrl"):
        assert fn in defined, f"install page calls {fn}() but never defines it"


# --- Class 2: the welcome page must tolerate the coarse onboarding view (no steps) -----------------
def test_welcome_guards_missing_steps_from_coarse_lan_onboarding():
    from mongoose.cockpit import welcome_html
    w = welcome_html()
    assert "Array.isArray(s.steps)" in w, (
        "welcome render() does s.steps.findIndex/map unguarded; over the LAN address /cockpit/onboarding "
        "returns a coarse view with NO steps, so this crashes the page silently")


def test_onboarding_really_omits_steps_for_unauthenticated_lan(monkeypatch):
    """Prove the precondition the guard defends against: coarse onboarding has no steps over LAN."""
    from unittest.mock import MagicMock
    import mongoose.app as A
    monkeypatch.setattr(A, "get_settings", lambda: type("S", (), {"api_token": "tok"})())
    req = MagicMock()
    req.client.host = "192.168.1.50"
    req.headers = {}
    req.cookies = {}
    out = A.cockpit_onboarding(req)
    assert "steps" not in out, "coarse LAN onboarding should omit steps (that's what the guard handles)"


# --- Class 3: handoffs must land on localhost / a usable page, never a gated route -----------------
def test_install_opens_on_localhost_so_owner_pairing_gate_works():
    """The owner-pairing secret is loopback-only, so /install must open on localhost, not the LAN IP."""
    helper = PROBE.read_text()
    wrapper = WRAPPER.read_text()
    bootstrap = BOOTSTRAP.read_text()
    assert "mongoose_local_url()" in helper and "mongoose_open_local()" in helper, (
        "probe must define localhost-only URL helpers for owner pages")
    assert "localhost" in helper
    # the installer must open /install via the localhost opener, not the LAN-preferring one
    assert 'mongoose_open_local "/install"' in wrapper, "wrapper opens /install on the LAN IP, not localhost"
    assert 'mongoose_open_local "/install"' in bootstrap, "bootstrap opens /install on the LAN IP, not localhost"
    assert 'mongoose_open_path "/install"' not in wrapper, "wrapper still has a LAN-opening /install path"


def test_no_undefined_open_helper_in_scripts():
    """Guard against the trap of referencing an open-helper the probe never defines (would break the
    installer with 'command not found'). Every mongoose_open_* / mongoose_*_url used in the wrapper or
    bootstrap must be defined in the sourced probe."""
    import re
    helper = PROBE.read_text()
    defined = set(re.findall(r"^([A-Za-z_]\w*)\(\)", helper, re.M))
    for script in (WRAPPER, BOOTSTRAP):
        text = script.read_text()
        used = set(re.findall(r"\b(mongoose_(?:open_\w+|\w*url|\w*base))\b", text))
        for fn in used:
            assert fn in defined, f"{script.name} uses {fn}() but scripts/mongoose_local_probe.sh never defines it"


def test_install_done_card_has_visible_handoff_buttons():
    from mongoose.cockpit import install_html
    h = install_html()
    assert "Open the cockpit" in h and "Open chat" in h, "install handoff must show cockpit + chat buttons"
    # and they must target localhost (owner cockpit is loopback-only)
    assert "localWelcomeUrl()" in h and "localUiUrl(" in h


def test_ui_serves_friendly_html_not_raw_json_403_to_a_browser():
    """A human who clicks through to /ui over the LAN must get a recoverable HTML page with the localhost
    URL, not a raw {"error":"forbidden"} dead end. API callers (no text/html) still get JSON."""
    import inspect
    import mongoose.app as A
    src = inspect.getsource(A)
    assert 'path in ("/ui", "/cockpit")' in src and "text/html" in src, (
        "middleware doesn't give browsers an HTML 403 for the LAN-gated cockpit")
    assert "Open local cockpit" in src and "localhost" in src
