"""Chat-driven self-hardening: "Mongoose, try to harden your QR code logic".

The owner states a GOAL in chat; Mongoose builds a candidate patch to ITS OWN source in the
background, has independent peers review it, and only then surfaces it, as a staged update
candidate in the EXISTING approvals flow. Nothing here ever touches the live tree: the
staging pipeline (stage_update_candidate + check + owner approve + rollback) is the only
door, and the owner's approval is the only key.

The loop:  goal -> workspace copy -> Claude Code codegen -> gates (compile, pyflakes,
targeted tests) -> peer reviews (N independent verdicts, majority required) -> zip ->
stage_update_candidate -> owner notification. Every step lands in the job record, so chat
can answer "how is that patch going?" from receipts, not vibes.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from ..db import get_app_state, set_app_state

_JOBS_KEY = "self_patch:jobs"
REQUIRED_APPROVALS = 2
_GOAL_MAX = 500


def _now() -> float:
    return time.time()


def _load_jobs() -> dict[str, Any]:
    try:
        raw = get_app_state(_JOBS_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_jobs(jobs: dict[str, Any]) -> None:
    set_app_state(_JOBS_KEY, json.dumps(jobs))


def _update(job_id: str, **fields: Any) -> dict[str, Any]:
    jobs = _load_jobs()
    job = jobs.get(job_id, {})
    job.update(fields, updated_at=_now())
    jobs[job_id] = job
    _save_jobs(jobs)
    return job


def workspace_root() -> Path:
    base = (os.getenv("MONGOOSE_DATA_DIR") or "").strip() or os.path.expanduser("~/.mongoose/data")
    return Path(base) / "self_patch"


def list_jobs() -> list[dict[str, Any]]:
    # Reap here too, not only in request_self_patch: otherwise "how is it going" keeps
    # reporting "building" for a job that died hours ago, until someone happens to start a
    # new self-patch. Status should always reflect reality, not just the moment before a
    # fresh request forces a reap.
    _reap_stale_jobs()
    jobs = _load_jobs()
    return sorted(jobs.values(), key=lambda j: -j.get("created_at", 0))[:20]


def get_job(job_id: str) -> dict[str, Any] | None:
    _reap_stale_jobs()
    return _load_jobs().get(job_id)


_ACTIVE_STATUSES = {"queued", "building", "validating", "peer_review"}
# Worst-case legitimate duration, computed from the real per-stage timeouts: codegen 1800s +
# gates (3 commands x up to 600s each) 1800s + two peer reviews x 900s each 1800s = 5400s
# (90 minutes). The first version of this ceiling was 7200s, a 30-minute margin over that
# worst case, tighter than intended for a value with zero cost to being generous: nobody is
# waiting live on this reaper, it only exists to stop a genuinely dead job from permanently
# blocking future requests. Widened to 4 hours for real headroom on a loaded machine.
_STALE_JOB_S = 3600 * 4


def _reap_stale_jobs() -> None:
    now = _now()
    jobs = _load_jobs()
    changed = False
    for job in jobs.values():
        if (job.get("status") in _ACTIVE_STATUSES
                and now - job.get("updated_at", job.get("created_at", now)) > _STALE_JOB_S):
            job["status"] = "abandoned"
            job["error"] = "no progress for over 2 hours; likely orphaned by a restart"
            job["updated_at"] = now
            changed = True
            # The process that owned this job died before its own finally block could run,
            # so its workspace was never cleaned up. Reap it here too, or an orphaned job
            # leaves a permanent copy on disk exactly like the un-reaped case did.
            jid = job.get("id")
            if jid:
                shutil.rmtree(workspace_root() / jid, ignore_errors=True)
    if changed:
        _save_jobs(jobs)


def request_self_patch(goal: str, requested_by: str = "owner") -> dict[str, Any]:
    """Create the job record and return immediately; the caller starts the worker. Refuses
    goals that are empty, oversized, or from anyone but the owner surface. Also refuses a
    second job while one is genuinely active: codegen plus the full gate suite is expensive
    enough on a Mac mini that two overlapping runs is a resource-exhaustion nuisance, not a
    correctness risk, but there's no reason to allow it either. Stale jobs (orphaned by a
    crash or restart) are reaped first so they can never cause a permanent lockout."""
    goal = (goal or "").strip()
    if requested_by != "owner":
        return {"ok": False, "error": "self-patching is owner-only"}
    if not goal or len(goal) > _GOAL_MAX:
        return {"ok": False, "error": f"goal must be 1..{_GOAL_MAX} characters"}
    _reap_stale_jobs()
    active = [j for j in _load_jobs().values() if j.get("status") in _ACTIVE_STATUSES]
    if active:
        return {"ok": False, "error": "a self-patch is already in progress",
                "active_job_id": active[0].get("id"), "active_goal": active[0].get("goal"),
                "message": (f"Already working on '{active[0].get('goal')}'. Ask me its "
                            "status, or wait for it to finish or fail before starting another.")}
    job_id = uuid.uuid4().hex[:10]
    _update(job_id, id=job_id, goal=goal, status="queued", created_at=_now(),
            steps=[], reviews=[], stage_id=None, error=None)
    return {"ok": True, "job_id": job_id,
            "message": ("On it. I'll build a candidate in the background, have it peer-"
                        "reviewed, and put it in Approvals when it survives. Ask me "
                        "'how is the self-patch going?' any time.")}


def _step(job_id: str, name: str, ok: bool, detail: str = "") -> None:
    jobs = _load_jobs()
    job = jobs.get(job_id, {})
    job.setdefault("steps", []).append({"name": name, "ok": ok, "detail": detail[:500],
                                        "at": _now()})
    jobs[job_id] = job
    _save_jobs(jobs)


def _invoke_codegen(tree: Path, goal: str) -> dict[str, Any]:
    """Run Claude Code against the workspace copy. Mockable seam, same shape as
    plugin_author's invoker. Guardrails ride in the prompt AND in what we expose: the
    workspace contains only src/ and tests/, no .env, no data, no secrets."""
    prompt = (
        f"You are hardening the Mongoose codebase. Goal: {goal}\n"
        "Rules: modify only files under src/ and tests/. Keep every existing test passing. "
        "Add or extend tests that pin the hardening. No new dependencies. No em dashes in "
        "any output. Make the smallest change that genuinely achieves the goal."
    )
    # Locate Claude Code via the ONE shared resolver (builder._resolve_claude_cmd). A bare
    # shutil.which("claude") returns None under the launchd server PATH (which excludes ~/.local/bin),
    # which used to fail self-patch with a confusing "not found" , the same bug already fixed for the
    # build runner. Route through the shared resolver so every local invoker finds the binary the same way.
    from .builder import _resolve_claude_cmd
    from ..settings import get_settings
    exe = _resolve_claude_cmd(get_settings().claude_code_cmd or "claude")
    if not exe:
        return {"ok": False, "error": "Claude Code CLI not found on PATH or in the usual install "
                "locations (~/.local/bin, /opt/homebrew/bin, /usr/local/bin)"}
    try:
        # acceptEdits, NOT dontAsk: dontAsk denies every write headlessly (the agent could only read
        # and made zero edits while still exiting 0). The author writes to an ISOLATED copy tree here,
        # and _run_gates + _peer_review verify before anything is applied, so auto-approving edits is safe.
        r = subprocess.run([exe, "-p", prompt, "--permission-mode", "acceptEdits"],
                           cwd=str(tree), capture_output=True, text=True, timeout=1800)
        return {"ok": r.returncode == 0, "error": (r.stderr or "")[-400:] if r.returncode else None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_gates(tree: Path) -> dict[str, Any]:
    """The non-negotiables: everything compiles, pyflakes is silent, hygiene tests pass."""
    import sys
    env = dict(os.environ, PYTHONPATH=str(tree / "src"))
    for name, cmd in [
        ("compileall", [sys.executable, "-m", "compileall", "-q", "src/mongoose"]),
        ("pyflakes", [sys.executable, "-m", "pyflakes", "src"]),
        ("hygiene_tests", [sys.executable, "-m", "pytest", "-q", "-x", "--timeout=60",
                           "tests/test_release_hygiene.py", "tests/test_version_consistency.py",
                           "tests/test_dependency_hygiene.py"]),
    ]:
        try:
            r = subprocess.run(cmd, cwd=str(tree), env=env, capture_output=True,
                               text=True, timeout=600)
            if r.returncode != 0:
                return {"ok": False, "gate": name,
                        "detail": (r.stdout + r.stderr)[-400:]}
        except Exception as exc:
            return {"ok": False, "gate": name, "detail": str(exc)}
    return {"ok": True}


def _peer_review(tree: Path, goal: str, reviewer: int) -> dict[str, Any]:
    """One independent reviewer verdict. Mockable seam. Default implementation asks Claude
    Code to REVIEW (read-only prompt); a local Ollama reviewer can be slotted in later."""
    from .builder import _resolve_claude_cmd
    from ..settings import get_settings
    exe = _resolve_claude_cmd(get_settings().claude_code_cmd or "claude")
    if not exe:
        return {"approve": False, "reviewer": reviewer,
                "reason": "no reviewer available (Claude Code CLI not found on PATH or usual locations)"}
    prompt = (
        f"Review the uncommitted changes in this tree against the goal: {goal}\n"
        "You are reviewer number " + str(reviewer) + ", independent of the author. "
        "Answer with a single line starting APPROVE or REJECT, then one sentence why. "
        "Reject anything that weakens security, deletes tests, or exceeds the goal's scope."
    )
    try:
        r = subprocess.run([exe, "-p", prompt, "--permission-mode", "plan"],
                           cwd=str(tree), capture_output=True, text=True, timeout=900)
        out = (r.stdout or "").strip()
        return {"approve": out.upper().startswith("APPROVE"), "reviewer": reviewer,
                "reason": out[:300]}
    except Exception as exc:
        return {"approve": False, "reviewer": reviewer, "reason": str(exc)}


async def run_job(job_id: str) -> dict[str, Any]:
    """The whole loop, off the chat turn's critical path. Every blocking stage runs in a
    thread so the event loop stays live for chat. The workspace (a full src+tests copy) is
    always removed once the run ends, success or failure: stage_update_candidate extracts
    everything it needs from the zip DURING staging and never touches it again afterward, and
    the job's own steps/reviews/error already carry everything worth keeping. Without this,
    every attempt (including every rejected or failed one) left a permanent copy on disk."""
    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "unknown job"}
    goal = job["goal"]
    _update(job_id, status="building")
    ws = workspace_root() / job_id
    tree = ws / "tree"
    try:
        # 1) Isolated copy: src + tests + packaging files only. Never the live tree itself.
        def _copy() -> None:
            tree.mkdir(parents=True, exist_ok=True)
            for item in ("src", "tests", "pyproject.toml", "requirements.txt",
                         "release.json", "configs", "scripts"):
                s = Path(item)
                if s.is_dir():
                    shutil.copytree(s, tree / item, dirs_exist_ok=True)
                elif s.exists():
                    shutil.copy2(s, tree / item)
        await asyncio.to_thread(_copy)
        _step(job_id, "workspace", True, str(tree))

        # 2) Codegen
        gen = await asyncio.to_thread(_invoke_codegen, tree, goal)
        _step(job_id, "codegen", gen["ok"], gen.get("error") or "")
        if not gen["ok"]:
            _update(job_id, status="failed", error=f"codegen: {gen.get('error')}")
            return {"ok": False, "error": gen.get("error")}

        # 3) Gates
        _update(job_id, status="validating")
        gates = await asyncio.to_thread(_run_gates, tree)
        _step(job_id, "gates", gates["ok"], gates.get("detail", ""))
        if not gates["ok"]:
            _update(job_id, status="failed", error=f"gate {gates.get('gate')}: {gates.get('detail', '')[:200]}")
            return {"ok": False, "error": gates.get("detail")}

        # 4) Peer review: independent verdicts, majority of REQUIRED_APPROVALS must approve.
        _update(job_id, status="peer_review")
        reviews = []
        for i in range(1, REQUIRED_APPROVALS + 1):
            v = await asyncio.to_thread(_peer_review, tree, goal, i)
            reviews.append(v)
            _step(job_id, f"review_{i}", v["approve"], v.get("reason", ""))
        _update(job_id, reviews=reviews)
        approvals = sum(1 for v in reviews if v["approve"])
        if approvals < REQUIRED_APPROVALS:
            _update(job_id, status="rejected_by_peers",
                    error=f"{approvals}/{REQUIRED_APPROVALS} approvals")
            return {"ok": False, "error": "peers rejected the candidate"}

        # 5) Package and stage through the EXISTING approvals pipeline: the only door.
        def _package() -> dict[str, Any]:
            zip_path = ws / f"self-patch-{job_id}.zip"
            shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=str(ws),
                                base_dir="tree")
            from ..updater import stage_update_candidate
            return stage_update_candidate(zip_path)
        staged = await asyncio.to_thread(_package)
        stage_id = staged.get("stage_id") or staged.get("id")
        _step(job_id, "staged", bool(staged.get("ok", stage_id)), str(stage_id))
        _update(job_id, status="awaiting_owner_approval", stage_id=stage_id)

        # 6) Tell the owner, best effort.
        try:
            from . import mailbox as mb
            mb.send_message(sender="mongoose", recipient=mb.OWNER,
                            body=(f"Self-patch ready for approval: '{goal}'. Peers "
                                  f"{approvals}/{REQUIRED_APPROVALS} approved. Review it in "
                                  f"Approvals (stage {stage_id}); nothing is applied until "
                                  f"you approve."))
        except Exception:
            pass
        return {"ok": True, "stage_id": stage_id}
    except Exception as exc:
        _update(job_id, status="failed", error=str(exc))
        return {"ok": False, "error": str(exc)}
    finally:
        await asyncio.to_thread(shutil.rmtree, ws, ignore_errors=True)
