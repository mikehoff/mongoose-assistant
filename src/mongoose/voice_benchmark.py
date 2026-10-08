from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

# Voice benchmark.
#
# Answers a concrete question with a number from THIS machine: for a spoken request,
# is it better to feed the audio straight to an audio-native model (Gemma 4 E2B/E4B,
# which has a built-in speech encoder) in ONE call, or to keep the current pipeline
# (Whisper transcribes -> resident text model answers) in TWO steps?
#
# Two paths, same spoken input, measured end to end:
#   A) audio-native: POST the audio clip (base64) to gemma4:e2b in a single chat call.
#   B) pipeline:     Whisper -> text -> resident model.
#
# Honesty first: audio-into-Ollama is new and build-dependent. Before measuring, we
# PROBE whether the running Ollama actually accepts the audio field for the model.
# If it doesn't, the whole comparison SKIPs with the real error, rather than failing
# in a confusing way. (Same SKIP-not-fail discipline as the task benchmark.)


# Ollama's chat API takes per-message binary attachments under "images" historically;
# Gemma 4 audio is documented as sent "the same way as images" (a base64 clip). We
# try the documented audio field names in order and report which (if any) the server
# accepts, so we don't hard-code a guess that a future build changes.
_AUDIO_FIELD_CANDIDATES = ("audio", "audios", "images")


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


async def probe_audio_support(model: str, clip: Path) -> dict[str, Any]:
    """Check whether the running Ollama accepts an audio clip for `model`. Returns
    {ok, field, error}. ok=False means audio-in isn't available here, so the caller
    should SKIP the audio-native path with this reason, not score it as a failure."""
    import httpx
    from .settings import get_settings
    base = get_settings().ollama_base_url.rstrip("/")
    payload_audio = _b64(clip)
    last_err = ""
    for field in _AUDIO_FIELD_CANDIDATES:
        msg = {"role": "user",
               "content": "Transcribe and answer the spoken request.",
               field: [payload_audio]}
        payload = {"model": model, "messages": [msg], "stream": False,
                   "options": {"num_predict": 64}}
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{base}/api/chat", json=payload)
            if r.status_code == 200:
                data = r.json()
                # a 200 with a non-empty message means the field was accepted
                content = (data.get("message") or {}).get("content", "")
                if content:
                    return {"ok": True, "field": field, "sample": content[:200]}
                last_err = "200 but empty response"
            else:
                last_err = f"{r.status_code}: {r.text[:200]}"
        except Exception as exc:
            last_err = str(exc)
    return {"ok": False, "field": None, "error": last_err or "no audio field accepted"}


async def _time_audio_native(model: str, clip: Path, field: str) -> tuple[float, str]:
    """Path A: one call, audio straight to the model."""
    import httpx
    from .settings import get_settings
    base = get_settings().ollama_base_url.rstrip("/")
    msg = {"role": "user", "content": "Answer the spoken request concisely.",
           field: [_b64(clip)]}
    payload = {"model": model, "messages": [msg], "stream": False,
               "options": {"num_predict": 256},
               "keep_alive": get_settings().keep_alive_value}
    t = time.time()
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{base}/api/chat", json=payload)
        r.raise_for_status()
        out = (r.json().get("message") or {}).get("content", "")
    return time.time() - t, out


def _web_stt_model() -> str:
    """The STT model for browser/phone voice. base.en is too weak for compressed phone-mic audio, so it
    is only the last resort. Override with MONGOOSE_WEB_STT_MODEL (e.g. small.en / medium.en) then
    MONGOOSE_STT_MODEL."""
    import os
    return (os.environ.get("MONGOOSE_WEB_STT_MODEL", "").strip()
            or os.environ.get("MONGOOSE_STT_MODEL", "").strip()
            or "base.en")


def _preprocess_for_stt(src: Path, workdir: Path) -> Path:
    """Decode browser/phone audio to clean mono 16kHz PCM WAV for Whisper: high-pass rumble, low-pass
    non-speech, mild denoise, trim leading/trailing silence, dynamic-normalise level. Returns the
    cleaned WAV path, or the original clip if ffmpeg/filtering is unavailable (never makes things
    worse). This is the fix for raw-compressed-blob-into-Whisper producing garbage transcripts."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return src
    out = workdir / "clean.wav"
    # Filter chain, each piece speech-oriented. If a filter is missing in this ffmpeg build the whole
    # run fails and we fall back to the raw clip.
    af = ("highpass=f=80,lowpass=f=7500,"
          "afftdn=nf=-25,"
          "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-45dB:"
          "stop_periods=1:stop_silence=0.3:stop_threshold=-45dB,"
          "dynaudnorm=f=200:g=5")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
                        "-af", af, str(out)],
                       capture_output=True, timeout=60, check=True)
        if out.exists() and out.stat().st_size > 44:  # bigger than a bare WAV header
            return out
    except Exception:
        pass
    # Fallback: at least normalise to mono 16k without the fancy filters.
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(out)],
                       capture_output=True, timeout=60, check=True)
        if out.exists() and out.stat().st_size > 44:
            return out
    except Exception:
        pass
    return src


def _whisper_transcribe(clip: Path) -> tuple[float, str] | None:
    """Whisper STT for the voice product. Preprocesses the (usually browser-compressed) audio into clean
    mono 16kHz before transcription and uses a speech-appropriate model + flags. Returns (seconds,
    transcript) or None if Whisper isn't available."""
    from .voice import _whisper_cmd
    import shutil
    import subprocess
    import tempfile
    whisper = _whisper_cmd()
    if not whisper or not shutil.which("ffmpeg"):
        return None
    model = _web_stt_model()
    t = time.time()
    with tempfile.TemporaryDirectory() as d:
        dpath = Path(d)
        try:
            audio = _preprocess_for_stt(clip, dpath)
            subprocess.run([whisper, str(audio), "--model", model,
                            "--language", "en", "--task", "transcribe",
                            "--temperature", "0", "--condition_on_previous_text", "False",
                            "--fp16", "False",
                            "--output_format", "txt", "--output_dir", d],
                           capture_output=True, timeout=180, check=True)
            txt = next(dpath.glob("*.txt"), None)
            transcript = txt.read_text(encoding="utf-8").strip() if txt else ""
        except Exception:
            return None
    return time.time() - t, transcript


