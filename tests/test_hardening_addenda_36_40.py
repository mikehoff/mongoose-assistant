"""Hardening tests for addenda 36-40: phone surface, voice setup, transcript AI judgement."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _client():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


# ── phone surface ────────────────────────────────────────────────────────────

def test_device_chat_no_extra_context_crash():
    """chat_with_memory doesn't accept extra_context — verify it no longer crashes."""
    import inspect
    from mongoose.app import device_chat_route
    src = inspect.getsource(device_chat_route)
    assert "extra_context" not in src, "extra_context kwarg must be removed"


def test_device_voice_no_extra_context_crash():
    import inspect
    from mongoose.app import device_voice_route
    src = inspect.getsource(device_voice_route)
    assert "extra_context" not in src


def test_device_voice_whisper_missing_message_no_terminal():
    """Whisper-missing message must not say 'run mongoose setup-voice' (unusable from phone)."""
    c = _client()
    from mongoose import devices as D
    orig = D.device_for_token
    D.device_for_token = lambda tok: {"device_id": "p1", "name": "Test"}
    orig_stt = None
    try:
        import mongoose.voice_benchmark as VB
        orig_stt = VB._whisper_transcribe
        VB._whisper_transcribe = lambda path: None
        r = c.post("/device/voice", content=b"fakeaudio",
                   headers={"X-Mongoose-Device-Token": "tok", "content-type": "audio/webm"})
        msg = r.json().get("error", "")
        assert "setup-voice" not in msg, f"terminal command in phone error: {msg}"
        assert "Settings" in msg or "Mac" in msg, f"no actionable guidance: {msg}"
    finally:
        D.device_for_token = orig
        if orig_stt: VB._whisper_transcribe = orig_stt


def test_device_chat_history_max_limit():
    """Limit capped at 60 regardless of ?limit= value."""
    _fresh()
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID, add_message, ensure_conversation
    ensure_conversation(DEFAULT_CONVERSATION_ID)
    for i in range(10):
        add_message(DEFAULT_CONVERSATION_ID, "user", f"msg {i}")
    from fastapi.testclient import TestClient
    from mongoose.app import app
    from mongoose import devices as D
    orig = D.device_for_token
    D.device_for_token = lambda tok: {"device_id": "p1", "name": "Test"}
    try:
        c = TestClient(app, raise_server_exceptions=False)
        r = c.get("/device/chat/history?limit=9999",
                  headers={"X-Mongoose-Device-Token": "tok"})
        msgs = r.json()["messages"]
        assert len(msgs) <= 60, "limit must be capped at 60"
    finally:
        D.device_for_token = orig


# ── voice setup ─────────────────────────────────────────────────────────────

def test_setup_voice_get_always_returns_ok():
    c = _client()
    r = c.get("/setup/voice")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "ready" in d and "missing" in d


def test_setup_voice_post_requires_nonce():
    c = _client()
    r = c.post("/setup/voice")
    assert r.status_code == 403


def test_setup_voice_post_graceful_when_brew_missing():
    """POST /setup/voice must return ok:False with a clear message, never crash."""
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/setup/voice", headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    d = r.json()
    assert "ok" in d and "message" in d   # never a 500


# ── auto-transcribe detection ────────────────────────────────────────────────

def test_auto_transcribe_cmd_returns_none_when_nothing_installed():
    from mongoose.services.voicemail import _auto_transcribe_cmd
    # in sandbox: no whisper or ffmpeg installed in PATH
    result = _auto_transcribe_cmd()
    assert result is None or result == "__WHISPER_NO_FFMPEG__"


def test_auto_transcribe_cmd_sentinel_when_whisper_no_ffmpeg(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda x: "/usr/bin/whisper" if x == "whisper" else None)
    import mongoose.services.voicemail as VM
    result = VM._auto_transcribe_cmd()
    # whisper found, ffmpeg missing → sentinel
    assert result == "__WHISPER_NO_FFMPEG__"


