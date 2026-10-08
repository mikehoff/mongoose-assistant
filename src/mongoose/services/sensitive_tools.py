"""Sensitive tools, state-mutating chat actions behind a propose/confirm gate.

The planner NEVER executes these inline (see tools.run_tool_turn): when the model picks a sensitive
tool, the loop returns a PROPOSAL (a plain-language description of what would happen + a short-lived
confirmation token) and stops. The user must explicitly confirm in a follow-up, confirm_sensitive_action
validates the token and only then calls the underlying function.

The underlying function is ALWAYS the same gated one the CLI uses (e.g. apply_update_stage already
requires an approved+checked stage and yes=True). Chat does not get a private, weaker path. This is the
claim-consistency lesson applied to permissions: one definition of "is this allowed", reused.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

from ..db import audit

# Pending proposals: token -> {tool, args, created_at}. In-memory and short-lived; a confirmation must
# arrive within _TTL_SECONDS. One process, one user , no cross-user concern.
_PENDING: dict[str, dict[str, Any]] = {}
_TTL_SECONDS = 600.0


def _human_summary(tool: str, args: dict[str, Any]) -> str:
    if tool == "run_hardening_scan":
        return "Run a self-hardening scan now (records observations from scheduler/watchdog/audit signals and checks claim consistency). Evidence-only, it does not change code or apply anything."
    if tool == "run_due_jobs":
        return "Run any background scheduler jobs that are currently due, once (the same as `mongoose scheduler --once`). Safe: it only runs jobs already scheduled and skips owner-paused jobs."
    if tool == "scheduler_control":
        action = args.get("action") or "status"
        jobs = args.get("jobs") or args.get("job") or args.get("names")
        if str(action).lower().replace(" ", "_") in {"free_memory", "memory", "quiet", "relieve_memory"} and not jobs:
            return "Pause the deferrable/background-heavy scheduler jobs now, to free memory and reduce model/IO contention. Safety/continuity jobs such as alerts, DDNS, backups, v3 reconcile and RTM operational refresh keep running."
        return f"Change scheduler jobs: action={action!r}, jobs={jobs!r}. This is durable across restarts and can be reversed."
    if tool == "wp_plugin_action":
        action = str(args.get("action") or "status").lower()
        if action in {"deploy", "activate", "deactivate", "promote"}:
            from .. import profile as _profile
            return (f"Run WordPress plugin action {action!r} for the {_profile.rtm_name()} RTM site. "
                    "This can change what is installed or active on the live WordPress site, so it needs explicit confirmation.")
        if action in {"build", "scaffold"}:
            return (f"Run WordPress plugin action {action!r}. This mutates local RTM plugin/build files "
                    "but does not touch the live site unless you later deploy it.")
        return f"Run WordPress plugin action {action!r}."
    if tool == "check_update":
        sid = args.get("stage_id") or "(the staged candidate)"
        smoke = " including a runtime smoke-test (boots the candidate on a temporary port to verify it serves correctly)" if args.get("runtime_smoke") else ""
        return f"Run pre-apply checks on staged update {sid} (manifest, compile, lint{smoke}). Applies nothing."
    if tool == "apply_update":
        sid = args.get("stage_id") or "(the staged candidate)"
        return f"APPLY staged update {sid}. This replaces the running version. It only proceeds if the stage is already checked and approved; a backup is taken first."
    if tool == "attempt_bug_fix":
        bid = args.get("bug_id") or "(bug id missing)"
        return (f"Attempt a candidate fix for {bid}: copy the source tree to a bugfix workspace, "
                "optionally ask the configured code agent to edit it, generate a patch, run local verifier checks, "
                "and request peer/v3 review. This does NOT apply live code.")
    if tool == "apply_bug_fix":
        bid = args.get("bug_id") or "(bug id missing)"
        return (f"APPLY the candidate fix for {bid} through Mongoose's staged update system. "
                "This requires at least one recorded peer/verifier OK and still runs the normal update-stage gates.")
    if tool == "data_dump":
        raw = str(args.get("kind") or args.get("text") or "everything").lower()
        if "journal" in raw or "wellness" in raw or "mindful" in raw:
            what = "your private journal/check-in entries"
        elif "bug" in raw:
            what = "your bug reports"
        elif "email" in raw:
            what = "your stored emails"
        else:
            what = "your owner data (emails, tasks, notes, diary, facts, bugs and more)"
        return (f"Create a downloadable export containing {what}, and return a private link that expires "
                "on its own. The export is owner-scoped (it never includes a guest's private content), but "
                "it does package your own private data into a file, so it needs your explicit confirmation.")
    if tool == "recommend_song":
        song = str(args.get("song") or args.get("track") or "that track").strip()
        who = str(args.get("to") or args.get("recipient") or args.get("who") or "someone").strip()
        return (f"Share the song {song!r} with {who} (it lands in their own chat, with a push, and is "
                "recorded). This reaches another person, so it needs your explicit confirmation.")
    if tool == "message_person":
        who = str(args.get("to") or args.get("recipient") or args.get("who") or "someone").strip()
        body = str(args.get("text") or args.get("message") or args.get("body") or "").strip()
        return (f"Send a message to {who}: {body!r}. This delivers to a real person, so it needs your "
                "explicit confirmation.")
    if tool == "whatsapp_draft":
        contact = str(args.get("contact") or "a contact").strip()
        body = str(args.get("body") or "").strip()
        return (f"Compose a WhatsApp DRAFT to {contact}: {body!r}. This only creates a draft for your "
                "approval; it is never sent without a separate send step.")
    return f"Run {tool} with {args}."


def propose_sensitive_action(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return a proposal the chat loop shows the user. Does NOT execute anything."""
    token = secrets.token_urlsafe(8)
    _PENDING[token] = {"tool": tool, "args": dict(args or {}), "created_at": time.time()}
    return {
        "ok": True,
        "needs_confirmation": True,
        "action": tool,
        "summary": _human_summary(tool, args or {}),
        "confirm_token": token,
        "how_to_confirm": "Reply to confirm, then call confirm_sensitive_action with this token. The action does not run until confirmed.",
    }