async def _time_pipeline(model: str, clip: Path) -> dict[str, Any] | None:
    """Path B: Whisper -> resident text model. Returns timings or None if Whisper
    isn't installed (so the caller SKIPs path B with that reason)."""
    stt = _whisper_transcribe(clip)
    if stt is None:
        return None
    stt_s, transcript = stt
    from .llm import ask_ollama
    t = time.time()
    answer = await ask_ollama(transcript, role="chat", model=model)
    llm_s = time.time() - t
    return {"stt_s": round(stt_s, 2), "llm_s": round(llm_s, 2),
            "total_s": round(stt_s + llm_s, 2), "transcript": transcript, "answer": answer}


async def run_voice_benchmark(clip_path: str, *, audio_model: str = "gemma4:e2b",
                              resident_model: str | None = None) -> dict[str, Any]:
    """Compare audio-native vs Whisper-pipeline for one spoken clip. Either path
    SKIPs (not fails) if its prerequisite is missing: no audio support in Ollama, or
    no Whisper installed. Returns a structured result for the caller to print."""
    from .settings import get_settings
    clip = Path(clip_path)
    if not clip.exists():
        return {"ok": False, "error": f"audio clip not found: {clip_path}"}
    resident = resident_model or get_settings().ollama_chat_model

    result: dict[str, Any] = {"clip": str(clip), "audio_model": audio_model,
                              "resident_model": resident}

    # Path A: probe first, then time only if supported.
    probe = await probe_audio_support(audio_model, clip)
    if not probe["ok"]:
        result["audio_native"] = {"skipped": True,
                                  "reason": "Ollama did not accept audio input for "
                                            f"{audio_model}: {probe.get('error')}",
                                  "hint": "needs an Ollama build with Gemma 4 audio input; "
                                          "update Ollama and re-pull the model"}
    else:
        try:
            dt, out = await _time_audio_native(audio_model, clip, probe["field"])
            result["audio_native"] = {"skipped": False, "latency_s": round(dt, 2),
                                      "answer": out[:300], "field": probe["field"]}
        except Exception as exc:
            result["audio_native"] = {"skipped": True, "reason": f"audio call failed: {exc}"}

    # Path B: Whisper pipeline.
    pipe = await _time_pipeline(resident, clip)
    if pipe is None:
        result["pipeline"] = {"skipped": True,
                              "reason": "Whisper not installed (need whisper + ffmpeg)",
                              "hint": "mongoose setup-voice"}
    else:
        result["pipeline"] = {"skipped": False, **pipe}

    # Verdict only when BOTH ran.
    a, b = result["audio_native"], result["pipeline"]
    if not a.get("skipped") and not b.get("skipped"):
        faster = "audio_native" if a["latency_s"] < b["total_s"] else "pipeline"
        result["verdict"] = {
            "faster": faster,
            "audio_native_s": a["latency_s"],
            "pipeline_s": b["total_s"],
            "delta_s": round(abs(a["latency_s"] - b["total_s"]), 2),
            "note": "latency only; check the answers above for correctness, the real tie-breaker",
        }
    else:
        result["verdict"] = {"faster": None,
                             "note": "one path skipped; no head-to-head this run"}
    return result


def format_voice_result(r: dict[str, Any]) -> str:
    """Human-readable rendering of a voice-benchmark result."""
    if not r.get("ok", True) and "error" in r:
        return f"Voice benchmark: {r['error']}"
    lines = [f"=== Voice benchmark: {Path(r['clip']).name} ==="]
    a = r["audio_native"]
    if a.get("skipped"):
        lines.append(f"  audio-native ({r['audio_model']}): SKIP, {a['reason']}")
    else:
        lines.append(f"  audio-native ({r['audio_model']}): {a['latency_s']}s")
        lines.append(f"      answer: {a['answer']}")
    b = r["pipeline"]
    if b.get("skipped"):
        lines.append(f"  pipeline (Whisper -> {r['resident_model']}): SKIP, {b['reason']}")
    else:
        lines.append(f"  pipeline (Whisper -> {r['resident_model']}): {b['total_s']}s "
                     f"(stt {b['stt_s']}s + llm {b['llm_s']}s)")
        lines.append(f"      heard: {b['transcript']}")
        lines.append(f"      answer: {b['answer'][:200]}")
    v = r["verdict"]
    if v.get("faster"):
        lines.append(f"  -> faster: {v['faster']} by {v['delta_s']}s ({v['note']})")
    else:
        lines.append(f"  -> {v['note']}")
    return "\n".join(lines)
