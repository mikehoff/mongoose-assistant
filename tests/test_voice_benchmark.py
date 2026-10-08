"""Tests for the voice benchmark. No real audio in the sandbox, so httpx and Whisper
are mocked. The point is the SKIP-not-fail discipline: each path degrades cleanly when
its prerequisite (Ollama audio support / Whisper) is missing, and a head-to-head
verdict is produced only when BOTH paths actually run."""
import asyncio

import mongoose.voice_benchmark as VB


def _clip(tmp_path):
    p = tmp_path / "clip.wav"
    p.write_bytes(b"RIFFfake-wav-bytes")
    return p


def test_probe_skips_when_audio_rejected(monkeypatch, tmp_path):
    """If Ollama returns an error for every audio field, probe reports ok=False with
    the error so the caller SKIPs (not fails)."""
    class FakeResp:
        status_code = 400
        text = "unknown field: audio"
        def json(self): return {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None): return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    r = asyncio.run(VB.probe_audio_support("gemma4:e2b", _clip(tmp_path)))
    assert r["ok"] is False and r["field"] is None and "audio" in r["error"]


def test_probe_accepts_when_field_works(monkeypatch, tmp_path):
    class FakeResp:
        status_code = 200
        text = ""
        def json(self): return {"message": {"content": "Tell me a joke about a pelican"}}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None): return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    r = asyncio.run(VB.probe_audio_support("gemma4:e2b", _clip(tmp_path)))
    assert r["ok"] is True and r["field"] in VB._AUDIO_FIELD_CANDIDATES


def test_voice_benchmark_skips_audio_when_unsupported(monkeypatch, tmp_path):
    """audio-native SKIPs (with a reason) when Ollama rejects audio; pipeline runs."""
    async def no_audio(model, clip): return {"ok": False, "field": None, "error": "no audio field accepted"}
    monkeypatch.setattr(VB, "probe_audio_support", no_audio)
    # pipeline works (Whisper + resident mocked)
    monkeypatch.setattr(VB, "_whisper_transcribe", lambda clip: (0.5, "turn off the lounge lights"))

    async def fake_ask(prompt, role="chat", model=None): return "Turning off the lounge lights."
    import mongoose.llm as llm
    monkeypatch.setattr(llm, "ask_ollama", fake_ask)

    r = asyncio.run(VB.run_voice_benchmark(str(_clip(tmp_path)), resident_model="gemma4:26b"))
    assert r["audio_native"]["skipped"] is True
    assert "audio" in r["audio_native"]["reason"]
    assert r["pipeline"]["skipped"] is False
    # no head-to-head when one side skipped
    assert r["verdict"]["faster"] is None


def test_voice_benchmark_skips_pipeline_without_whisper(monkeypatch, tmp_path):
    async def yes_audio(model, clip): return {"ok": True, "field": "audio", "sample": "hi"}
    monkeypatch.setattr(VB, "probe_audio_support", yes_audio)

    async def fake_native(model, clip, field): return 1.2, "Turning off the lounge lights."
    monkeypatch.setattr(VB, "_time_audio_native", fake_native)
    # Whisper absent
    monkeypatch.setattr(VB, "_whisper_transcribe", lambda clip: None)

    r = asyncio.run(VB.run_voice_benchmark(str(_clip(tmp_path)), resident_model="gemma4:26b"))
    assert r["audio_native"]["skipped"] is False
    assert r["pipeline"]["skipped"] is True
    assert "Whisper" in r["pipeline"]["reason"]
    assert r["verdict"]["faster"] is None


def test_voice_benchmark_verdict_when_both_run(monkeypatch, tmp_path):
    """When BOTH paths run, a verdict names the faster one and the delta."""
    async def yes_audio(model, clip): return {"ok": True, "field": "audio", "sample": "hi"}
    monkeypatch.setattr(VB, "probe_audio_support", yes_audio)

    async def fake_native(model, clip, field): return 1.2, "answer A"
    monkeypatch.setattr(VB, "_time_audio_native", fake_native)
    monkeypatch.setattr(VB, "_whisper_transcribe", lambda clip: (0.8, "turn off lounge lights"))

    async def fake_ask(prompt, role="chat", model=None): return "answer B"
    import mongoose.llm as llm
    monkeypatch.setattr(llm, "ask_ollama", fake_ask)

    r = asyncio.run(VB.run_voice_benchmark(str(_clip(tmp_path)), resident_model="gemma4:26b"))
    assert r["audio_native"]["latency_s"] == 1.2
    assert not r["pipeline"]["skipped"]
    # audio-native (1.2s) vs pipeline (0.8 stt + llm time). verdict names a winner.
    assert r["verdict"]["faster"] in ("audio_native", "pipeline")
    assert "delta_s" in r["verdict"]
    # rendering doesn't crash and mentions both paths
    out = VB.format_voice_result(r)
    assert "audio-native" in out and "pipeline" in out


def test_missing_clip_errors_cleanly(tmp_path):
    r = asyncio.run(VB.run_voice_benchmark(str(tmp_path / "nope.wav")))
    assert r["ok"] is False and "not found" in r["error"]
