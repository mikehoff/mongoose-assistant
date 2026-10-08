import pytest
from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def test_speak_degrades_to_print_without_tts(db, monkeypatch, capsys):
    import mongoose.voice as V
    monkeypatch.setattr(V.shutil, "which", lambda c: None)  # no `say`
    V.speak("hello world")
    assert "hello world" in capsys.readouterr().out


def test_listen_returns_none_without_stt(db, monkeypatch):
    import mongoose.voice as V
    monkeypatch.setattr(V.shutil, "which", lambda c: None)
    assert V.listen() is None


def test_one_turn_chat(db, monkeypatch):
    import asyncio
    import mongoose.voice as V
    import mongoose.llm as llm

    async def fake_chat(prompt, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "hi there", "remembered": []}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    reply = asyncio.run(V._one_turn("hello", "voice", "default"))
    assert reply == "hi there"


def test_one_turn_home_control(db, monkeypatch):
    import asyncio
    import mongoose.voice as V
    import mongoose.tools as tools

    async def fake_tool(prompt):
        return {"reply": "Turning on the lamp.", "executed": True, "tool": "control_home"}
    monkeypatch.setattr(tools, "run_tool_turn", fake_tool)
    reply = asyncio.run(V._one_turn("turn on the lamp", "voice", "default"))
    assert "lamp" in reply.lower()


def test_run_voice_once(db, monkeypatch, capsys):
    import mongoose.voice as V
    import mongoose.llm as llm

    async def fake_chat(prompt, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "spoken reply", "remembered": []}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    monkeypatch.setattr(V.shutil, "which", lambda c: None)
    rc = V.run_voice(once="hello", conversation_id="t")
    assert rc == 0
    assert "spoken reply" in capsys.readouterr().out


def test_session_degrades_to_typed_without_stt(db, monkeypatch, capsys):
    # When STT is unavailable, the session must fall back to typed+spoken input,
    # not hard-fail. Feed one line then 'exit'.
    import mongoose.voice as V
    import mongoose.llm as llm

    async def fake_chat(prompt, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "ok", "remembered": []}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    monkeypatch.setattr(V, "_stt_ready", lambda: (False, "need sox"))
    monkeypatch.setattr(V, "_tts_available", lambda: False)
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    rc = V.run_session()
    assert rc == 0
    out = capsys.readouterr().out
    assert "need sox" in out and "type your input" in out


def test_session_smooth_single_turn(db, monkeypatch, capsys):
    import mongoose.voice as V
    import mongoose.llm as llm

    async def fake_chat(prompt, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "weather is fine", "remembered": []}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    monkeypatch.setattr(V, "_stt_ready", lambda: (True, ""))
    monkeypatch.setattr(V, "_tts_available", lambda: False)
    monkeypatch.setattr(V, "_ask_mode", lambda: "smooth")
    monkeypatch.setattr(V, "listen_until_silence", lambda **k: "what's the weather")
    rc = V.run_session()
    assert rc == 0
    assert "weather is fine" in capsys.readouterr().out


def test_session_continuous_then_exit(db, monkeypatch):
    import mongoose.voice as V
    import mongoose.llm as llm

    async def fake_chat(prompt, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "ok", "remembered": []}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    monkeypatch.setattr(V, "_stt_ready", lambda: (True, ""))
    monkeypatch.setattr(V, "_tts_available", lambda: False)
    monkeypatch.setattr(V, "_ask_mode", lambda: "continuous")
    seq = iter(["tell me a joke", "exit"])
    monkeypatch.setattr(V, "listen_until_silence", lambda **k: next(seq))
    assert V.run_session() == 0


def test_session_wake_word_ignores_non_trigger(db, monkeypatch):
    import mongoose.voice as V
    import mongoose.llm as llm

    async def fake_chat(prompt, conversation_id=None, role="chat", mode="default", system=None):
        return {"reply": "ok", "remembered": []}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    monkeypatch.setattr(V, "_stt_ready", lambda: (True, ""))
    monkeypatch.setattr(V, "_tts_available", lambda: False)
    monkeypatch.setattr(V, "_ask_mode", lambda: "wake")
    # wake mode listens twice per cycle: once for trigger, once for the question.
    # cycle 1: trigger="just noise" (ignored, no question listen)
    # cycle 2: trigger="mongoose" -> question="tell me a joke"
    # cycle 3: trigger="mongoose" -> question="exit"
    seq = iter(["just noise", "mongoose", "tell me a joke", "mongoose", "exit"])
    monkeypatch.setattr(V, "listen_until_silence", lambda **k: next(seq))
    assert V.run_session() == 0


def test_wake_word_same_utterance_command(db, monkeypatch):
    # "mongoose turn off the light" should execute the command in one utterance,
    # not discard it and re-listen.
    import mongoose.voice as V
    import mongoose.tools as tools

    calls = []
    async def fake_tool(prompt):
        calls.append(prompt)
        return {"reply": "Light off.", "executed": True, "tool": "control_home"}
    monkeypatch.setattr(tools, "run_tool_turn", fake_tool)
    monkeypatch.setattr(V, "_stt_ready", lambda: (True, ""))
    monkeypatch.setattr(V, "_tts_available", lambda: False)
    monkeypatch.setattr(V, "_ask_mode", lambda: "wake")
    # one combined utterance with wake+command, then a wake+exit
    seq = iter(["mongoose turn off the light", "mongoose exit"])
    monkeypatch.setattr(V, "listen_until_silence", lambda **k: next(seq))
    assert V.run_session() == 0
    # the command portion (wake word stripped) was executed
    assert any("turn off the light" in c for c in calls)
