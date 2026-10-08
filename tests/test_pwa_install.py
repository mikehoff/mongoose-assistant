"""The install-as-app journey: manifest + service worker + icons reachable WITHOUT auth
(Chrome fetches them credential-less), and the launchpad carries the beforeinstallprompt
capture with an iOS Add-to-Home-Screen fallback."""
from __future__ import annotations

import tempfile
import warnings

warnings.filterwarnings("ignore")


def _client(monkeypatch):
    td = tempfile.mkdtemp()
    monkeypatch.setenv("MONGOOSE_DB_PATH", td + "/p.sqlite3")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "t")
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app)


def test_install_prerequisites_reachable_without_device_token(monkeypatch):
    c = _client(monkeypatch)
    for path in ("/device/manifest.webmanifest", "/sw.js",
                 "/static/img/mongoose-192.png", "/static/img/mongoose-512.png"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_device_manifest_shape_for_installability(monkeypatch):
    c = _client(monkeypatch)
    m = c.get("/device/manifest.webmanifest").json()
    assert m["display"] == "standalone"
    assert m["start_url"].startswith("/device/")
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes


def test_launchpad_carries_the_install_prompt_wiring():
    from mongoose.cockpit import device_launchpad_html
    h = device_launchpad_html()
    assert "beforeinstallprompt" in h
    assert "Add to Home Screen" in h          # iOS fallback
    assert "display-mode: standalone" in h    # already-installed detection
    assert "mongoose-install-dismissed" in h  # dismissal is remembered
