from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Optional

from .settings import get_settings

# A usable voice wrapper around Mongoose chat: speaks replies aloud (TTS) and
# optionally takes spoken input from the microphone (STT). Designed for the Mac
# mini, where `say` is always present, but every external dependency is optional
# and degrades gracefully:
#   - TTS: macOS `say` (built in). If absent, falls back to printing only.
#   - STT: tries, in order, a local whisper CLI, then sox/`rec`+whisper, then
#     prompts for typed input. No mic dependency is required to use voice OUTPUT.
#
# This is deliberately a small REPL, not a `mongoose chat ... | say` pipe, because
# a pipe re-loads the persona/memory per turn, mangles multi-sentence prose, and
# can't do conversation continuity or barge-in. A loop keeps one conversation and
# one resident model across turns.


def _tts_available() -> bool:
    return shutil.which(get_settings().tts_cmd) is not None


def speak(text: str) -> None:
    """Speak text aloud via the configured TTS command (macOS `say` by default).
    Always also prints, so output is never lost if TTS is unavailable."""
    text = (text or "").strip()
    if not text:
        return
    print(text)
    settings = get_settings()
    cmd = settings.tts_cmd
    if not shutil.which(cmd):
        return
    args = [cmd]
    if settings.tts_voice:
        args += ["-v", settings.tts_voice]
    if settings.tts_rate:
        args += ["-r", str(settings.tts_rate)]
    try:
        # `say` reads the text as a trailing arg; pass via stdin to avoid arg-length
        # and quoting issues on long replies.
        r = subprocess.run(args, input=text, text=True, timeout=120,
                           capture_output=True, check=False)
        if r.returncode != 0 and not getattr(speak, "_warned", False):
            speak._warned = True
            print(f"  (note: '{cmd}' exited {r.returncode}; replies are printed but may "
                  f"not be spoken. Check System Settings > Sound > Output, or test with: {cmd} \"hello\")")
    except Exception:
        if not getattr(speak, "_warned", False):
            speak._warned = True
            print(f"  (note: couldn't run '{cmd}'; replies are printed but not spoken.)")


def which_cli(cmd: str) -> Optional[str]:
    """Find a CLI on PATH, or in the RUNNING interpreter's own bin. A server started via
    `.venv/bin/python -m mongoose ...` does not put `.venv/bin` on PATH (no `activate`), so a plain
    shutil.which('whisper') missed the venv-installed whisper and voice wrongly reported 'not set
    up' after a hand-launched restart. Probing sys.executable's dir makes detection PATH-independent."""
    import os
    import sys
    hit = shutil.which(cmd)
    if hit:
        return hit
    for d in (os.path.dirname(sys.executable), os.path.join(sys.prefix, "bin")):
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _whisper_cmd() -> Optional[str]:
    for c in ("whisper", "whisper-cli", "whisper-cpp"):
        found = which_cli(c)
        if found:
            return found
    return None


def _stt_ready() -> tuple[bool, str]:
    """Check the STT toolchain. listen_until_silence needs `rec` (sox's recorder),
    a whisper CLI, AND ffmpeg (whisper uses it to decode the audio). All three
    must be present, or the session would open and then fail mid-turn."""
    rec = shutil.which("rec")
    whisper = _whisper_cmd()
    ffmpeg = shutil.which("ffmpeg")
    if rec and whisper and ffmpeg:
        return True, ""
    missing = []
    if not rec:
        missing.append("sox (provides `rec`): brew install sox")
    if not whisper:
        missing.append("a whisper CLI: pip install openai-whisper")
    if not ffmpeg:
        missing.append("ffmpeg (whisper decodes audio with it): brew install ffmpeg")
    return False, "Microphone input needs: " + "; ".join(missing)


def voice_status() -> dict:
    """One consolidated, read-only view of the voice stack for the Cockpit Voice card: TTS output, the
    STT toolchain, the configured wake word / model / mic, whether the browser push-to-talk turn is
    usable, and the hands-free roadmap (AEC + wake-word listener, not yet wired). Touches no audio."""
    s = get_settings()
    stt_ok, stt_msg = _stt_ready()
    whisper = _whisper_cmd()
    return {
        "ok": True,
        "tts": {
            "available": _tts_available(),
            "command": s.tts_cmd,
            "voice": s.tts_voice or "(system default)",
            "rate": s.tts_rate or "(default)",
            "note": ("In the cockpit, replies are spoken by the browser's own speech synthesis; the "
                     "`say` command is the terminal fallback."),
        },
        "stt": {
            "ready": stt_ok,
            "whisper": bool(whisper),
            "whisper_path": whisper or "",
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "sox": bool(shutil.which("rec")),
            "model": s.stt_model,
            "detail": stt_msg,
        },
        "config": {
            "wake_word": s.wake_word,
            "mic_threshold": s.mic_threshold,
            "stt_model": s.stt_model,
        },
        "browser_push_to_talk": True,
        "hands_free": {
            "available": False,
            "acoustic_echo_cancellation": False,
            "wake_word_listener": False,
            "roadmap": ("Push-to-talk works today (hold the mic, speak, Mongoose transcribes and "
                        "replies). Always-on 'Hey Mongoose' needs two more pieces, not yet wired: "
                        "acoustic echo cancellation (so the mic stops hearing Mongoose's own voice) and "
                        "an on-device wake-word listener. Both stay local: no cloud, no keys."),
        },
    }


