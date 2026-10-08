from __future__ import annotations

from starlette.testclient import TestClient

from mongoose.cockpit import device_launchpad_html, phone_cockpit_html
from mongoose.app import app
from mongoose.services.item_model import remember_search_results, describe_actions
from mongoose.db import init_db


def test_phone_outbox_is_device_scoped_and_does_not_duplicate_displayed_messages():
    html = device_launchpad_html()
    assert "OUTBOX_PREFIX='mongoose_device_outbox_v2:'" in html
    assert "function outboxKey()" in html
    assert "t?t.slice(-12):'anon'" in html
    assert "function clearAllOutboxes()" in html
    assert "clearAllOutboxes(); localStorage.removeItem('mongoose_device_token')" in html
    assert "queueText(txt,true)" in html
    assert "if(!item.displayed) addBubble('user',item.text)" in html


def test_phone_mic_help_does_not_offer_dead_clickable_phone_localhost_link():
    html = device_launchpad_html()
    assert 'href="https://127.0.0.1:${port}/ui#setup"' not in html
    assert '<code style="color:var(--gold)">https://127.0.0.1:${port}/ui#setup</code>' in html


def test_service_worker_does_not_cache_forbidden_cockpit_shell_for_phone():
    sw = TestClient(app, raise_server_exceptions=False).get("/sw.js").text
    assert "const SHELL_URLS = ['/device/launchpad'];" in sw
    # Stronger than "never cache /ui": non-device navigations are handed to the browser
    # NATIVELY, untouched by the SW at all (a bare `return`), so cert interstitials and error
    # recovery still work and an owner-cockpit request can never be served from a stale cache.
    assert "if (!isDevice) {\n      return;\n    }" in sw
    assert "caches.match('/device/launchpad')" in sw
    assert "['/ui', '/device/launchpad']" not in sw


def test_phone_item_actions_are_real_buttons_not_label_only_alerts():
    html = phone_cockpit_html()
    assert "function renderActionButtons" in html
    assert "function runItemAction" in html
    assert "function phoneDownload" in html
    assert "alert(labels||'No actions available.')" not in html
    assert "/email/message/" in html
    assert "/voicemail/" in html


def test_item_action_description_carries_native_id_for_ui_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    init_db()
    remember_search_results(
        "lease demand",
        [{
            "item_ref": "E42ABCD",
            "kind": "email",
            "title": "Lease demand",
            "where": "imap/inbox",
            "connector": "imap",
            "native_id": 123,
            "open": 123,
            "actions": [{"action": "open_email", "label": "Open email"}],
            "correlation_keys": ["term:lease"],
        }],
    )
    d = describe_actions("E42ABCD")
    assert d["ok"] is True
    assert str(d["native_id"]) == "123"
    assert d["open"] == 123
    assert d["snapshot"]["item_ref"] == "E42ABCD"
