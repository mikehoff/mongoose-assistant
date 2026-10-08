from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..settings import get_settings

# An overnight build-runner. Mongoose owns the PLAN, the persistence, the
# checkpointing, the per-step verification gate, and the bus logging. It does NOT
# write the code itself: each step is handed to Claude Code (a frontier model
# with real supervision), because a local 14B model cannot build production
# software unsupervised. Mongoose makes the night organised, gated, and
# resumable; Claude Code provides the quality.
#
# Safety stance (this matters because the target triages real safety-critical
# leaseholder requests): every step has an explicit, machine-checkable success
# condition. A step only advances if its check passes. A failing step BLOCKS and
# does not cascade. Nothing is ever deleted. The whole run is a git history of
# passing steps, reviewable as a diff in the morning.
#
# A plan lives at <data_dir>/builds/<plan_id>/ as:
#   plan.json         the ordered steps and their state (the checkpoint)
#   steps/<id>.md     human-readable task + result log per step (append-only)
#   repo/             the git working tree the build produces

STATUS = ("pending", "running", "done", "failed", "blocked")

import re as _re
# Step ids and plan ids become filesystem path components, so they must be
# strictly validated (no slashes, dots, traversal, whitespace).
_STEP_ID_RE = _re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,60}$")
_PLAN_ID_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[a-f0-9]{6}$")


def _builds_root() -> Path:
    p = get_settings().data_dir / "builds"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _plan_dir(plan_id: str) -> Path:
    if not _PLAN_ID_RE.match(plan_id or ""):
        raise ValueError(f"invalid plan id: {plan_id!r}")
    return _builds_root() / plan_id


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_steps(steps: list[dict[str, Any]]) -> None:
    """Validate step shapes for BOTH manual and model-generated plans (shared so
    the two paths can't diverge). Raises ValueError on any problem.

    Each step needs id, title, task, and a success criterion: either a structured
    `verify` spec (preferred, safe: a named verifier Mongoose runs) or a legacy
    `check` command string (deprecated; only runs if it's a safe structured check
    or allow_shell is set)."""
    from .verifiers import validate_verify
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list")
    ids = set()
    for s in steps:
        if not isinstance(s, dict):
            raise ValueError("each step must be a dict")
        for k in ("id", "title", "task"):
            if k not in s or not str(s[k]).strip():
                raise ValueError(f"step missing required field: {k}")
        # Success criterion: verify (preferred) or check (legacy). At least one.
        if "verify" in s and s["verify"] is not None:
            err = validate_verify(s["verify"])
            if err:
                raise ValueError(f"step {s['id']}: {err}")
        elif not str(s.get("check", "")).strip():
            raise ValueError(f"step {s['id']}: needs a 'verify' spec or a 'check' command")
        if not _STEP_ID_RE.match(str(s["id"])):
            raise ValueError(f"invalid step id (must match {_STEP_ID_RE.pattern}): {s['id']!r}")
        if s["id"] in ids:
            raise ValueError(f"duplicate step id: {s['id']}")
        ids.add(s["id"])
    for s in steps:
        for dep in s.get("depends_on", []) or []:
            if dep not in ids:
                raise ValueError(f"step {s['id']} depends on unknown id {dep}")


