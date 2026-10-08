"""Remote build node: run Claude Code builds on the HA Pi over SSH.

The Mac mini is Mongoose's brain, but the Pi is always-on and its Claude Code draws on the Claude
subscription (the Mac's would meter under the headless billing split). So the Pi is the natural place
to OFFLOAD builds: dispatch a `claude -p` build to the Pi over the existing SSH transport, let it run
(overnight, agentically) without tying up the Mac, then pull the result back for the Mac to review and
commit.

Design choices, all deliberate:
  - Reuses the SAME transport as everything else (_ssh_base_cmd / _run_remote / push_file_to_peer):
    one auth path (key or keyless-password), one place that knows how to reach the Pi.
  - Mirrors the LOCAL builder's contract exactly: invoke_remote_build(...) returns
    (ok, output, rate_limited) just like builder._invoke_claude_code, so the planner/runner can target
    Mac or Pi through one code path.
  - Billing-honest: readiness refuses to silently run if an ANTHROPIC_API_KEY is set on the Pi (that
    bills at API rates instead of the subscription) unless the caller explicitly allows it.
  - Never raises for an expected failure (not configured, Pi down, claude missing, timeout, rate
    limit). Every path returns a structured result.

This module does NOT decide WHEN to build remotely: that's the runner's job, informed by
claude_code.recommend_route. It owns the HOW of remote execution.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from ..settings import get_settings
from .protocol_v3 import _run_remote, push_file_to_peer, acquire_lock, release_lock


def _q(s: str) -> str:
    """Shell-quote for a remote command."""
    return shlex.quote(str(s))


def remote_build_dir() -> str:
    """The Pi-side directory where build repos are staged (PI_BUILD_DIR, default ~/mongoose-builds).
    Expanded on the Pi by the remote shell, so a leading ~ is intentional and fine."""
    s = get_settings()
    return (getattr(s, "pi_build_dir", "~/mongoose-builds") or "~/mongoose-builds").rstrip("/")


def _config_dir() -> str:
    s = get_settings()
    return (getattr(s, "pi_config_dir", "/config") or "/config").rstrip("/")


def touches_shared_config(remote_repo: str) -> bool:
    """Does this build run inside the shared HA config tree (/config), i.e. Chat's and Code's
    territory governed by Protocol v3? Builds in Mongoose's OWN namespace (~/mongoose-builds) touch
    nothing shared and need no lock; a build pointed at /config (or /config/<area>) MUST go through the
    v3 lock + approval path so it can't collide with the other agents."""
    cfg = _config_dir()
    rr = (remote_repo or "").rstrip("/")
    # Normalise a leading ~: we only treat an absolute /config path as shared.
    return rr == cfg or rr.startswith(cfg + "/")


def _v3_lock_target(remote_repo: str) -> str:
    """The lock name to take for a /config build. We lock the top-level area under /config (e.g.
    'packages', 'dashboards') so two agents don't build the same area at once, matching the ownership
    table. Falls back to locking 'config' itself for a whole-tree build."""
    cfg = _config_dir()
    rr = (remote_repo or "").rstrip("/")
    if rr == cfg:
        return "config"
    rest = rr[len(cfg) + 1:]  # strip "<cfg>/"
    top = rest.split("/", 1)[0] if rest else "config"
    return top or "config"


