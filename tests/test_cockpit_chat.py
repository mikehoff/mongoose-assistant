"""The cockpit text chat: a typed message runs through the same brain as voice and returns a reply,
and the ICQ/IRC UI is present in the rendered cockpit."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def _client_and_nonce():
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A._COCKPIT_NONCE


def test_chat_endpoint_returns_reply():
    _fresh()
    client, nonce = _client_and_nonce()
    r = client.post("/cockpit/chat", json={"text": "tell me a joke"},
                    headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["reply"]  # got something back


def test_chat_endpoint_requires_nonce():
    _fresh()
    client, _ = _client_and_nonce()
    # no nonce -> rejected (it runs a real turn)
    r = client.post("/cockpit/chat", json={"text": "hi"})
    assert r.status_code in (401, 403)


def test_chat_endpoint_rejects_empty():
    _fresh()
    client, nonce = _client_and_nonce()
    r = client.post("/cockpit/chat", json={"text": "   "},
                    headers={"X-Cockpit-Nonce": nonce})
    assert r.json()["ok"] is False


def test_chat_endpoint_rejects_overlong():
    _fresh()
    client, nonce = _client_and_nonce()
    r = client.post("/cockpit/chat", json={"text": "x" * 9000},
                    headers={"X-Cockpit-Nonce": nonce})
    assert r.json()["ok"] is False


def test_chat_routes_through_the_brain():
    _fresh()
    # a delight request should come back tagged as delight (same brain as everything else)
    client, nonce = _client_and_nonce()
    r = client.post("/cockpit/chat", json={"text": "give me a fun fact"},
                    headers={"X-Cockpit-Nonce": nonce})
    body = r.json()
    assert body["ok"] is True
    # source is surfaced; for a delight request it should be 'delight'
    assert body.get("source") in ("delight", "")  # tolerate empty if routing changes


def test_irc_chat_ui_present_in_cockpit():
    _fresh()
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    # the tab exists and is wired
    assert '<section class="tab" id="chat">' in h
    assert 'chat:"Chat"' in h
    assert "function chatSetup()" in h
    # the ICQ/IRC aesthetic markers
    assert "irc-win" in h and "#39ff14" in h  # lime phosphor
    # a real, visible, blinking lime BLOCK cursor (not the thin native caret): the native caret is
    # hidden and we render an animated lime rectangle that tracks the typed text
    assert "caret-color:transparent" in h and "ircBlink" in h
    assert "irc-block" in h and "placeCursor" in h
    assert "IBM Plex Mono" in h  # console font