def _transcribe(wav: str) -> Optional[str]:
    """Transcribe a wav file with the local whisper CLI."""
    import os
    import tempfile
    whisper = _whisper_cmd()
    if not whisper:
        return None
    settings = get_settings()
    try:
        out = subprocess.run(
            [whisper, wav, "--model", settings.stt_model, "--language", "en",
             "--output_format", "txt", "--output_dir", tempfile.gettempdir()],
            capture_output=True, text=True, timeout=180)
        txt_path = os.path.join(tempfile.gettempdir(),
                                os.path.basename(wav).rsplit(".", 1)[0] + ".txt")
        if os.path.exists(txt_path):
            with open(txt_path, encoding="utf-8") as f:
                text = f.read().strip()
            try:
                os.remove(txt_path)
            except OSError:
                pass
            return text or None
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def listen_until_silence(*, max_seconds: int = 30, silence_s: float | None = None,
                         threshold: str | None = None) -> Optional[str]:
    """Record from the mic and STOP automatically when you stop talking, using
    sox's silence detection (so turns are as long or short as your speech, not a
    fixed clip). Returns transcript text, or None if STT isn't available.

    The trigger threshold and trailing-silence window are tunable to the room/mic
    via MONGOOSE_MIC_THRESHOLD (e.g. "2%", raise it for a noisy room, lower it for
    a quiet mic) and MONGOOSE_MIC_SILENCE_S."""
    import os
    import tempfile
    settings = get_settings()
    thr = threshold or settings.mic_threshold
    sil = str(silence_s if silence_s is not None else settings.mic_silence_s)
    rec = shutil.which("rec")
    if not rec or not _whisper_cmd():
        return None
    wav = os.path.join(tempfile.gettempdir(), f"mongoose_mic_{os.getpid()}.wav")
    try:
        # Start recording after first sound; stop after the trailing-silence
        # window. `silence 1 0.1 <thr>` = begin once level exceeds thr;
        # `1 <sil> <thr>` = end after that much trailing silence below thr.
        subprocess.run(
            [rec, "-q", wav, "trim", "0", str(max_seconds),
             "silence", "1", "0.1", thr, "1", sil, thr],
            timeout=max_seconds + 5, check=False)
        if not os.path.exists(wav) or os.path.getsize(wav) < 2000:
            return ""  # nothing meaningful captured
        return _transcribe(wav)
    except Exception:
        return None
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def listen(seconds: int = 6) -> Optional[str]:
    """Record a fixed-duration clip and transcribe it. Returns None if no STT
    path is available (caller should fall back to typed input). Kept for the
    simple per-turn path; the interactive session uses listen_until_silence."""
    import os
    import tempfile
    rec = shutil.which("rec") or shutil.which("sox")
    if not rec or not _whisper_cmd():
        return None
    wav = os.path.join(tempfile.gettempdir(), f"mongoose_mic_{os.getpid()}.wav")
    try:
        subprocess.run([rec, "-q", wav, "trim", "0", str(seconds)],
                       timeout=seconds + 5, check=False)
        return _transcribe(wav)
    except Exception:
        return None
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


async def _one_turn(prompt: str, conversation_id: str, mode: str) -> str:
    """Run a single Mongoose turn: home-control text routes through the tool turn,
    everything else through persona+memory chat. Mirrors /assist so spoken and
    typed paths behave identically."""
    from .tools import _user_asked_for_home_control, run_tool_turn
    from .llm import chat_with_memory, LocalModelError
    try:
        if _user_asked_for_home_control(prompt):
            out = await run_tool_turn(prompt)
            return out.get("reply") or "Done."
        out = await chat_with_memory(prompt, conversation_id=conversation_id, role="chat", mode=mode)
        return out["reply"]
    except LocalModelError as exc:
        return exc.user_message