def _purge_expired() -> None:
    now = time.time()
    for tok in [t for t, p in _PENDING.items() if now - p["created_at"] > _TTL_SECONDS]:
        _PENDING.pop(tok, None)


def confirm_sensitive_action(token: str) -> dict[str, Any]:
    """Validate a confirmation token and run the underlying GATED function. Called only after the user
    has explicitly confirmed. Returns the function's result."""
    _purge_expired()
    pending = _PENDING.pop((token or "").strip(), None)
    if not pending:
        return {"ok": False, "error": "no such pending action (it may have expired); ask again to get a fresh proposal"}
    tool, args = pending["tool"], pending["args"]
    audit("tools", "sensitive_confirmed", True, route=tool, payload={"arg_keys": sorted(args.keys())})
    try:
        return _execute(tool, args)
    except Exception as exc:
        audit("tools", "sensitive_failed", False, route=tool, reason=str(exc)[:160])
        return {"ok": False, "error": f"{tool} failed: {str(exc)[:200]}"}


async def confirm_sensitive_action_async(token: str) -> dict[str, Any]:
    """Async confirmation path for chat/API handlers that are already inside an event loop.

    The previous sync-only path used `asyncio.run()` for `run_due_jobs`; that is fine from a CLI test,
    but crashes when called by the FastAPI chat loop. This keeps the same proposal/token gate while
    executing async underlying jobs without nesting event loops.
    """
    _purge_expired()
    pending = _PENDING.pop((token or "").strip(), None)
    if not pending:
        return {"ok": False, "error": "no such pending action (it may have expired); ask again to get a fresh proposal"}
    tool, args = pending["tool"], pending["args"]
    audit("tools", "sensitive_confirmed", True, route=tool, payload={"arg_keys": sorted(args.keys()), "async": True})
    try:
        return await _execute_async(tool, args)
    except Exception as exc:
        audit("tools", "sensitive_failed", False, route=tool, reason=str(exc)[:160])
        return {"ok": False, "error": f"{tool} failed: {str(exc)[:200]}"}


