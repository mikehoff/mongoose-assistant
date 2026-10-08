"""WhatsApp connection journey: the connect -> scan -> test -> monitor flow. The send path stays on
the approved-outbox (draft -> approve -> send); the journey adds no unapproved-send route.

Env is set via monkeypatch (not os.environ.update) so the per-test environment snapshot in conftest
restores it cleanly — otherwise WAHA_URL leaks into later tests and flips their expected state."""
import os
import tempfile


def _fresh(monkeypatch, **env):
    d = tempfile.mkdtemp()
    os.chdir(d)
    monkeypatch.setenv("MONGOOSE_DATA_DIR", d)
    monkeypatch.setenv("MONGOOSE_DB_PATH", os.path.join(d, "m.sqlite3"))
    for k in ("WAHA_URL", "WHATSAPP_WAHA_URL", "WHATSAPP_SELF_NUMBER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


class _FakeProv:
    def __init__(self, state, qr_ok=True):
        self._state = state
        self._qr_ok = qr_ok
    def status(self):
        return {"ok": True, "session": "default", "state": self._state}
    def start_session(self):
        return {"ok": True, "state": "starting"}
    def qr(self):
        return {"ok": True, "qr_base64": "QUFB", "mime": "image/png"} if self._qr_ok else {"ok": False, "error": "no qr"}
    def send_text(self, chat_id, text):
        return {"ok": True, "response": {}}


def test_not_configured_when_waha_unreachable(monkeypatch):
    _fresh(monkeypatch)
    from mongoose.services import whatsapp_journey as J

    # Deterministically model "WAHA not running": provider reachable check fails AND no explicit URL.
    class _Unreachable:
        def status(self):
            return {"ok": False, "error": "WAHA unreachable: connection refused", "url": "http://127.0.0.1:3000"}
    monkeypatch.setattr(J, "_provider", lambda: _Unreachable())
    monkeypatch.setattr(J, "_waha_configured", lambda: False)
    assert J.connection_state()["state"] == "not_configured"


def test_scan_qr_state_and_qr(monkeypatch):
    _fresh(monkeypatch, WAHA_URL="http://fake:3000")
    from mongoose.services import whatsapp_journey as J
    monkeypatch.setattr(J, "_provider", lambda: _FakeProv("SCAN_QR_CODE"))
    assert J.connection_state()["state"] == "scan_qr"
    assert J.login_qr()["qr_base64"] == "QUFB"


def test_connected_state(monkeypatch):
    _fresh(monkeypatch, WAHA_URL="http://fake:3000")
    from mongoose.services import whatsapp_journey as J
    monkeypatch.setattr(J, "_provider", lambda: _FakeProv("WORKING"))
    assert J.connection_state()["state"] == "connected"


def test_connect_starts_session(monkeypatch):
    _fresh(monkeypatch, WAHA_URL="http://fake:3000")
    from mongoose.services import whatsapp_journey as J
    monkeypatch.setattr(J, "_provider", lambda: _FakeProv("STARTING"))
    assert J.connect()["ok"] is True


def test_test_send_refused_when_not_connected(monkeypatch):
    _fresh(monkeypatch, WAHA_URL="http://fake:3000", WHATSAPP_SELF_NUMBER="447700900000")
    from mongoose.services import whatsapp_journey as J
    monkeypatch.setattr(J, "_provider", lambda: _FakeProv("SCAN_QR_CODE"))
    r = J.send_test()
    assert r["ok"] is False
    assert "isn't connected" in r["error"]


def test_test_send_uses_approved_outbox(monkeypatch):
    """The test send must go through draft->approve->send (the only send path), proving the journey
    doesn't add an unapproved-send shortcut."""
    _fresh(monkeypatch, WAHA_URL="http://fake:3000", WHATSAPP_SELF_NUMBER="447700900000")
    from mongoose.services import whatsapp_journey as J
    monkeypatch.setattr(J, "_provider", lambda: _FakeProv("WORKING"))
    import mongoose.services.whatsapp as W
    calls = {}

    def spy_approve(draft_id, send_fn=None):
        calls["draft_id"] = draft_id
        return {"ok": True, "sent": True}

    monkeypatch.setattr(W, "approve_and_send", spy_approve)
    r = J.send_test()
    assert r.get("ok") is True
    assert "draft_id" in calls  # went through the approved-outbox path


def test_test_send_requires_a_number(monkeypatch):
    _fresh(monkeypatch, WAHA_URL="http://fake:3000")  # no WHATSAPP_SELF_NUMBER
    from mongoose.services import whatsapp_journey as J
    monkeypatch.setattr(J, "_provider", lambda: _FakeProv("WORKING"))
    r = J.send_test()
    assert r["ok"] is False
    assert "number" in r["error"].lower()