def run_voice(*, mic: bool = False, mode: str = "default",
              conversation_id: str = "voice", once: Optional[str] = None) -> int:
    """Interactive voice loop. Speaks every reply. With mic=True, listens for
    spoken input (falling back to typed input if STT is unavailable). Without it,
    reads typed input but still speaks replies (good for hands-free listening).

    `once` runs a single prompt and exits (useful for scripting/testing)."""
    if not _tts_available():
        print("(note: no TTS command found; replies will print but not speak. "
              "On macOS, `say` is built in.)")

    if once is not None:
        reply = asyncio.run(_one_turn(once, conversation_id, mode))
        speak(reply)
        return 0

    print("Mongoose voice. Speak or type. Say/type 'exit' to quit"
          + (" (mic on)." if mic else " (type input; replies are spoken)."))
    while True:
        try:
            if mic:
                print("\n[listening...]")
                heard = listen()
                if heard is None:
                    # No STT path; fall back to typed input for this turn.
                    heard = input("you (type): ").strip()
                else:
                    print(f"you (heard): {heard}")
            else:
                heard = input("\nyou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not heard:
            continue
        if heard.lower() in {"exit", "quit", "stop", "goodbye"}:
            speak("Goodbye.")
            return 0

        reply = asyncio.run(_one_turn(heard, conversation_id, mode))
        speak(reply)


# --- Interactive single-session mic, with a mode chosen up front ----------------

_SESSION_MODES = {
    "1": ("continuous", "Continuous: I listen, you talk, I reply, repeat. Stops on silence."),
    "2": ("ptt", "Push-to-talk: press Enter, speak, it stops when you go quiet."),
    "3": ("wake", "Wake-word: idle until you say the wake word, then it listens."),
    "4": ("smooth", "Single turn: one spoken question, one spoken answer, then exit."),
}


def _ask_mode() -> str:
    print("\nMongoose mic session. Pick a mode:")
    for k, (_, desc) in _SESSION_MODES.items():
        print(f"  {k}. {desc}")
    while True:
        choice = input("mode [1-4]: ").strip()
        if choice in _SESSION_MODES:
            return _SESSION_MODES[choice][0]
        print("Please choose 1, 2, 3, or 4.")


def _is_exit(text: str) -> bool:
    return text.strip().lower() in {"exit", "quit", "stop", "goodbye", "that's all"}


def _typed_spoken_loop(mode: str, conversation_id: str) -> int:
    """Fallback when STT is unavailable: read typed input, speak replies. Honours
    the voice expectation that mic input degrades gracefully rather than failing."""
    print("\n(Speech-to-text isn't available, so type your input. Replies are still "
          "spoken.) Say/type 'exit' to finish.")
    while True:
        try:
            heard = input("\nyou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not heard:
            continue
        if _is_exit(heard):
            speak("Goodbye.")
            return 0
        speak(asyncio.run(_one_turn(heard, conversation_id, mode)))


def run_session(*, mode: str = "default", conversation_id: str | None = None,
                wake_word: Optional[str] = None) -> int:
    """One interactive mic session. Asks which listening mode to use up front,
    then runs that loop until you exit. All modes auto-stop on silence (no fixed
    clip length) and speak every reply. If STT is unavailable, falls back to a
    typed+spoken loop rather than hard-failing."""
    if conversation_id is None:
        from .services.memory import DEFAULT_CONVERSATION_ID
        conversation_id = DEFAULT_CONVERSATION_ID
    if not _tts_available():
        print("(note: no TTS command found; replies will print but not speak.)")

    ok, hint = _stt_ready()
    if not ok:
        print(hint)
        return _typed_spoken_loop(mode, conversation_id)

    listen_mode = _ask_mode()
    wake = (wake_word or get_settings().wake_word or "mongoose").lower()

    def _strip_wake(text: str) -> str:
        """Remove a leading wake word so 'mongoose, turn off the light' yields
        'turn off the light'."""
        low = text.lower()
        idx = low.find(wake)
        if idx == -1:
            return text
        rest = text[idx + len(wake):]
        return rest.lstrip(",.:;-").strip()

    if listen_mode == "smooth":
        print("\nSpeak your question...")
        heard = listen_until_silence()
        if heard:
            print(f"you: {heard}")
            speak(asyncio.run(_one_turn(heard, conversation_id, mode)))
        else:
            print("(didn't catch anything)")
        return 0

    print(f"\nSession started ({listen_mode}). Say 'exit' to finish.")
    if listen_mode == "wake":
        print(f"Idle. Say '{wake}' to wake me.")

    consecutive_fail = 0
    while True:
        try:
            if listen_mode == "ptt":
                input("\n[press Enter, then speak] ")
            elif listen_mode == "wake":
                trigger = listen_until_silence(max_seconds=12)
                if not trigger or wake not in trigger.lower():
                    continue
                # If the wake word and the command came in one utterance
                # ("mongoose, turn off the light"), use the remainder directly
                # instead of discarding it and listening again.
                command = _strip_wake(trigger)
                if command:
                    print(f"you: {command}")
                    if _is_exit(command):
                        speak("Goodbye.")
                        return 0
                    speak(asyncio.run(_one_turn(command, conversation_id, mode)))
                    print(f"Idle. Say '{wake}' to wake me.")
                    continue
                speak("Yes?")

            heard = listen_until_silence()
            if heard is None:
                # A single failed capture is transient (e.g. whisper got a silent
                # clip): retry. Only after several in a row do we conclude STT is
                # genuinely broken and degrade to typed input.
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    print("(couldn't capture audio a few times; switching to typed input)")
                    return _typed_spoken_loop(mode, conversation_id)
                print("(didn't catch that, listening again...)")
                continue
            consecutive_fail = 0
            heard = heard.strip()
            if not heard:
                if listen_mode == "continuous":
                    continue  # silence, keep listening
                print("(didn't catch anything)")
                continue
            print(f"you: {heard}")
            if _is_exit(heard):
                speak("Goodbye.")
                return 0
            speak(asyncio.run(_one_turn(heard, conversation_id, mode)))
            if listen_mode == "wake":
                print(f"Idle. Say '{wake}' to wake me.")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


# --- Voice dependency setup --------------------------------------------------

def setup_voice(*, yes: bool = False) -> int:
    """Detect and (with confirmation) install the voice mic toolchain:
    sox + ffmpeg via Homebrew, and openai-whisper via pip INTO THE CURRENT VENV.

    Safety: pip always targets sys.executable (the running interpreter), so it
    can never install into the wrong venv, the exact failure mode that bit a real
    user. brew is only invoked if present. Nothing runs without confirmation
    unless --yes is passed."""
    import sys
    import platform

    rec = shutil.which("rec")
    whisper = _whisper_cmd()
    ffmpeg = shutil.which("ffmpeg")

    if rec and whisper and ffmpeg:
        print("Voice toolchain is already complete (sox + whisper + ffmpeg). You're set.")
        return 0

    brew = shutil.which("brew")
    brew_pkgs = []
    if not rec:
        brew_pkgs.append("sox")
    if not ffmpeg:
        brew_pkgs.append("ffmpeg")
    need_whisper = not whisper

    print("Voice mic input needs the following, which are missing:")
    for pkg in brew_pkgs:
        print(f"  - {pkg} (Homebrew)")
    if need_whisper:
        print(f"  - openai-whisper (pip, into this venv: {sys.executable})")

    if platform.system() != "Darwin" and brew_pkgs:
        print("\nThis auto-installer targets macOS/Homebrew. On another OS, install "
              + ", ".join(brew_pkgs) + " with your package manager, then re-run.")
    if brew_pkgs and not brew:
        print("\nHomebrew not found. Install it from https://brew.sh, then re-run, "
              "or install " + ", ".join(brew_pkgs) + " manually.")
        # still offer to do the pip part
        brew_pkgs = []

    if not yes:
        plan = []
        if brew_pkgs:
            plan.append("brew install " + " ".join(brew_pkgs))
        if need_whisper:
            plan.append(f"{sys.executable} -m pip install openai-whisper")
        if not plan:
            print("\nNothing I can auto-install here. See the notes above.")
            return 1
        print("\nI will run:")
        for cmd in plan:
            print(f"  {cmd}")
        try:
            ans = input("\nProceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if ans not in ("y", "yes"):
            print("Cancelled. Nothing installed.")
            return 1

    ok = True
    if brew_pkgs and brew:
        print(f"\nInstalling via Homebrew: {' '.join(brew_pkgs)} ...")
        r = subprocess.run([brew, "install", *brew_pkgs])
        ok = ok and (r.returncode == 0)
    if need_whisper:
        print(f"\nInstalling openai-whisper into {sys.executable} ...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "openai-whisper"])
        ok = ok and (r.returncode == 0)

    # Re-check.
    rec = shutil.which("rec")
    whisper = _whisper_cmd()
    ffmpeg = shutil.which("ffmpeg")
    if rec and whisper and ffmpeg:
        print("\nDone. Voice toolchain complete. Try: mongoose voice --session")
        return 0
    still = [n for n, p in (("sox/rec", rec), ("whisper", whisper), ("ffmpeg", ffmpeg)) if not p]
    print(f"\nStill missing: {', '.join(still)}. "
          "If brew just installed something, open a fresh terminal so PATH updates, then re-run.")
    # The toolchain is NOT complete, so this is a failure regardless of whether the
    # install commands reported success (brew can succeed while the binary isn't yet
    # on the current shell's PATH). Return non-zero so callers/scripts don't treat
    # an incomplete setup as done.
    return 1
