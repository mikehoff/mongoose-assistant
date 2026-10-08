"""Second-lane surpriser: unprompted benchmark revelations.

The first lane is the cross-domain suggestion surpriser (surpriser.py): in-memory, polled, "I noticed X
relates to Y". This is the SECOND lane we discussed: the single permanent-conversation brain itself
sends unprompted messages about what the background benchmarks are finding, so Mongoose occasionally
surprises you with a genuine new message ("I've been benchmarking overnight, and qwen is now clearly
faster for tool calls, want me to switch?").

Design intent (important): these must read as Mongoose GENUINELY initiating a message, NOT as if you
typed something. So a revelation is written as an ASSISTANT-role message straight into the permanent
`mongoose-main` conversation. It then appears in the Chat tab exactly like any Mongoose message, in the
real conversation history, persistent, attributed to Mongoose, never faked as user input. There is a
hidden prompt/template behind it (that's fine and expected), but the surface is an honest Mongoose
message. When the revelation is notable (a clear better setup, or a finished overnight run), it also
pushes once to the phone.

It is quiet by nature: it only speaks when there's something actually new to say (a fresh benchmark run
since last time, or a newly-vested better setup), it de-dupes so it never repeats the same revelation,
and it respects the same rate-limit/quiet-hours discipline as lane one. It reads benchmark results and
writes a chat message; it changes no code and applies no model switch by itself.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from ..db import get_app_state, set_app_state, utc_now_iso
from .memory import DEFAULT_CONVERSATION_ID, add_message

# app_state keys
_LAST_REVEAL_SIG = "benchmark_reveal_last_sig"
_LAST_REVEAL_AT = "benchmark_reveal_last_at"

# Don't reveal more than once per this many hours, even if multiple runs happen (stay un-naggy).
_MIN_HOURS_BETWEEN = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hours_since(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (_now() - then).total_seconds() / 3600.0
    except Exception:
        return 1e9


def _read_benchmark_state() -> dict[str, Any]:
    """Read the visibility markers the benchmark job records, plus the tuning evaluation, to decide if
    there's a fresh, worth-sharing revelation."""
    last_run = get_app_state("benchmark_last_run", "")
    raw_summary = get_app_state("benchmark_last_summary", "")
    try:
        summary = json.loads(raw_summary) if raw_summary else {}
    except Exception:
        summary = {}
    return {"last_run": last_run, "summary": summary}


def _craft_revelation() -> dict[str, Any] | None:
    """Decide whether there's something new to say, and if so craft the message. Returns
    {text, why, signature, notable} or None to stay silent. Pure: reads state, writes nothing."""
    state = _read_benchmark_state()
    summary = state["summary"]
    if not summary or not summary.get("ran"):
        return None

    # The signature is the run time + the configured-vs-recommended shape, so a NEW run or a NEW
    # recommendation produces a new signature (and thus a new revelation), but re-reading the same
    # run does not.
    run_at = summary.get("at", state.get("last_run", ""))

    # Bring in the tuning evaluation for the "is there a better setup?" angle.
    rec_text = None
    notable = False
    try:
        from ..model_tuning import evaluate
        ev = evaluate(allow_auto_apply=False)  # read-only here; never auto-switch from the surpriser
        if ev.get("vested") and ev.get("actionable"):
            rec_text = ev.get("headline") or "a better model setup is now supported by the benchmarks"
            notable = True
        elif ev.get("vested") and ev.get("auto_applied"):
            rec_text = "I switched to a clearly better, safe model setup based on the latest benchmarks"
            notable = True
    except Exception:
        ev = {}

    models = summary.get("models", [])
    n = summary.get("result_count", 0)

    sig_basis = f"{run_at}|{rec_text or 'no-rec'}|{len(models)}"
    # Never use Python's built-in hash() for long-run dedupe: it is intentionally salted per
    # process, so the same benchmark revelation can get a different signature after restart and
    # repeat weeks later.
    signature = "bench:" + hashlib.sha256(sig_basis.encode("utf-8")).hexdigest()[:20]

    # de-dupe + rate-limit
    if get_app_state(_LAST_REVEAL_SIG, "") == signature:
        return None
    if _hours_since(get_app_state(_LAST_REVEAL_AT, "")) < _MIN_HOURS_BETWEEN:
        return None

    # craft the message , phrased as Mongoose volunteering something it found
    when = _friendly_when(run_at)
    if rec_text:
        text = (f"I ran model benchmarks {when} and have a finding: {rec_text}. "
                f"I compared {len(models)} model(s) across {n} scored check(s). "
                "Say the word and I'll show the comparison or make the change.")
        why = "from the overnight benchmark window"
    else:
        text = (f"Quiet update: I benchmarked {len(models)} model(s) {when} "
                f"({n} scored check(s)). Your current setup still looks like the right call, "
                "nothing to change, just keeping an eye on it.")
        why = "overnight benchmark check"
        notable = False  # a "nothing changed" note is in-chat only, no phone push

    return {"text": text, "why": why, "signature": signature, "notable": notable,
            "run_at": run_at}


