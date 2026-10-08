# Talking to Mongoose (voice)

`mongoose voice` is a spoken wrapper around the same chat/home-control path as
`mongoose chat`. It speaks every reply aloud, and can optionally take microphone
input. It keeps one conversation and one resident model across turns, so it's a
real spoken session, not a `mongoose chat ... | say` pipe (which reloads persona
and memory each turn and mangles multi-sentence prose).

## Output only (hands-free listening), zero setup on macOS

```
mongoose voice
```

You type; Mongoose speaks the reply through the built-in macOS `say`. Good for
cooking, walking around, or resting your eyes. Type `exit` to quit.

One-shot (speak a single reply and exit, handy for scripts):

```
mongoose voice --say "what's my brief?"
```

## Microphone input (speech in, speech out)

```
mongoose voice --mic
```

This needs two optional local tools (both private, nothing leaves the machine):
- a recorder: `sox` (provides `rec`), via `brew install sox`
- a transcriber: a local `whisper` CLI, e.g. `pip install openai-whisper` or a
  whisper.cpp build on PATH

If either is missing, `--mic` automatically falls back to typed input for that
turn (and tells you once), so it never hard-fails. Voice OUTPUT always works.

## What it routes

Same logic as Assist: a home-control phrase ("turn off the study lights") runs a
tool turn and speaks the result; anything else is a persona+memory chat turn. So
the spoken path behaves exactly like the typed and HA-Assist paths.

## Settings (.env, all optional)

```
MONGOOSE_TTS_CMD=say            # TTS command (macOS `say` by default)
MONGOOSE_TTS_VOICE=             # e.g. "Daniel", "Serena"; blank = system default
MONGOOSE_TTS_RATE=              # words/min for `say`, e.g. 180; blank = default
MONGOOSE_STT_MODEL=base.en      # whisper model for --mic transcription
```

Pick a voice you like with `say -v '?'` to list installed voices, then set
`MONGOOSE_TTS_VOICE`. A faster `MONGOOSE_TTS_RATE` (e.g. 200) makes long replies
less tedious.

## Interactive mic session (one session, choose a mode)

```
mongoose voice --session
```

This starts a single continuous session and asks which listening style you want
up front:

1. **Continuous** — it listens, you talk, it replies, and it listens again.
   Each turn auto-stops when you stop talking (silence-detected, no fixed clip).
2. **Push-to-talk** — press Enter to start each turn; it records until you go quiet.
3. **Wake-word** — stays idle until it hears the wake word (default "mongoose",
   set `MONGOOSE_WAKE_WORD`), then listens for your request.
4. **Single turn** — one spoken question, one spoken answer, then exits.

All modes auto-stop on silence and speak every reply. Say "exit" (or Ctrl-C) to
end. Needs the same `sox` + `whisper` tools as `--mic`; if they're missing it
tells you exactly what to install and points you at the type-and-listen mode.

Settings: `MONGOOSE_WAKE_WORD=mongoose` sets the wake-word trigger.

## Troubleshooting the mic

`mongoose doctor` now reports a `voice` check (rec + whisper + ffmpeg + TTS). All
three CLI tools are needed for mic input:
- `brew install sox` (provides `rec`)
- `pip install openai-whisper` (into the SAME venv you run mongoose from)
- `brew install ffmpeg` (whisper decodes audio with it)

If the session keeps saying it didn't catch anything, the silence threshold is
likely wrong for your room. Tune it without editing code:
- noisy room or sensitive mic: raise `MONGOOSE_MIC_THRESHOLD` (e.g. `4%`)
- quiet mic: lower it (e.g. `1%`)
- it cuts you off too soon: raise `MONGOOSE_MIC_SILENCE_S` (e.g. `2.5`)

Push-to-talk (mode 2) bypasses start-detection, so if mode 1 misses you but mode
2 works, it's purely the threshold.

If replies print but you hear nothing, that's macOS audio output, not Mongoose.
Test with `say "hello"` directly; check System Settings > Sound > Output; a
wedged CoreAudio sometimes needs `sudo killall coreaudiod`.

## One-command mic setup

Instead of installing sox/ffmpeg/whisper by hand, run:

```
mongoose setup-voice
```

It detects what's missing, shows you exactly what it will run (brew for
sox/ffmpeg, pip for whisper INTO the venv you're running from), asks for
confirmation, then installs. `--yes` skips the prompt. This avoids the two
common traps: installing whisper into the wrong venv, and discovering missing
tools one error at a time. A fresh `bootstrap_macos.sh` now installs these
automatically; `update` tells you to run `setup-voice` if they're missing.
