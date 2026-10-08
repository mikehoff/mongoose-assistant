"""Regression: the service worker must never show 'Mongoose is offline' on bootstrap or owner
surfaces. That card is only valid for the installed device PWA shell, which is the only thing
the SW caches. First-run pages also proactively release any stale controlling SW."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

import mongoose.app as A


def test_offline_card_is_device_shell_only():
    sw = TestClient(A.app).get("/sw.js").text
    nav = sw[sw.index("req.mode === 'navigate'"):sw.index("// Non-navigation")]
    # Non-device navigations must be handed to the browser NATIVELY (bare return, no
    # respondWith of any kind). A SW-mediated fetch can never surface Safari's certificate
    # interstitial, so after a local-cert change the user was stuck on a false offline card
    # with no way to re-trust the cert and no way to download the fixed SW.
    assert "if (!isDevice) {" in nav
    guard_body = nav.split("if (!isDevice) {", 1)[1].split("}", 1)[0]
    assert "return;" in guard_body
    assert "respondWith" not in guard_body
    assert "e.respondWith(fetch(req))" not in nav
    # the ONLY respondWith in the navigate branch is the device-shell fallback
    assert nav.count("e.respondWith(") == 1
    assert nav.index("if (!isDevice)") < nav.index("Mongoose is offline")
    assert "caches.match('/device/launchpad')" in nav
    # the device card tells a working-network user how to recover from a cert change
    assert "accept the certificate prompt" in nav


def test_bootstrap_pages_release_stale_sw_and_show_no_offline_card():
    c = TestClient(A.app)  # loopback: first-run surfaces reachable
    for path in ("/install", "/welcome"):
        html = c.get(path).text
        assert "unregister()" in html, f"{path} should release a stale service worker"
        assert "Mongoose is offline" not in html, f"{path} must never render the offline card"


def test_sw_registration_forces_immediate_update_check():
    """A phone whose stale SW serves the offline card never runs page scripts on the broken
    navigation, so recovery hinges on the browser's SW-update fetch. Forcing r.update() on
    every successful load makes that propagation immediate instead of eventual."""
    import pathlib
    t = (pathlib.Path(__file__).resolve().parents[1] / "src" / "mongoose" / "cockpit.py").read_text()
    assert t.count("navigator.serviceWorker.register('/sw.js').then(function(r){try{r.update();}") == 2