def create_plan(name: str, steps: list[dict[str, Any]], *, repo_subdir: str = "repo") -> dict[str, Any]:
    """Create a new build plan from a list of step specs. Each step spec needs:
        id, title, task (the prompt for Claude Code), check (a shell command that
        must exit 0 for the step to count as done).
    Optional: depends_on (list of step ids), retries (default 1).
    The plan is written to disk as the durable checkpoint."""
    validate_steps(steps)
    plan_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S") + "-" + uuid.uuid4().hex[:6]
    pdir = _plan_dir(plan_id)
    (pdir / "steps").mkdir(parents=True, exist_ok=True)
    (pdir / repo_subdir).mkdir(parents=True, exist_ok=True)

    norm_steps = []
    seen = set()
    for s in steps:
        sid = s["id"]
        if sid in seen:
            raise ValueError(f"duplicate step id: {sid}")
        seen.add(sid)
        norm_steps.append({
            "id": sid,
            "title": s["title"],
            "task": s["task"],
            "check": s.get("check"),
            "verify": s.get("verify"),
            "depends_on": s.get("depends_on", []),
            "retries": int(s.get("retries", 1)),
            "status": "pending",
            "attempts": 0,
            "result": None,
        })

    plan = {
        "plan_id": plan_id,
        "name": name,
        "repo_subdir": repo_subdir,
        "created": _now(),
        "approved": False,
        "steps": norm_steps,
    }
    _save_plan(plan)
    # Initialise the git repo so every step is a reviewable commit.
    repo = pdir / repo_subdir
    try:
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        # Set a local identity so commits never depend on the machine's global
        # git config (which may be unset on a fresh box).
        subprocess.run(["git", "config", "user.email", "builder@mongoose.local"], cwd=repo, check=False)
        subprocess.run(["git", "config", "user.name", "Mongoose Builder"], cwd=repo, check=False)
        subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "build: plan created"],
                       cwd=repo, check=False)
    except Exception:
        pass
    return plan


def _save_plan(plan: dict[str, Any]) -> None:
    _atomic_write(_plan_dir(plan["plan_id"]) / "plan.json", json.dumps(plan, indent=2))


def load_plan(plan_id: str) -> dict[str, Any] | None:
    path = _plan_dir(plan_id) / "plan.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_plans() -> list[dict[str, Any]]:
    out = []
    for p in sorted(_builds_root().glob("*/plan.json")):
        try:
            plan = json.loads(p.read_text(encoding="utf-8"))
            done = sum(1 for s in plan["steps"] if s["status"] == "done")
            out.append({"plan_id": plan["plan_id"], "name": plan["name"],
                        "approved": plan["approved"],
                        "progress": f"{done}/{len(plan['steps'])}"})
        except Exception:
            continue
    return out


def approve_plan(plan_id: str) -> bool:
    """A plan must be explicitly approved before it can run. No spec goes from
    text to an unsupervised overnight run without a human gate."""
    plan = load_plan(plan_id)
    if not plan:
        return False
    plan["approved"] = True
    _save_plan(plan)
    return True


def _append_step_log(plan_id: str, step_id: str, text: str) -> None:
    path = _plan_dir(plan_id) / "steps" / f"{step_id}.md"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n--- {_now()} ---\n{text}\n")


def _ready_steps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    done_ids = {s["id"] for s in plan["steps"] if s["status"] == "done"}
    ready = []
    for s in plan["steps"]:
        if s["status"] not in ("pending", "failed"):
            continue
        if s["attempts"] > s["retries"]:
            continue
        if all(dep in done_ids for dep in s["depends_on"]):
            ready.append(s)
    return ready


import shlex as _shlex

# Characters that enable shell features (chaining, redirection, substitution,
# globbing). A check containing any of these is "shell-shaped" and is only run
# when shell checks are explicitly allowed.
_SHELL_METACHARS = set(";&|`$><(){}*?!\n\\\"'")

# Safe structured checks we can run WITHOUT a shell: the model is steered toward
# these by the planner prompt. Each maps to an argv list run with shell=False.
# NOTE: `python -c` is deliberately NOT here. It is arbitrary code execution and
# has no place in an unattended runner's default-safe set. Python-level checks
# must be expressed as a test file run via pytest, not inline code.
_SAFE_CHECK_PREFIXES = (
    "python -m pytest",
    "pytest",
    "test -f ",
    "test -d ",
    "test -e ",
    "ruff check",
)