def readiness(*, allow_api_key: bool = False) -> dict[str, Any]:
    """Can we run a build on the Pi right now? Pure read, changes nothing. Checks, in order:
      1. Pi SSH transport configured + reachable,
      2. Claude Code present on the Pi,
      3. billing posture (no ANTHROPIC_API_KEY on the Pi = uses the subscription),
      4. the build dir is creatable.
    Returns {ok, ready, reasons[], detail, pi}: ok=False means a hard stop, ready=False with ok=True
    means a soft/billing caveat the caller may override with allow_api_key."""
    from .claude_code import _pi_claude_code
    pi = _pi_claude_code()
    reasons: list[str] = []
    if not pi.get("reachable"):
        return {"ok": False, "ready": False, "pi": pi,
                "reasons": [pi.get("detail", "Pi not reachable over SSH.")],
                "detail": "The Pi isn't reachable over SSH. Set up Pi SSH first (Protocol v3 / Setup)."}
    if not pi.get("present"):
        return {"ok": False, "ready": False, "pi": pi,
                "reasons": ["Claude Code is not installed on the Pi."],
                "detail": "Claude Code isn't on the Pi. Install it there (a Claude subscription "
                          "includes the CLI) so the Pi can build."}
    # Billing caveat: an API key on the Pi means metered API-rate billing, not the subscription.
    if pi.get("api_key_in_env") and not allow_api_key:
        reasons.append("ANTHROPIC_API_KEY is set on the Pi: a build there would bill at API rates, "
                       "not your subscription. Unset it on the Pi, or pass allow_api_key=True to "
                       "proceed knowingly.")
        return {"ok": True, "ready": False, "pi": pi, "reasons": reasons,
                "detail": reasons[0]}
    # Make sure the build dir can exist (cheap mkdir -p; harmless if already there).
    bd = remote_build_dir()
    mk = _run_remote(f"mkdir -p {_q(bd)} && echo __ok__", timeout=12)
    if not (mk.ok and "__ok__" in mk.stdout):
        return {"ok": False, "ready": False, "pi": pi,
                "reasons": [f"Couldn't create the build dir {bd} on the Pi: {mk.detail or mk.stderr[:160]}"],
                "detail": f"Couldn't prepare {bd} on the Pi."}
    return {"ok": True, "ready": True, "pi": pi, "reasons": [],
            "detail": f"Pi ready to build in {bd} (Claude Code present, "
                      + ("subscription billing)." if not pi.get("api_key_in_env") else "API-rate billing: you allowed it).")}


def _rate_limited(text: str) -> bool:
    """Same rate-limit detection the local builder uses, so behaviour is consistent across Mac/Pi."""
    low = (text or "").lower()
    return ("rate limit" in low or "rate_limit" in low or "usage limit" in low or "quota" in low)


