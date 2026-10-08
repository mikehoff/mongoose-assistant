"""Tests for the voice/DB robustness fixes (this session's bug reports)."""
from mongoose.db import init_db


def test_db_self_heals_without_explicit_init(tmp_path, monkeypatch):
    # The crash you hit: a fresh install never ran `init-db`, so the first DB
    # access threw "no such table: conversations". connect() must self-heal.
    dbp = tmp_path / "data" / "mongoose.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(dbp))
    # deliberately do NOT call init_db
    import mongoose.db as db
    db._SCHEMA_READY.clear()
    from mongoose.services.memory import ensure_conversation
    cid = ensure_conversation("voice")  # must not raise
    assert cid
    with db.connect() as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversations" in tables


def test_stt_ready_requires_ffmpeg(tmp_path, monkeypatch):
    # whisper needs ffmpeg; readiness must require all three and name the missing one.
    import mongoose.voice as V
    monkeypatch.setattr(V.shutil, "which",
                        lambda c: "/usr/bin/" + c if c in ("rec", "whisper") else None)
    monkeypatch.setattr(V, "_whisper_cmd", lambda: "/usr/bin/whisper")
    ok, hint = V._stt_ready()
    assert ok is False
    assert "ffmpeg" in hint


def test_stt_ready_ok_with_all_three(monkeypatch):
    import mongoose.voice as V
    monkeypatch.setattr(V.shutil, "which",
                        lambda c: "/usr/bin/" + c if c in ("rec", "ffmpeg") else None)
    monkeypatch.setattr(V, "_whisper_cmd", lambda: "/usr/bin/whisper")
    ok, _ = V._stt_ready()
    assert ok is True


def test_session_tolerates_transient_capture_failure(tmp_path, monkeypatch):
    # A single None from listen_until_silence must NOT permanently drop to typed;
    # it should retry. Three in a row degrades.
    dbp = tmp_path / "data" / "mongoose.sqlite3"
    dbp.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(dbp))
    init_db(dbp)
    import mongoose.voice as V
    import mongoose.llm as llm

    async def fake_chat(prompt, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "ok", "remembered": []}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    monkeypatch.setattr(V, "_stt_ready", lambda: (True, ""))
    monkeypatch.setattr(V, "_tts_available", lambda: False)
    monkeypatch.setattr(V, "_ask_mode", lambda: "continuous")
    # one transient None (retry), then a real utterance, then exit
    seq = iter([None, "tell me a joke", "exit"])
    monkeypatch.setattr(V, "listen_until_silence", lambda **k: next(seq))
    # never falls into typed loop (no input() needed); ends via 'exit'
    assert V.run_session() == 0


def test_mic_threshold_configurable(monkeypatch):
    monkeypatch.setenv("MONGOOSE_MIC_THRESHOLD", "5%")
    monkeypatch.setenv("MONGOOSE_MIC_SILENCE_S", "2.0")
    from mongoose.settings import get_settings
    s = get_settings()
    assert s.mic_threshold == "5%" and s.mic_silence_s == 2.0


def test_setup_voice_targets_current_venv(monkeypatch, capsys):
    # The key safety property: pip is invoked via sys.executable, never a guessed
    # venv (the failure that bit a real user). Decline so nothing actually runs.
    import sys
    import mongoose.voice as V
    # whisper missing, rec+ffmpeg present so only the pip step is planned
    monkeypatch.setattr(V.shutil, "which",
                        lambda c: "/usr/bin/" + c if c in ("rec", "ffmpeg", "brew") else None)
    monkeypatch.setattr(V, "_whisper_cmd", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    rc = V.setup_voice()
    out = capsys.readouterr().out
    assert sys.executable in out  # pip targets the running interpreter
    assert "openai-whisper" in out
    assert rc == 1  # declined


def test_setup_voice_noop_when_complete(monkeypatch, capsys):
    import mongoose.voice as V
    monkeypatch.setattr(V.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.setattr(V, "_whisper_cmd", lambda: "/usr/bin/whisper")
    rc = V.setup_voice()
    assert rc == 0
    assert "already complete" in capsys.readouterr().out