def _check_paths_contained(check: str, repo: Path) -> bool:
    """Every path-like argument in a structured check must stay within the repo.
    Rejects '..' traversal and absolute paths, so a 'safe' check can't point at
    files outside the build repo (e.g. `pytest ../evil_test.py`)."""
    repo_root = repo.resolve()
    for tok in _shlex.split(check):
        if tok.startswith("-"):
            continue  # flags like -q, --quiet
        # Treat tokens that look like paths (contain / or .. or a known suffix).
        looks_pathy = ("/" in tok or tok in ("..", ".") or tok.startswith("..")
                       or tok.endswith((".py", ".txt", ".cfg", ".toml", ".md")))
        if not looks_pathy:
            continue
        try:
            resolved = (repo_root / tok).resolve()
            resolved.relative_to(repo_root)
        except ValueError:
            return False
    return True


def _is_safe_structured_check(check: str, repo: Path | None = None) -> bool:
    c = check.strip()
    if not any(c.startswith(p) for p in _SAFE_CHECK_PREFIXES):
        return False
    # Reject shell metacharacters so e.g. "pytest; rm -rf ~" can't ride a safe prefix.
    if set(c) & _SHELL_METACHARS:
        return False
    # When a repo is given, every path argument must be contained within it.
    if repo is not None and not _check_paths_contained(c, repo):
        return False
    return True


def _run_check(check: str, cwd: Path, *, allow_shell: bool = False) -> tuple[bool, str]:
    """Run a step's success check. By default, only safe structured checks
    (pytest / test -f / test -d / ruff) are run, with shell=False. A check that
    uses shell features (or arbitrary code like `python -c`) is refused unless
    allow_shell is set, because in the unattended builder the check string is
    model-generated and would otherwise be arbitrary execution."""
    check = (check or "").strip()
    try:
        if _is_safe_structured_check(check, cwd) and not allow_shell:
            argv = _shlex.split(check)
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=600)
            return r.returncode == 0, (r.stdout + r.stderr)[-2000:]
        if not allow_shell:
            return False, ("check refused: not a safe structured check and shell checks "
                           "are disabled. Use pytest/test -f/test -d/ruff, or pass "
                           "allow_shell. Check was: " + check[:120])
        # Explicitly allowed shell execution (dangerous; opt-in only).
        r = subprocess.run(check, shell=True, cwd=cwd, capture_output=True, text=True, timeout=600)
        return r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return False, "check timed out"
    except Exception as exc:
        return False, f"check error: {exc}"


def _resolve_claude_cmd(cmd: str) -> str | None:
    """Find the Claude Code binary regardless of the caller's PATH. The launchd server PATH excludes
    ~/.local/bin, so a bare 'claude' fails there; probe the known install locations. Returns an
    absolute path or None."""
    import shutil
    p = Path(cmd).expanduser()
    if p.is_absolute() and p.exists():
        return str(p)
    found = shutil.which(cmd)
    if found:
        return found
    home = Path.home()
    for cand in (home / ".local/bin" / cmd, Path("/opt/homebrew/bin") / cmd,
                 Path("/usr/local/bin") / cmd, home / ".local/bin/claude",
                 home / ".claude/local/claude"):
        if cand.exists():
            return str(cand)
    return None


