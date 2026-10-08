"""First-minutes / forgotten-user surface regression tests.

These lock in the routes that matter when a user has just installed Mongoose or comes
back later and only remembers "open Mongoose" rather than a specific path.
"""
from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient


def _client(client_host: str = "testclient"):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ.pop("MONGOOSE_API_TOKEN", None)
    os.environ.pop("MONGOOSE_ALLOW_REMOTE", None)
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False, client=(client_host, 50000))


def test_bare_root_is_a_forgiving_front_door_not_404():
    c = _client()
    r = c.get("/")
    assert r.status_code == 200
    assert "This is the front door" in r.text
    assert "Open the cockpit" in r.text
    assert "Continue guided setup" in r.text
    assert "Open phone surface" in r.text
    assert "what changed today" in r.text
    assert "log bug" in r.text


def test_root_is_public_but_privacy_safe_from_lan():
    c = _client(client_host="192.168.1.44")
    r = c.get("/")
    assert r.status_code == 200
    assert "local assistant" in r.text
    assert "MONGOOSE_API_TOKEN" not in r.text
    assert "api_token" not in r.text.lower()


def test_welcome_complete_state_is_a_reorientation_screen():
    from mongoose.cockpit import welcome_html
    h = welcome_html()
    assert "Mongoose is ready." in h
    assert "Ask / chat" in h
    assert "Search my stuff" in h
    assert "What changed?" in h
    assert "Connect more" in h
    assert "remind me to journal tonight" in h


def test_welcome_remote_coarse_state_has_no_fake_pairing_below():
    from mongoose.cockpit import welcome_html
    h = welcome_html()
    assert "remote page cannot mint owner codes" in h
    assert "Open phone surface" in h
    assert "Use the QR/code shown on the Mac" in h
    assert "code below" not in h.lower()


def test_install_done_handoff_is_honest_and_not_dead():
    from mongoose.cockpit import install_html
    h = install_html()
    assert "Continue guided setup" in h
    assert "Open full cockpit" in h
    assert "Open chat" in h
    assert "Retry pairing code" in h
    assert "try the cockpit" not in h