async def _execute_async(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Async dispatch to the SAME gated functions, for FastAPI/chat contexts."""
    if tool == "run_due_jobs":
        from .scheduler import run_due_jobs
        return {"ok": True, "result": await run_due_jobs()}
    if tool in {"attempt_bug_fix", "apply_bug_fix"}:
        import asyncio
        return await asyncio.to_thread(_execute, tool, args)
    # Outward/social tools: the underlying executors live in tools.py. recommend_song / message_person
    # are async; whatsapp_draft is sync but fast (a local draft). Each is the SAME executor the model
    # tool-loop would have called; the confirm gate only decides WHEN it runs, not HOW.
    if tool == "recommend_song":
        from ..tools import _tool_recommend_song
        return await _tool_recommend_song(args)
    if tool == "message_person":
        from ..tools import _tool_message_person
        return await _tool_message_person(args)
    if tool == "whatsapp_draft":
        from ..tools import _tool_whatsapp_draft
        return _tool_whatsapp_draft(args)
    if tool == "self_patch":
        import asyncio
        from .self_patch import request_self_patch, run_job
        res = request_self_patch(str(args.get("goal") or ""), requested_by="owner")
        if res.get("ok"):
            asyncio.create_task(run_job(res["job_id"]))
        return res
    # ALL remaining sensitive functions are synchronous _execute() calls, and several are NOT quick:
    # apply_update / check_update shell out to git and the updater, run_hardening_scan does a heavy
    # scan, wp_plugin_action runs Claude Code for minutes. Running any of them directly on the event
    # loop freezes the whole server for its duration (the plugin build proved this). Run every one in a
    # worker thread so a confirmed action can never block the loop.
    import asyncio
    return await asyncio.to_thread(_execute, tool, args)


def peek_pending(token: str) -> tuple[str, dict[str, Any]] | None:
    """Look at a pending action WITHOUT consuming it, so a caller can decide to run a long one in the
    background before confirming. Returns (tool, args) or None."""
    p = _PENDING.get((token or "").strip())
    return (p["tool"], dict(p.get("args") or {})) if p else None


def _execute(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch to the SAME gated function the CLI uses. No weaker path for chat."""
    if tool == "run_hardening_scan":
        from . import self_harden as H
        return {"ok": True, "result": H.scan_runtime()}
    if tool == "run_due_jobs":
        import asyncio
        from .scheduler import run_due_jobs
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return {"ok": True, "result": asyncio.run(run_due_jobs())}
        return {"ok": False, "error": "run_due_jobs confirmation is already inside an event loop; use confirm_sensitive_action_async"}
    if tool == "scheduler_control":
        from .scheduler import scheduler_control
        return {"ok": True, "result": scheduler_control(
            str(args.get("action") or "status"),
            args.get("jobs") if "jobs" in args else (args.get("job") or args.get("names")),
            reason=str(args.get("reason") or ""),
            actor=str(args.get("actor") or "owner"),
        )}
    if tool == "check_update":
        from ..updater import check_update_stage
        sid = (args.get("stage_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "no stage_id given"}
        return {"ok": True, "result": check_update_stage(sid, runtime_smoke=bool(args.get("runtime_smoke")))}
    if tool == "apply_update":
        from ..updater import apply_update_stage
        sid = (args.get("stage_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "no stage_id given"}
        # apply_update_stage enforces its own gate (approved + checked) and needs yes=True; we pass it
        # because the user has explicitly confirmed via the token. The function still refuses an
        # unapproved/unchecked stage , chat cannot bypass that.
        rc = apply_update_stage(sid, yes=True)
        return {"ok": rc == 0, "returncode": rc}
    if tool == "wp_plugin_action":
        return _execute_wp_plugin_action(args)
    if tool == "attempt_bug_fix":
        from .bug_workflow import attempt_fix
        return attempt_fix(
            str(args.get("bug_id") or ""),
            run_codegen=bool(args.get("run_codegen", True)),
            request_peers=bool(args.get("request_peers", True)),
        )
    if tool == "apply_bug_fix":
        from .bug_workflow import apply_fix
        return apply_fix(str(args.get("bug_id") or ""))
    if tool == "data_dump":
        return _execute_data_dump(args)
    if tool in {"recommend_song", "message_person"}:
        # Async executors reached from the SYNC confirm path (CLI/tests). The chat/API path always uses
        # _execute_async; this exists so the sync dispatcher is symmetric and usable off the event loop.
        import asyncio
        from ..tools import _tool_message_person, _tool_recommend_song
        coro = _tool_recommend_song(args) if tool == "recommend_song" else _tool_message_person(args)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        coro.close()
        return {"ok": False, "error": f"{tool} confirmation is already inside an event loop; use confirm_sensitive_action_async"}
    if tool == "whatsapp_draft":
        from ..tools import _tool_whatsapp_draft
        return _tool_whatsapp_draft(args)
    if tool == "self_patch":
        # Owner-confirmed self-hardening: create the job, then run its worker in the
        # background. This sync path has no running event loop, so it cannot simply
        # create_task like the async dispatcher does; the same-thread alternative,
        # asyncio.run(run_job(...)), would BLOCK THIS CALLING THREAD for up to ~90 minutes
        # (codegen 1800s + gates up to 1800s + two 900s peer reviews). Nothing in the live
        # app currently calls this sync path (chat always uses the async dispatcher below),
        # but it is tested public API and the smoke suite asserts both dispatchers are
        # symmetric, so it must behave correctly if something starts calling it. Fixed the
        # same way as the embeddings startup fetch and the tutorial's IMAP sync earlier this
        # session: a daemon thread running its own private event loop, so the caller returns
        # immediately either way.
        import threading
        from .self_patch import request_self_patch, run_job
        res = request_self_patch(str(args.get("goal") or ""), requested_by="owner")
        if not res.get("ok"):
            return res
        job_id = res["job_id"]

        def _run_in_background() -> None:
            import asyncio as _a
            _a.run(run_job(job_id))
        threading.Thread(target=_run_in_background, daemon=True).start()
        return {"ok": True, "job_id": job_id,
                "message": "Self-patch started in the background; review it in Approvals once it stages."}
    return {"ok": False, "error": f"unknown sensitive tool {tool!r}"}


def _execute_data_dump(args: dict[str, Any]) -> dict[str, Any]:
    """Confirmed owner data export. Parses the requested kind the same way the chat tool does, then
    calls the SAME create_dump the UI/CLI use. Lives behind the proposal/confirm gate so chat cannot
    package private data merely because a model picked the tool."""
    from .data_dump import create_dump, available_kinds
    raw = str(args.get("kind") or args.get("text") or "").lower()
    kind = "everything"
    if "journal" in raw or "wellness" in raw or "mindful" in raw:
        kind = "journal"
    elif "bug" in raw:
        kind = "bugs"
    else:
        for k in available_kinds():
            if k != "everything" and k in raw:
                kind = k
                break
    return create_dump(kind)


def _execute_wp_plugin_action(args: dict[str, Any]) -> dict[str, Any]:
    """Confirmed Example Court WordPress/plugin mutations.

    This mirrors the public wp_plugin tool executor, but lives behind the sensitive proposal/confirm
    dispatcher so chat cannot build, deploy, activate or promote a plugin merely because a model picked
    the action. The underlying service functions remain the same ones used by the UI/CLI.
    """
    from .wp_plugin import (
        build_plugin_zip,
        deploy_plugin_beta,
        promote_plugin,
        scaffold_example_court_plugin,
        wp_set_plugin_status,
    )

    action = str(args.get("action") or "").strip().lower()
    if action == "scaffold":
        return scaffold_example_court_plugin(str(args.get("version") or "0.1.0"))
    if action == "build":
        return build_plugin_zip(str(args.get("version_suffix") or "beta"))
    if action == "deploy":
        # deploy_plugin_beta builds or selects the beta zip and uploads to WordPress; keep it gated.
        return deploy_plugin_beta(str(args.get("version_suffix") or "beta"), args.get("zip_path"))
    if action in {"activate", "deactivate"}:
        plugin = str(args.get("plugin") or "").strip()
        if not plugin:
            return {"ok": False, "error": "plugin slug is required for activate/deactivate"}
        return wp_set_plugin_status(plugin, "active" if action == "activate" else "inactive")
    if action == "promote":
        return promote_plugin(str(args.get("beta_slug") or ""),
                              str(args.get("production_slug") or ""))
    if action == "refresh":
        from .wp_plugin import refresh_scaffold
        return refresh_scaffold(str(args.get("version") or "0.2.0"))
    if action == "author":
        # The bug this fixes: author existed in the public tool executor (so the dry-run preview
        # worked) but NOT here, so a proposed author action died at confirmation with this very
        # error. Caught by cross-checking the parallel coder's patch. The confirmed action runs
        # the real codegen loop (backup, Claude Code on the plugin source, php validation,
        # byte-identical rollback on failure), then optionally chains build and gated deploy.
        from .plugin_author import author_plugin_change
        res = author_plugin_change(str(args.get("spec") or args.get("brief") or ""),
                                   route=str(args.get("route") or "auto"), apply=True)
        res["action"] = "author"
        if res.get("ok") and args.get("build_after", True):
            res["build"] = build_plugin_zip(str(args.get("version_suffix") or "beta"))
            if res["build"].get("ok") and args.get("deploy_after"):
                res["deploy"] = deploy_plugin_beta(str(args.get("version_suffix") or "beta"),
                                                   res["build"].get("zip_path"))
        return res
    return {
        "ok": False,
        "error": "wp_plugin_action needs action author/refresh/scaffold/build/deploy/activate/deactivate/promote after confirmation",
    }