def _invoke_claude_code(task: str, repo: Path) -> tuple[bool, str, bool]:
    """Hand a step to Claude Code in headless, no-prompt mode. Mongoose grants the
    permissions so Code never stops to ask the human. Returns
    (ok, output, rate_limited).

    Uses `--permission-mode acceptEdits`. The old `dontAsk` was wrong: it means "do not
    prompt AND deny anything that would need approval", so in headless mode it DENIED every
    Write/Edit/Bash-write, the agent could only read, made zero changes, and the run still
    exited 0 (the false-green that shipped the OLD plugin code as if a spec were implemented).
    `acceptEdits` auto-approves the file Write/Edit the author loop needs while leaving other
    tool gates in place (safer than bypassPermissions, which disables every gate). The agent is
    still fenced to the target dir by the task prompt, and plugin_author validates + rolls back.
    Wrapped in a timeout because a stuck headless agent otherwise runs forever.
    Non-zero exit can mean rate-limit exhaustion, which we detect so the runner
    can stop escalating instead of burning the whole budget on failures."""
    settings = get_settings()
    cmd = settings.claude_code_cmd
    if not cmd:
        return False, "claude_code_cmd not configured", False
    # Resolve the real binary: the server runs under launchd with a restricted PATH that excludes
    # ~/.local/bin (where Claude Code installs), so a bare "claude" is not found and the run fails with
    # a confusing agent_run_failed. Probe the usual install locations explicitly.
    resolved = _resolve_claude_cmd(cmd)
    if not resolved:
        return (False, f"Claude Code binary '{cmd}' not found on PATH or in the usual locations "
                f"(~/.local/bin, /opt/homebrew/bin, /usr/local/bin). Install Claude Code or set "
                f"claude_code_cmd to its absolute path.", False)
    cmd = resolved
    try:
        r = subprocess.run(
            [cmd, "-p", task, "--permission-mode", "acceptEdits",
             "--output-format", "stream-json", "--verbose"],
            cwd=repo, capture_output=True, text=True,
            timeout=settings.build_step_timeout_s)
        raw_out = (r.stdout + r.stderr)
        low = raw_out.lower()
        rate_limited = ("rate limit" in low or "rate_limit" in low
                        or "usage limit" in low or "quota" in low)
        try:
            from .agent_output_hygiene import summarize_stream_json
            out = summarize_stream_json(raw_out, max_chars=4000)
        except Exception:
            out = raw_out[-4000:]
        return r.returncode == 0, out, rate_limited
    except FileNotFoundError:
        return False, f"claude code binary not found: {cmd}", False
    except subprocess.TimeoutExpired:
        return False, "claude code step timed out", False
    except Exception as exc:
        return False, f"claude code error: {exc}", False