async def run_benchmark_revelation_cycle(*, force: bool = False) -> dict[str, Any]:
    """The second-lane cycle the scheduler calls. If there's a fresh, worth-sharing benchmark finding,
    write it as a genuine Mongoose-initiated message into the permanent conversation (so it shows up in
    chat as a real Mongoose message, never as if the user typed it), and push to the phone if notable.
    Otherwise stay silent. `force` bypasses the rate-limit for manual testing only."""
    rev = _craft_revelation()
    if not rev:
        return {"surfaced": False, "reason": "nothing new to reveal"}

    # 1. write into the permanent conversation as an assistant (Mongoose) message , this is what makes
    #    it appear as a genuine unprompted Mongoose message in the Chat tab and in history.
    add_message(DEFAULT_CONVERSATION_ID, "assistant", rev["text"])

    # 2. record signature + time so we don't repeat it
    set_app_state(_LAST_REVEAL_SIG, rev["signature"])
    set_app_state(_LAST_REVEAL_AT, utc_now_iso())

    # 3. push to phone when notable
    pushed = False
    if rev.get("notable"):
        try:
            from .notifications import send_to_phone
            res = await send_to_phone("Mongoose, benchmark finding", rev["text"][:280],
                                      priority="default", tags="bar_chart",
                                      target_device_profiles=("primary_phone", "home_tablet"))
            pushed = bool(res.get("sent"))
        except Exception:
            pushed = False

    return {"surfaced": True, "text": rev["text"], "notable": rev.get("notable", False),
            "pushed": pushed, "signature": rev["signature"]}


def latest_benchmark_visibility() -> dict[str, Any]:
    """A compact, read-only view of benchmark activity for the cockpit: when benchmarks last ran, what
    they found, and when the last revelation was surfaced. Lets the user SEE the benchmark lane even if
    they weren't watching chat at the moment a revelation landed."""
    state = _read_benchmark_state()
    # Whether the overnight benchmark lane is actually switched on. Without this the cockpit
    # card promised "runs automatically 5-8am" even when the lane was off and would never run,
    # so the user had no way to tell it was idle, nor to turn it on.
    enabled = False
    window = "05:00-08:00"
    try:
        from ..settings import get_settings
        s = get_settings()
        enabled = bool(getattr(s, "benchmark_enabled", False))
        window = f"{int(getattr(s, 'benchmark_window_start', 5)):02d}:00-" \
                 f"{int(getattr(s, 'benchmark_window_end', 8)):02d}:00"
    except Exception:
        pass
    return {
        "enabled": enabled,
        "last_run": state.get("last_run") or None,
        "last_started": get_app_state("benchmark_last_started", "") or None,
        "summary": state.get("summary") or {},
        "last_revelation_at": get_app_state(_LAST_REVEAL_AT, "") or None,
        "window": f"{window} local (configurable)",
    }


def benchmark_history(limit: int = 30) -> list[dict[str, Any]]:
    """Every recorded benchmark run, newest first (capped at `limit`).

    Runs are stored at data/benchmarks/<date>/<model>/<task>.json, each holding model, task, prompt
    and raw_output (only tasks that were captured for diagnosis are written). Returns per run: the
    date, the model(s) it covered, and per-task {task, model, prompt(truncated), summary, outcome}.
    Read-only; best-effort; never raises."""
    from pathlib import Path
    from ..settings import get_settings
    try:
        base = Path(get_settings().data_dir) / "benchmarks"
    except Exception:
        return []
    if not base.exists():
        return []
    runs: list[dict[str, Any]] = []
    try:
        date_dirs = sorted([d for d in base.iterdir() if d.is_dir()],
                           key=lambda p: p.name, reverse=True)
    except Exception:
        return []
    for date_dir in date_dirs:
        models: list[str] = []
        tasks: list[dict[str, Any]] = []
        try:
            model_dirs = sorted([m for m in date_dir.iterdir() if m.is_dir()],
                                key=lambda p: p.name)
        except Exception:
            model_dirs = []
        for model_dir in model_dirs:
            models.append(model_dir.name)
            for tf in sorted(model_dir.glob("*.json")):
                try:
                    rec = json.loads(tf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                prompt = str(rec.get("prompt") or "").strip().replace("\n", " ")
                if len(prompt) > 180:
                    prompt = prompt[:180] + "…"
                raw = str(rec.get("raw_output") or "").strip().replace("\n", " ")
                summary = (raw[:220] + "…") if len(raw) > 220 else raw
                tasks.append({
                    "task": rec.get("task") or tf.stem,
                    "model": rec.get("model") or model_dir.name,
                    "prompt": prompt,
                    "summary": summary,
                    "outcome": str(rec.get("score_reason") or "").strip(),
                })
        runs.append({"date": date_dir.name, "models": models,
                     "task_count": len(tasks), "tasks": tasks})
        if len(runs) >= limit:
            break
    return runs


# ----- helpers -----

def _friendly_when(iso: str) -> str:
    if not iso:
        return "recently"
    h = _hours_since(iso)
    if h < 1:
        return "just now"
    if h < 12:
        return "this morning"
    if h < 36:
        return "overnight"
    return "recently"