def sync_repo_to_pi(local_repo: str | Path, *, remote_subdir: str | None = None) -> dict[str, Any]:
    """Copy a local build repo to the Pi's build dir so Claude Code can work on it there. Uses the
    verified scp transport (push_file_to_peer) file by file. Returns {ok, remote_repo, files, error?}.

    Scope note: this walks the repo and pushes regular files, skipping .git and common heavy/noise dirs.
    It is meant for the modest build repos Mongoose generates, not arbitrarily large trees."""
    repo = Path(local_repo).expanduser()
    if not repo.exists() or not repo.is_dir():
        return {"ok": False, "error": f"local repo not found: {repo}"}
    bd = remote_build_dir()
    sub = remote_subdir or repo.name
    remote_repo = f"{bd}/{sub}"
    mk = _run_remote(f"mkdir -p {_q(remote_repo)} && echo __ok__", timeout=12)
    if not (mk.ok and "__ok__" in mk.stdout):
        return {"ok": False, "error": f"couldn't create remote repo dir {remote_repo}: {mk.detail or mk.stderr[:160]}"}
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache", "data", "secrets"}
    pushed: list[str] = []
    errors: list[str] = []
    for path in sorted(repo.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(repo)
        if any(part in skip_dirs for part in rel.parts):
            continue
        remote_path = f"{remote_repo}/{rel.as_posix()}"
        # ensure the parent dir exists on the Pi (rglob may give nested files)
        parent = remote_path.rsplit("/", 1)[0]
        _run_remote(f"mkdir -p {_q(parent)}", timeout=12)
        r = push_file_to_peer(str(path), remote_path)
        if r.get("ok") or r.get("unchanged"):
            pushed.append(rel.as_posix())
        else:
            errors.append(f"{rel.as_posix()}: {r.get('error', 'failed')}")
    if errors:
        return {"ok": False, "remote_repo": remote_repo, "files": pushed,
                "error": f"{len(errors)} file(s) failed to sync: " + "; ".join(errors[:5])}
    return {"ok": True, "remote_repo": remote_repo, "files": pushed}


def invoke_remote_build(task: str, remote_repo: str, *, allow_api_key: bool = False,
                        timeout: int | None = None, respect_v3: bool = True) -> tuple[bool, str, bool]:
    """Run `claude -p <task>` on the Pi inside remote_repo, headless. Mirrors builder._invoke_claude_code
    so the runner can target Mac or Pi through one (ok, output, rate_limited) contract.

    Headless flags match the local invocation: --permission-mode acceptEdits (dontAsk denied every
    write headlessly so the agent made zero edits and still exited 0), stream-json + verbose output.
    The whole thing runs in a single SSH command:
    `cd <repo> && claude -p '<task>' ...`. Honours build_step_timeout_s (overridable).

    PROTOCOL v3: if the build runs inside the shared HA config tree (/config), it is Chat's/Code's
    territory. When respect_v3 is True (default), we take the v3 lock for that area FIRST (atomic
    noclobber create, TTL, release-only-own), refuse if another live agent holds it, and release after.
    Builds in Mongoose's own ~/mongoose-builds namespace touch nothing shared and skip locking."""
    s = get_settings()
    cmd = (getattr(s, "pi_claude_code_cmd", "claude") or "claude").strip()
    if not cmd:
        return False, "pi_claude_code_cmd not configured", False
    # Cheap pre-flight: the SSH transport must be usable. readiness() does the full check; here we just
    # avoid running a long build command when the Pi is plainly unconfigured/unreachable.
    rd = readiness(allow_api_key=allow_api_key)
    if not rd.get("ok"):
        return False, rd.get("detail", "Pi not ready to build."), False
    if not rd.get("ready"):
        # soft stop (billing) the caller didn't override
        return False, rd.get("detail", "Pi build not allowed (billing caveat)."), False

    # Protocol v3 coordination: a build in shared /config territory must hold the lock for that area so
    # it can't collide with Chat or Code. Mongoose's own namespace needs no lock.
    shared = respect_v3 and touches_shared_config(remote_repo)
    lock_target = None
    if shared:
        from .protocol_v3 import acquire_lock
        lock_target = _v3_lock_target(remote_repo)
        # TTL covers the whole build plus a little headroom so a long build doesn't go stale mid-run.
        lock_ttl = int(timeout or getattr(s, "build_step_timeout_s", 1800) or 1800) + 120
        lk = acquire_lock(lock_target, ttl_seconds=lock_ttl)
        if not lk.get("acquired"):
            holder = lk.get("holder_sid") or lk.get("error") or "another agent"
            return (False,
                    f"Protocol v3: '{lock_target}' under /config is locked by {holder}; not building "
                    f"there to avoid colliding with Chat/Code. Retry once the lock clears.", False)

    try:
        step_timeout = int(timeout or getattr(s, "build_step_timeout_s", 1800) or 1800)
        # Build the remote command. shlex.quote the task so quotes/newlines in the prompt can't break
        # the remote shell or inject anything. claude reads -p as one argument.
        remote_cmd = (
            f"cd {_q(remote_repo)} && "
            f"{shlex.quote(cmd)} -p {_q(task)} "
            f"--permission-mode acceptEdits --output-format stream-json --verbose"
        )
        # Give the SSH call a little headroom beyond the build timeout so we capture claude's own
        # timeout message rather than ssh being killed first.
        res = _run_remote(remote_cmd, timeout=step_timeout + 30)
        out = (res.stdout or "") + (res.stderr or "")
        if not res.configured:
            return False, "Pi SSH transport not configured.", False
        rl = _rate_limited(out)
        # res.ok reflects ssh exit 0; a non-zero claude exit (rate limit, error) comes back ok=False
        # with the reason in out. Mirror the local builder: success is exit 0.
        return res.ok, out[-4000:], rl
    finally:
        if lock_target:
            from .protocol_v3 import release_lock
            release_lock(lock_target)  # release-only-own; safe even if we never acquired


def run_remote_build(task: str, local_repo: str | Path, *, allow_api_key: bool = False,
                     pull_back: bool = True, timeout: int | None = None) -> dict[str, Any]:
    """End-to-end convenience: sync a local repo to the Pi, run the build there, and (optionally) note
    where the result is so the Mac can pull + review + commit. Returns a structured result; never raises.

    pull_back currently records the remote path for the caller to fetch (the Mac-side review/commit step
    owns the actual pull, using the same scp transport in reverse). This keeps the build node focused on
    EXECUTION while the Mac stays the place that reviews and commits: exactly the split you wanted."""
    rd = readiness(allow_api_key=allow_api_key)
    if not rd.get("ok") or not rd.get("ready"):
        return {"ok": False, "stage": "readiness", "detail": rd.get("detail"), "readiness": rd}
    synced = sync_repo_to_pi(local_repo)
    if not synced.get("ok"):
        return {"ok": False, "stage": "sync", "detail": synced.get("error"), "sync": synced}
    remote_repo = synced["remote_repo"]
    ok, output, rate_limited = invoke_remote_build(task, remote_repo,
                                                   allow_api_key=allow_api_key, timeout=timeout)
    result = {
        "ok": ok,
        "stage": "build",
        "remote_repo": remote_repo,
        "rate_limited": rate_limited,
        "output_tail": output,
        "files_synced": len(synced.get("files", [])),
        "pull_back": bool(pull_back),
        "review_hint": (f"Build ran on the Pi in {remote_repo}. Pull it back to the Mac to review and "
                        f"commit (the Mac stays the place that reviews/commits)." if ok else
                        "Build did not succeed on the Pi; see output_tail for why."),
    }
    if rate_limited:
        result["detail"] = ("The Pi's Claude Code hit a rate/usage limit. Stop escalating and retry "
                            "later, or run the step on the Mac.")
    return result


def _bus_announce(topic: str, body: str) -> None:
    """Best-effort post to the v3 bus. Never raises; if v3 isn't enabled or the Pi can't be written,
    we just skip the announcement (the build itself doesn't depend on it)."""
    try:
        from .protocol_v3 import build_envelope, _append_to_bus
        env = build_envelope(topic=topic, body=body, to="all", needs_ack=False)
        _append_to_bus(env)
    except Exception:
        pass


def coordinated_remote_build(task: str, local_repo: str | Path, *, lock_target: str = "mongoose-build",
                             allow_api_key: bool = False, pull_back: bool = True,
                             timeout: int | None = None, ttl_seconds: int = 3600) -> dict[str, Any]:
    """Run a remote Pi build UNDER Protocol v3 coordination so it is safe to run while Chat/Code Claude
    are also working on the Pi. Sequence:
      1. take a v3 lock on lock_target (default 'mongoose-build'). If a LIVE peer holds it, BACK OFF and
         do not build (no collision with Code Claude's work). A stale lock is reclaimed per protocol.
      2. announce on the bus that mongoose is building (visibility + audit for the other agents).
      3. sync + run the build on the Pi.
      4. post a completion message to the bus and RELEASE the lock (release-only-your-own).

    This is the v3-aware entry point. invoke_remote_build / run_remote_build remain for callers that
    manage coordination themselves (or run when the Pi is exclusively mongoose's). Never raises."""
    # 1. Lock first: this is what stops two agents writing the same Pi files at once.
    lock = acquire_lock(lock_target, ttl_seconds=ttl_seconds)
    if not lock.get("ok"):
        if lock.get("error") == "held":
            return {"ok": False, "stage": "lock", "coordinated": True,
                    "detail": f"Another agent holds the '{lock_target}' lock right now "
                              f"(sid {lock.get('holder_sid')}). Backing off so the build doesn't "
                              f"collide with their work; try again when they're done.",
                    "lock": lock}
        if lock.get("error") == "not_configured":
            return {"ok": False, "stage": "lock", "coordinated": True,
                    "detail": "Pi SSH transport not configured; can't take the build lock.", "lock": lock}
        return {"ok": False, "stage": "lock", "coordinated": True,
                "detail": f"Couldn't take the build lock: {lock.get('detail', lock.get('error'))}",
                "lock": lock}
    # If we reclaimed a stale lock, the protocol requires we say so on the bus.
    if lock.get("reclaimed"):
        _bus_announce("lock-reclaim",
                      f"mongoose reclaimed a STALE '{lock_target}' lock (previous sid "
                      f"{lock.get('previous_sid')}) to start a build.")
    try:
        _bus_announce("mongoose-build-start",
                      f"mongoose is starting a build on the Pi (target '{lock_target}'). "
                      f"Task: {task[:160]}")
        # 3. The actual build (sync + run), reusing the uncoordinated path now that we hold the lock.
        result = run_remote_build(task, local_repo, allow_api_key=allow_api_key,
                                  pull_back=pull_back, timeout=timeout)
        result["coordinated"] = True
        result["lock_target"] = lock_target
        # 4. Completion announcement.
        if result.get("ok"):
            _bus_announce("mongoose-build-done",
                          f"mongoose finished a build on the Pi (target '{lock_target}'): success. "
                          f"The Mac will review and commit.")
        else:
            _bus_announce("mongoose-build-failed",
                          f"mongoose's Pi build (target '{lock_target}') did not succeed at stage "
                          f"'{result.get('stage')}'.")
        return result
    finally:
        # ALWAYS release our own lock, success or failure.
        release_lock(lock_target)


def _bus_announce_placeholder() -> None:  # pragma: no cover
    return None


def status_report() -> dict[str, Any]:
    """One-call summary for the cockpit/CLI: is the Pi a usable build node right now, and how would a
    build there be billed? Pure read."""
    rd = readiness()
    return {
        "build_dir": remote_build_dir(),
        "ready": rd.get("ready", False),
        "ok": rd.get("ok", False),
        "detail": rd.get("detail"),
        "reasons": rd.get("reasons", []),
        "pi": rd.get("pi", {}),
    }