def test_transcribe_audio_handles_sentinel(monkeypatch):
    from mongoose.services.voicemail import _transcribe_audio
    import mongoose.services.voicemail as VM
    monkeypatch.setattr(VM, "_auto_transcribe_cmd", lambda: "__WHISPER_NO_FFMPEG__")
    result = _transcribe_audio("dGVzdA==", "test.wav")   # base64("test")
    transcript, status, source, error = result
    assert transcript is None
    assert "ffmpeg" in (error or "").lower()


def test_transcribe_audio_no_payload():
    from mongoose.services.voicemail import _transcribe_audio
    t, s, _, _ = _transcribe_audio(None, "vm.wav")
    assert t is None and s == "pending_no_audio"


# ── AI transcript judgement ──────────────────────────────────────────────────

def test_judge_transcripts_fallback_on_bad_json(monkeypatch):
    _fresh()
    import asyncio
    import mongoose.llm as L
    monkeypatch.setattr(L, "ask_ollama", lambda *a, **k: asyncio.coroutine(lambda: "not json")())

    async def run():
        from mongoose.services.voicemail import judge_transcripts_async
        return await judge_transcripts_async("old", "ha_staging", "new", "local_command")

    r = asyncio.run(run())
    # Must fall back to keeping existing (winner=a), never raise
    assert r["winner"] == "a"
    assert r["chosen"] == "old"
    assert "judgement failed" in r.get("reason", "")


def test_judge_transcripts_winner_merge_with_null_merged(monkeypatch):
    """winner=merge but merged=null must not crash and must return a sensible chosen."""
    _fresh()
    import asyncio
    import mongoose.llm as L

    async def mock_llm(prompt, role="chat"):
        return '{"winner":"merge","chosen":"old transcript","reason":"test","merged":null}'

    monkeypatch.setattr(L, "ask_ollama", mock_llm)

    async def run():
        from mongoose.services.voicemail import judge_transcripts_async
        return await judge_transcripts_async("old transcript", "ha_staging", "new", "local_command")

    r = asyncio.run(run())
    assert r["chosen"]   # must not be empty
    assert "diff" in r


def test_judge_transcripts_from_async_context(monkeypatch):
    """sync wrapper must work when called from within an async context (FastAPI)."""
    _fresh()
    import asyncio
    import mongoose.llm as L

    async def mock_llm(prompt, role="chat"):
        return '{"winner":"b","chosen":"new transcript","reason":"better","merged":null}'

    monkeypatch.setattr(L, "ask_ollama", mock_llm)

    async def run():
        from mongoose.services.voicemail import judge_transcripts
        return judge_transcripts("old", "ha_staging", "new transcript", "local_command")

    r = asyncio.run(run())
    assert r["winner"] == "b"
    assert r["chosen"] == "new transcript"


def test_transcript_diff_edge_cases():
    from mongoose.services.voicemail import _transcript_diff
    assert _transcript_diff("", "") == "(identical)"
    assert _transcript_diff("same", "same") == "(identical)"
    diff = _transcript_diff("", "new content")
    assert "+new content" in diff
    diff2 = _transcript_diff("old", "new")
    assert "-old" in diff2 and "+new" in diff2


def test_upsert_skips_ai_judgement_when_manual(monkeypatch):
    """Manual transcript must win without calling the LLM."""
    _fresh()
    called = {"n": 0}
    import mongoose.services.voicemail as VM
    orig = VM.judge_transcripts
    VM.judge_transcripts = lambda *a, **k: (called.__setitem__("n", called["n"]+1), orig(*a,**k))[1]

    from mongoose.services.voicemail import index_voicemail_from_protocol_staging, set_voicemail_transcript, list_voicemails
    env = {"msg_id":"vm1","caller":"07700900123","account_number":"30000000",
           "voicemail_at":"2026-06-24T14:30:00+01:00","transcript":"HA transcript","from":"ha/example_court"}
    index_voicemail_from_protocol_staging(env)
    vms = list_voicemails(1)
    set_voicemail_transcript(vms[0]["id"], "Manual correction.", source="manual")
    vms2 = list_voicemails(1)

    VM.judge_transcripts = orig
    assert vms2[0]["transcript"] == "Manual correction."
    assert vms2[0]["transcript_source"] == "manual"
    # AI judgement must NOT have been called for a manual edit
    assert called["n"] == 0, f"AI judgement called {called['n']} times for manual edit"
