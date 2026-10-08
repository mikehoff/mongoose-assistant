"""Phone surface: /device/chat, /device/voice, /device/chat/history, launchpad UI."""
import os
import tempfile


def _app_client():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


def test_device_chat_requires_auth():
    c = _app_client()
    r = c.post("/device/chat", json={"message": "hello"})
    assert r.status_code in (401, 403)


def test_device_voice_requires_auth():
    c = _app_client()
    r = c.post("/device/voice", content=b"fake", headers={"content-type": "audio/webm"})
    assert r.status_code in (401, 403)


def test_device_chat_history_requires_auth():
    c = _app_client()
    assert c.get("/device/chat/history").status_code in (401, 403)


def test_device_chat_empty_message_rejected(monkeypatch):
    c = _app_client()
    from mongoose import devices as D
    monkeypatch.setattr(D, "device_for_token", lambda tok: {"device_id": "phone1", "name": "Test"})
    r = c.post("/device/chat", json={"message": ""},
               headers={"X-Mongoose-Device-Token": "fake-tok"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_device_chat_history_returns_list(monkeypatch):
    c = _app_client()
    from mongoose import devices as D
    monkeypatch.setattr(D, "device_for_token", lambda tok: {"device_id": "phone1", "name": "Test"})
    r = c.get("/device/chat/history", headers={"X-Mongoose-Device-Token": "fake-tok"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert isinstance(d["messages"], list)


def test_device_chat_history_shares_conversation_with_mac():
    """History endpoint reads from DEFAULT_CONVERSATION_ID — the same brain the Mac cockpit uses."""
    c = _app_client()
    from mongoose import devices as D
    monkeypatch_holder = []

    class _mp:
        @staticmethod
        def setattr(obj, name, val, **kw):
            orig = getattr(obj, name, None)
            setattr(obj, name, val)
            monkeypatch_holder.append((obj, name, orig))

    mp = _mp()
    mp.setattr(D, "device_for_token", lambda tok: {"device_id": "phone1", "name": "Test"})
    # Seed the shared conversation directly (same path the Mac cockpit uses)
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID, add_message, ensure_conversation
    ensure_conversation(DEFAULT_CONVERSATION_ID)
    add_message(DEFAULT_CONVERSATION_ID, "user", "sentinel-ping-12345")
    hdrs = {"X-Mongoose-Device-Token": "fake-tok"}
    hist = c.get("/device/chat/history", headers=hdrs).json()
    contents = [m.get("content") or m.get("text") or "" for m in hist["messages"]]
    assert any("sentinel-ping-12345" in s for s in contents), f"seeded message not in history: {contents}"
    for obj, name, orig in monkeypatch_holder:
        if orig is not None:
            setattr(obj, name, orig)


def test_device_voice_no_audio_rejected(monkeypatch):
    c = _app_client()
    from mongoose import devices as D
    monkeypatch.setattr(D, "device_for_token", lambda tok: {"device_id": "phone1", "name": "Test"})
    r = c.post("/device/voice", content=b"",
               headers={"X-Mongoose-Device-Token": "fake-tok", "content-type": "audio/webm"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_device_voice_whisper_missing_is_friendly(monkeypatch):
    """When Whisper isn't installed the phone gets a clear 'run setup-voice' message."""
    c = _app_client()
    from mongoose import devices as D
    monkeypatch.setattr(D, "device_for_token", lambda tok: {"device_id": "phone1", "name": "Test"})
    import mongoose.app as APP
    monkeypatch.setattr(APP, "_whisper_transcribe", lambda path: None, raising=False)
    from mongoose import voice_benchmark as VB
    monkeypatch.setattr(VB, "_whisper_transcribe", lambda path: None)
    r = c.post("/device/voice", content=b"fakeaudio",
               headers={"X-Mongoose-Device-Token": "fake-tok", "content-type": "audio/webm"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False and d.get("whisper_missing") is True
    assert "setup-voice" not in d.get("error", ""), "must not show terminal command on phone"
    assert "Settings" in d.get("error", "") or "Mac" in d.get("error", "")


def test_launchpad_has_ptt_and_chat():
    c = _app_client()
    r = c.get("/device/launchpad")
    assert r.status_code == 200
    html = r.text
    assert 'id="ptt"' in html               # push-to-talk button
    assert 'id="msg"' in html               # text input
    assert '/device/voice' in html          # voice endpoint wired
    assert '/device/chat' in html           # chat endpoint wired
    assert '/device/chat/history' in html   # history endpoint wired
    assert 'Hold to speak' in html          # clear PTT affordance
    assert 'speechSynthesis' in html        # TTS for spoken replies