def _run_sync(coro) -> Any:
    """Run an async coroutine from sync code. The builder is invoked from the CLI
    (sync), so this is safe. If it's ever called from inside a running event loop
    (e.g. a future async HTTP route), fail with a clear message instead of the
    cryptic 'event loop is already running'."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()  # avoid 'coroutine was never awaited' warning
    raise RuntimeError(
        "builder functions are sync and must not be called from an async context; "
        "run them in a thread (e.g. asyncio.to_thread) if calling from a route")


def _invoke_local_draft(task: str, repo: Path) -> tuple[bool, str]:
    """Have the local code model draft the step. Free, unlimited, no permissions
    needed: it returns code/file contents which we write to the repo. The local
    model does the high-volume grind; Code reviews and fixes afterwards.

    The model is asked to return a JSON list of {path, content} files. We parse
    and write them. On any parse failure we treat the draft as empty and let Code
    handle the step from scratch, so a weak local turn never blocks progress."""
    from ..llm import ask_ollama, LocalModelError

    system = (
        "You are drafting code files for a prototyping build step. Return ONLY a "
        "JSON array of objects, each {\"path\": relative/path, \"content\": file text}. "
        "No prose, no markdown fences. Draft is fine; it will be reviewed and fixed."
    )
    try:
        raw = _run_sync(ask_ollama(task, system=system, role="code"))
    except LocalModelError as exc:
        return False, f"local model unavailable: {exc}"
    except Exception as exc:
        return False, f"local draft error: {exc}"

    text = raw.strip().strip("`")
    if text.startswith("json"):
        text = text[4:].strip()
    try:
        files = json.loads(text)
        if not isinstance(files, list):
            return False, "local draft not a list"
    except Exception:
        return False, "local draft not parseable JSON"

    written = []
    repo_root = repo.resolve()
    for f in files:
        try:
            rel = str(f["path"]).lstrip("/")
            dest = (repo_root / rel).resolve()
            # Proper containment: relative_to raises if dest escapes repo_root.
            # (startswith is a classic prefix bug: /repo allows /repoevil.)
            dest.relative_to(repo_root)
            # Reject symlink traversal: no path component may be a symlink that
            # points outside the repo.
            if dest.is_symlink() or any(p.is_symlink() for p in dest.parents
                                        if str(p).startswith(str(repo_root))):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f.get("content", ""), encoding="utf-8")
            written.append(rel)
        except (ValueError, KeyError, OSError):
            continue
    return bool(written), f"local drafted: {', '.join(written) or '(nothing usable)'}"


def _invoke_code_step(task: str, repo: Path, *, where: str = "local",
                      allow_api_key: bool = False) -> tuple[bool, str, bool]:
    """Run a Claude Code build step either on the Mac (local subprocess) or on the Pi (over SSH).
    Returns the same (ok, output, rate_limited) contract regardless of where it ran, so the runner is
    location-agnostic. 'pi' offloads to the always-on, subscription-billed Pi build node; on the Pi
    path the repo is synced over first."""
    if where == "pi":
        from . import remote_build as RB
        rd = RB.readiness(allow_api_key=allow_api_key)
        if not rd.get("ok") or not rd.get("ready"):
            # Fall back to local rather than failing the whole step, but say so in the output.
            note = f"[pi build unavailable: {rd.get('detail')}] falling back to local Claude Code.\n"
            ok, out, rl = _invoke_claude_code(task, repo)
            return ok, note + out, rl
        synced = RB.sync_repo_to_pi(repo)
        if not synced.get("ok"):
            note = f"[pi sync failed: {synced.get('error')}] falling back to local.\n"
            ok, out, rl = _invoke_claude_code(task, repo)
            return ok, note + out, rl
        # Take a v3 lock + announce on the bus before invoking, so the build is safe alongside
        # Chat/Code Claude on the Pi. If a live peer holds the build lock, back off to local.
        lk = RB.acquire_lock("mongoose-build", ttl_seconds=getattr(get_settings(), "build_step_timeout_s", 1800) + 120)
        if not lk.get("ok"):
            note = f"[pi build lock unavailable: {lk.get('detail', lk.get('error'))}] falling back to local.\n"
            ok, out, rl = _invoke_claude_code(task, repo)
            return ok, note + out, rl
        try:
            if lk.get("reclaimed"):
                RB._bus_announce("lock-reclaim",
                                 f"mongoose reclaimed a stale 'mongoose-build' lock (prev sid "
                                 f"{lk.get('previous_sid')}) for a build step.")
            RB._bus_announce("mongoose-build-start",
                             f"mongoose is building a step on the Pi. Task: {task[:160]}")
            ok, out, rl = RB.invoke_remote_build(task, synced["remote_repo"], allow_api_key=allow_api_key)
            RB._bus_announce("mongoose-build-done" if ok else "mongoose-build-failed",
                             f"mongoose Pi build step {'succeeded' if ok else 'did not succeed'}.")
            return ok, out, rl
        finally:
            RB.release_lock("mongoose-build")
    return _invoke_claude_code(task, repo)


def run_step(plan_id: str, step_id: str, *, post_to_bus: bool = True,
             code_budget_left: int | None = None, allow_shell: bool = False,
             where: str = "local", allow_api_key: bool = False) -> dict[str, Any]:
    """Execute one step, two-tier:
        1. local model drafts the files (free, no permissions);
        2. Claude Code reviews and fixes the draft (headless, Mongoose-granted
           permissions), if there's Code budget left;
        3. run the check; commit on pass, block on fail.
    where='local' runs Tier 2 on the Mac; where='pi' offloads it to the Pi build node (always-on,
    subscription-billed) over SSH, syncing the repo there first.
    Returns the usual status plus code_used (0/1) and rate_limited so the loop can
    manage the budget. Checkpoints after every step."""
    plan = load_plan(plan_id)
    if not plan:
        return {"ok": False, "error": "no such plan"}
    if not plan["approved"]:
        return {"ok": False, "error": "plan not approved; call approve_plan first"}
    step = next((s for s in plan["steps"] if s["id"] == step_id), None)
    if not step:
        return {"ok": False, "error": "no such step"}

    repo = _plan_dir(plan_id) / plan["repo_subdir"]
    step["status"] = "running"
    step["started_at"] = _now()
    step["attempts"] += 1
    _save_plan(plan)
    _append_step_log(plan_id, step_id, f"ATTEMPT {step['attempts']}: {step['title']}")

    # Tier 1: local draft (always; free).
    drafted, draft_out = _invoke_local_draft(step["task"], repo)
    _append_step_log(plan_id, step_id, f"local draft ok={drafted}\n{draft_out}")

    # Tier 2: Code review/fix, if budget remains.
    code_used, rate_limited = 0, False
    success_desc = (f"named verifier {step['verify']}" if step.get("verify")
                    else f"the check: {step.get('check')}")
    if code_budget_left is None or code_budget_left > 0:
        review_task = (
            f"Review and complete this build step in the current repo. A local model "
            f"drafted an initial version which may be incomplete or wrong. Make it "
            f"correct and make its success criterion pass.\n\nSTEP: {step['title']}\n\n{step['task']}\n\n"
            f"Success is measured by {success_desc}"
        )
        built_ok, build_out, rate_limited = _invoke_code_step(review_task, repo, where=where,
                                                              allow_api_key=allow_api_key)
        code_used = 1
        _append_step_log(plan_id, step_id,
                         f"claude code review ok={built_ok} rate_limited={rate_limited} where={where}\n{build_out}")
    else:
        _append_step_log(plan_id, step_id, "code budget exhausted: local draft only")
        built_ok = drafted

    # Verify success. Prefer the structured `verify` spec (a named verifier
    # Mongoose runs); fall back to the legacy `check` string only if no verify is
    # present (and that path stays gated to safe structured checks / allow_shell).
    if step.get("verify"):
        from .verifiers import run_verifier
        check_ok, check_out = run_verifier(step["verify"], repo)
    else:
        check_ok, check_out = _run_check(step.get("check") or "", repo, allow_shell=allow_shell)
    _append_step_log(plan_id, step_id, f"check ok={check_ok}\n{check_out}")

    if check_ok:
        step["status"] = "done"
        step["result"] = "passed"
        try:
            subprocess.run(["git", "add", "-A"], cwd=repo, check=False)
            subprocess.run(["git", "commit", "-q", "-m", f"build: {step['id']} {step['title']}"],
                           cwd=repo, check=False)
        except Exception:
            pass
    else:
        step["status"] = "blocked" if step["attempts"] > step["retries"] else "failed"
        step["result"] = f"draft={drafted} code_used={code_used} check_ok={check_ok}"
    _save_plan(plan)

    if post_to_bus:
        from .bus import send_message
        send_message(sender="builder", to=get_settings().owner_id,
                     type="alert" if step["status"] == "blocked" else "note",
                     subject=f"[{plan['name']}] {step['id']}: {step['status']}",
                     body=f"{step['title']}\nresult: {step['result']}")
    return {"ok": True, "status": step["status"], "step": step_id,
            "code_used": code_used, "rate_limited": rate_limited}


def recover_stale_steps(plan_id: str, *, grace_seconds: int = 300) -> int:
    """Reset steps stuck in 'running' (e.g. after a crash mid-step) back to
    'failed' so they can be retried, instead of being skipped forever. A step is
    stale if its started_at is older than the step timeout + check timeout +
    grace. Returns how many were recovered."""
    from datetime import datetime, timezone as _tz
    plan = load_plan(plan_id)
    if not plan:
        return 0
    settings = get_settings()
    ceiling = settings.build_step_timeout_s + 600 + grace_seconds
    now = datetime.now(_tz.utc)
    recovered = 0
    for s in plan["steps"]:
        if s["status"] != "running":
            continue
        started = s.get("started_at")
        stale = True
        if started:
            try:
                age = (now - datetime.fromisoformat(started)).total_seconds()
                stale = age > ceiling
            except (ValueError, TypeError):
                stale = True
        if stale:
            s["status"] = "failed"
            s["result"] = "recovered from stale 'running' (likely crash)"
            recovered += 1
    if recovered:
        _save_plan(plan)
    return recovered


def run_plan(plan_id: str, *, max_steps: int = 100, post_to_bus: bool = True,
             code_call_budget: int | None = None, allow_shell: bool = False,
             max_seconds: int | None = None, where: str = "local",
             allow_api_key: bool = False) -> dict[str, Any]:
    """The overnight loop: repeatedly run whichever steps are ready until none
    are, then post a summary. Safe to re-run (done steps skip, blocked steps stay
    blocked). `code_call_budget` caps how many times Claude Code is invoked this
    run; when exhausted, remaining steps run local-draft-only. If Code reports a
    rate limit, the runner stops escalating for the rest of the run (backs off to
    local-only) rather than hanging or wasting calls on failures.

    `max_seconds` is a wall-clock ceiling (default from BUILD_RUN_MAX_SECONDS,
    ~12h) so an overnight run stops gracefully instead of running unbounded."""
    import time as _time
    plan = load_plan(plan_id)
    if not plan:
        return {"ok": False, "error": "no such plan"}
    if not plan["approved"]:
        return {"ok": False, "error": "plan not approved"}

    # Recover any steps left 'running' by a previous crashed run before starting.
    recovered = recover_stale_steps(plan_id)

    if max_seconds is None:
        max_seconds = get_settings().build_run_max_seconds
    started_wall = _time.monotonic()

    if code_call_budget is None:
        code_call_budget = get_settings().build_code_call_budget
    budget_left = code_call_budget
    rate_limited_off = False
    hit_time_cap = False

    ran, code_calls = 0, 0
    while ran < max_steps:
        if max_seconds is not None and (_time.monotonic() - started_wall) >= max_seconds:
            hit_time_cap = True
            break
        plan = load_plan(plan_id)
        ready = _ready_steps(plan)
        if not ready:
            break
        effective_budget = 0 if rate_limited_off else budget_left
        out = run_step(plan_id, ready[0]["id"], post_to_bus=post_to_bus,
                       code_budget_left=effective_budget, allow_shell=allow_shell,
                       where=where, allow_api_key=allow_api_key)
        ran += 1
        if out.get("code_used"):
            budget_left = max(0, budget_left - 1)
            code_calls += 1
        if out.get("rate_limited"):
            rate_limited_off = True  # back off Code for the rest of the run

    plan = load_plan(plan_id)
    done = sum(1 for s in plan["steps"] if s["status"] == "done")
    blocked = [s["id"] for s in plan["steps"] if s["status"] == "blocked"]
    summary = {"ok": True, "plan": plan["name"], "done": done,
               "total": len(plan["steps"]), "blocked": blocked, "ran": ran,
               "code_calls": code_calls, "rate_limited": rate_limited_off,
               "recovered": recovered, "hit_time_cap": hit_time_cap,
               "elapsed_s": round(_time.monotonic() - started_wall, 1)}
    if post_to_bus:
        from .bus import send_message
        body = (f"Overnight build '{plan['name']}': {done}/{len(plan['steps'])} steps done, "
                f"{code_calls} Claude Code calls used"
                + (f", recovered {recovered} stale step(s)" if recovered else "")
                + (" (hit rate limit, backed off to local-only)" if rate_limited_off else "")
                + (" (stopped at the time cap; re-run to continue)" if hit_time_cap else "")
                + (f". BLOCKED: {', '.join(blocked)} (see step logs)." if blocked else ". All green."))
        send_message(sender="builder", to=get_settings().owner_id, type="brief",
                     subject=f"Build complete: {plan['name']}", body=body)
    return summary


_PLANNER_SYSTEM = (
    "You are planning an overnight code prototyping build. Given a project "
    "description, output ONLY a JSON array of build steps, no prose. Each step: "
    '{"id": "NN-short-slug", "title": "...", "task": "detailed instruction for a '
    'coding agent", "verify": {"type": "...", ...}, "depends_on": ["earlier-id", '
    '...], "retries": 1}. '
    "The `verify` object declares how to check success, chosen from this FIXED set "
    "(you do NOT write commands; you pick a verifier and its arguments):\n"
    '  {"type": "file_exists", "path": "src/app.py"}\n'
    '  {"type": "dir_exists", "path": "tests"}\n'
    '  {"type": "file_contains", "path": "README.md", "text": "Usage"}\n'
    '  {"type": "pytest", "target": "tests/test_x.py"}   (target optional = run all)\n'
    '  {"type": "ruff", "path": "."}                       (path optional)\n'
    '  {"type": "python_imports", "module": "myapp.core"}\n'
    '  {"type": "always_pass"}                             (only if truly unverifiable)\n'
    "All paths are repo-relative and must stay inside the repo (no .. or absolute "
    "paths). Rules: 8-15 steps; ids zero-padded and ordered; the FIRST step "
    "scaffolds the project (pyproject/package layout, pytest) and later steps "
    "depend on it; every step must have a concrete verify. Order so each step "
    "builds on verified prior steps; keep steps small enough for one focused "
    "coding session."
)


def plan_project(description: str) -> list[dict[str, Any]]:
    """Use the LOCAL model to draft a step DAG from a free-text description. This
    is the autonomous planner: no human writes the steps. Returns [] on failure so
    the caller can decide whether to proceed."""
    from ..llm import ask_ollama, LocalModelError
    try:
        raw = _run_sync(ask_ollama(description, system=_PLANNER_SYSTEM, role="code"))
    except (LocalModelError, Exception):
        return []
    text = raw.strip().strip("`")
    if text.startswith("json"):
        text = text[4:].strip()
    try:
        steps = json.loads(text)
    except Exception:
        return []
    if not isinstance(steps, list) or not steps:
        return []
    for s in steps:
        if isinstance(s, dict):
            s.setdefault("depends_on", [])
            s.setdefault("retries", 1)
    # Use the SAME validator as the manual path, so a model-generated plan is held
    # to identical rules (id regex, verify-spec validity, dependency graph, no
    # duplicates). A malformed plan is dropped rather than half-trusted.
    try:
        validate_steps(steps)
    except ValueError:
        return []
    return steps


def create_and_run(description: str, *, name: str | None = None,
                   auto_approve: bool = False, post_to_bus: bool = True,
                   code_call_budget: int | None = None,
                   allow_shell: bool = False,
                   max_seconds: int | None = None) -> dict[str, Any]:
    """Fully-unattended path: local model plans the project, then (if auto_approve)
    the runner builds it overnight without a human gate. Returns the run summary or
    the created plan awaiting approval.

    Safety: with auto_approve and without allow_shell, a plan containing any
    shell-shaped check is REFUSED, because under --auto-approve no human sees the
    model-generated checks before they would run."""
    steps = plan_project(description)
    if not steps:
        return {"ok": False, "error": "local planner could not produce a valid plan"}
    if auto_approve and not allow_shell:
        # A step with a named `verify` spec is always safe (no command runs). Only
        # legacy `check` strings need the shell-shape gate under auto-approve.
        unsafe = [s["id"] for s in steps
                  if not s.get("verify") and not _is_safe_structured_check(s.get("check", ""))]
        if unsafe:
            return {"ok": False,
                    "error": "auto-approve refused: plan has non-structured (shell-shaped) "
                             "checks in steps " + ", ".join(unsafe) + ". Review with "
                             "build-show + build-approve, or pass allow_shell to accept the risk."}
    plan = create_plan(name or description[:40], steps)
    if not auto_approve:
        return {"ok": True, "planned": True, "plan_id": plan["plan_id"],
                "steps": len(steps), "awaiting_approval": True}
    approve_plan(plan["plan_id"])
    return run_plan(plan["plan_id"], post_to_bus=post_to_bus,
                    code_call_budget=code_call_budget, allow_shell=allow_shell,
                    max_seconds=max_seconds)
