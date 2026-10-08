"""Two gates born from the same incident: an edit ate the decorator + signature of
GET /device/messages, leaving its body as dead code, so the phone's message fetch shipped
broken while every test stayed green (dead code is syntactically valid) -- and the updater's
own pyflakes gate would then have refused to deploy the release.

1. Route inventory: every critical route must actually be REGISTERED on the app, so losing a
   decorator fails CI the moment it happens instead of shipping.
2. Lint hygiene: pyflakes must be clean across src and tests, matching the deploy gate, so a
   release can never again pass its own suite yet fail to deploy."""
from __future__ import annotations

import subprocess
import sys
import warnings

import pytest

warnings.filterwarnings("ignore")

CRITICAL_ROUTES = {
    ("GET", "/health"),
    ("GET", "/device/messages"),
    ("POST", "/device/messages/send"),
    ("POST", "/device/messages/read"),
    ("POST", "/device/chat"),
    ("GET", "/device/chat/history"),
    ("POST", "/device/voice"),
    ("POST", "/device/irc/send"),
    ("GET", "/device/irc/history"),
    ("GET", "/device/whoami"),
    ("POST", "/cockpit/chat"),
    ("GET", "/cockpit/chat/history"),
    ("POST", "/cockpit/voice"),
    ("GET", "/cockpit/session/claim"),
    ("POST", "/llm/chat"),
    ("POST", "/assist"),
    ("GET", "/notifications/recent"),
    ("GET", "/build/plugin/download/{name}"),
    ("POST", "/conversation/{conversation_id}/clear"),
}


def test_critical_routes_registered():
    import mongoose.app as A
    registered = set()
    for route in A.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path:
            for m in methods:
                registered.add((m, path))
    missing = CRITICAL_ROUTES - registered
    assert not missing, f"critical routes missing from the app (lost decorator?): {sorted(missing)}"


def test_pyflakes_clean_matching_the_deploy_gate():
    pytest.importorskip("pyflakes")
    r = subprocess.run([sys.executable, "-m", "pyflakes", "src", "tests"],
                       capture_output=True, text=True)
    assert r.returncode == 0, "pyflakes findings (the updater gate will refuse to deploy):\n" + r.stdout
