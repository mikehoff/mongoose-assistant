"""Deterministic chat bridge for the site's RTM WordPress plugin authoring.

This is deliberately small and rule-based. The local model may phrase things warmly, but the routing
for "make the WordPress plugin useful, build it, deploy it" must not depend on a magic prompt or on the
model choosing the right tool. It returns either None (normal chat) or a ready-to-persist reply payload.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from .. import profile as _profile

_CONFIRM_RE = re.compile(r"\bconfirm\b(?:\s+token)?\s+([A-Za-z0-9_\-]{8,})", re.IGNORECASE)

# Holds live background build tasks so asyncio does not garbage-collect them mid-run.
_BUILD_TASKS: set = set()


@lru_cache(maxsize=4)
def _plugin_terms_re_for(rtm_name: str) -> re.Pattern[str]:
    # The organisation's name is a plugin cue too ("update the <name> page"), spaces optional.
    name = r"\s*".join(re.escape(w) for w in rtm_name.lower().split()) or r"(?!x)x"
    return re.compile(
        r"\b(wordpress|wp|plugin|" + name + r"|rtm\s+portal|service\s+charges?\s+page|communications?\s+page|leaseholder\s+portal)\b",
        re.IGNORECASE,
    )


def _plugin_terms_re() -> re.Pattern[str]:
    return _plugin_terms_re_for(_profile.rtm_name())


_AUTHOR_TERMS_RE = re.compile(
    r"\b(author|write|rewrite|change|update|improve|fix|make|build\s+something\s+better|better|real|working|useful|hollow|placeholder|coming\s+soon|"
    r"service\s+charges?|communications?|notices?|minutes?|dashboard|screen|page|show|render)\b",
    re.IGNORECASE,
)
_BUILD_TERMS_RE = re.compile(r"\b(build|zip|package|compile)\b", re.IGNORECASE)
_DEPLOY_TERMS_RE = re.compile(r"\b(deploy|upload|push|send\s+to\s+wordpress|install\s+on\s+wordpress|put\s+it\s+on\s+the\s+site)\b", re.IGNORECASE)
_SCAFFOLD_TERMS_RE = re.compile(r"\b(scaffold|create\s+starter|start\s+from\s+scratch)\b", re.IGNORECASE)


def _format_confirmed_result(res: dict[str, Any]) -> str:
    if not res.get("ok"):
        # Surface the REAL error, not just the generic reason: the underlying detail (output_tail /
        # hint / problems) lives at the top level or nested under "result". Without this the owner sees
        # only "agent_run_failed" and cannot tell it was, say, "claude binary not found".
        inner = res.get("result") if isinstance(res.get("result"), dict) else {}
        reason = str(res.get("error") or res.get("reason") or inner.get("reason") or "unknown")
        # Prefer the SPECIFIC validation problems (which file, which check) over the generic hint, so
        # the owner can actually refine the spec rather than guess.
        probs = res.get("problems") or inner.get("problems")
        if probs:
            detail = "What failed validation:\n- " + "\n- ".join(str(p) for p in list(probs)[:6])
        else:
            detail = str(res.get("output_tail") or res.get("hint") or inner.get("output_tail")
                         or inner.get("hint") or "")
        msg = "I tried to run the confirmed action, but it failed: " + reason
        if detail:
            msg += "\n\n" + detail[:600]
        return msg
    action = str(res.get("action") or "").lower()
    if action == "author":
        bits = [f"Done: I updated the {_profile.rtm_name()} WordPress plugin source from the chat-approved brief."]
        if res.get("path"):
            bits.append(f"Source: {res['path']}.")
        build = res.get("build") or {}
        if build.get("ok"):
            bits.append(f"Built zip: {build.get('file_name') or build.get('zip_path')}.")
        deploy = res.get("deploy") or {}
        if deploy:
            if deploy.get("ok"):
                bits.append("Uploaded the beta to WordPress; it is still separate from activation unless you explicitly activate/promote it.")
            else:
                bits.append("The source/build worked, but WordPress upload failed: " + str(deploy.get("reason") or deploy.get("error"))[:200])
        changed = res.get("changed_files") or []
        if changed:
            bits.append("Changed: " + ", ".join(str(c) for c in changed[:8]) + ".")
        if res.get("validation", {}).get("php_lint") is False:
            bits.append("Note: php is not installed on this Mac, so validation was structural only; brew install php enables full syntax checking.")
        return "\n".join(bits)
    # Generic fallback for any other sensitive confirmation token.
    facts = []
    for key in ("zip_path", "file_name", "plugin", "version", "status", "next_step"):
        if res.get(key):
            facts.append(f"{key}: {res[key]}")
    return "Confirmed and run. " + ("; ".join(facts) if facts else "The action completed.")


def _proposal_reply(proposal: dict[str, Any], *, action: str = "author", deploy_after: bool = False) -> str:
    """Plain, action-specific confirmation. DEPLOY does not touch the code; AUTHOR runs Claude Code;
    they must read differently so 'deploy' never sounds like (or triggers) a rebuild."""
    token = proposal.get("confirm_token")
    if action == "deploy":
        lines = ["I'll deploy the plugin that's already built to WordPress as a beta. The code doesn't "
                 "change, and it won't be activated until you separately ask."]
    elif action == "scaffold":
        lines = [f"I'll scaffold a fresh {_profile.rtm_name()} plugin skeleton (a starting point, not your live code)."]
    elif action == "build":
        lines = ["I'll package the current plugin source into a zip. Nothing is uploaded and the live "
                 "site isn't touched."]
    else:  # author
        lines = [proposal.get("summary") or f"I'll update the {_profile.rtm_name()} plugin from your request.",
                 "I'll run Claude Code on the plugin source (with a backup, PHP validation, and automatic "
                 "rollback if it comes out broken), then build the plugin zip."]
        if deploy_after:
            lines.append("Since you asked to deploy it too, the run will also upload the beta to "
                         "WordPress afterwards, but won't activate it without a separate step.")
        else:
            lines.append("It will not touch the live WordPress site unless you separately ask to deploy it.")
    lines.append(f"To go ahead, reply: confirm {token}")
    return "\n".join(lines)


async def maybe_handle_plugin_chat(prompt: str, conversation_id: str = "", *, force: bool = False) -> dict[str, Any] | None:
    """Return a chat result for plugin build/deploy intents, or None for normal chat.

    The caller persists the returned reply into the conversation. This function may execute a previously
    proposed sensitive action only when the user supplies its short-lived confirmation token.

    force=True: the unified intent resolver already decided this is a plugin-build command (e.g. "build
    the next RTM iteration", which lacks the literal word "plugin"), so skip the keyword/likelihood
    gates and author from the brief. The confirm-token path and the propose/confirm safety gate are
    unchanged: nothing builds or deploys without the explicit `confirm <token>` reply.
    """
    text = (prompt or "").strip()
    if not text:
        return None

    # Confirmation is generic: if the owner says "confirm <token>", do not make the local model infer
    # a token from prose. Just pass the token to the existing sensitive gate.
    cm = _CONFIRM_RE.search(text)
    if cm:
        token = cm.group(1)
        from .sensitive_tools import confirm_sensitive_action_async, peek_pending
        peek = peek_pending(token)
        if peek and peek[0] == "wp_plugin_action" and conversation_id:
            # A plugin build runs Claude Code for minutes. Do NOT hold the chat request open (that
            # froze the phone with "reconnecting"): acknowledge immediately and run it in the
            # background, then post the result to this conversation when it finishes.
            import asyncio

            async def _run_and_report() -> None:
                try:
                    res = await confirm_sensitive_action_async(token)
                    out = _format_confirmed_result(res)
                except Exception as exc:
                    out = "The plugin build hit an error: " + str(exc)[:200]
                try:
                    from .memory import add_message
                    add_message(conversation_id, "assistant", out)
                except Exception:
                    pass

            t = asyncio.create_task(_run_and_report())
            _BUILD_TASKS.add(t)
            t.add_done_callback(_BUILD_TASKS.discard)
            return {"reply": ("On it. I'm running Claude Code on the plugin source now. This usually "
                              "takes a few minutes; I'll post the result right here when it's done, "
                              "and it will not touch the live site."),
                    "source": "wp_plugin_chat"}
        res = await confirm_sensitive_action_async(token)
        return {"reply": _format_confirmed_result(res), "source": "sensitive_confirm", "result": res}

    if not force and not _plugin_terms_re().search(text):
        return None

    # Likelihood gate: if the last thing this bridge did in this conversation drew confusion, a
    # verbatim repeat, or the same gist reworded, stand down and let the model take the turn.
    # The confirm-token intercept above deliberately bypasses this: "confirm <token>" is
    # explicit intent, never a misfire. A resolver-forced call also bypasses it (the resolver is
    # the deliberate decision).
    from .rule_engagement import rule_ok
    if not force and not rule_ok("wp_plugin_chat", text, conversation_id):
        return None

    from .sensitive_tools import propose_sensitive_action

    low = text.lower()
    deploy = bool(_DEPLOY_TERMS_RE.search(text))
    # "change the code" = any request that alters the plugin source: explicit author verbs OR a build
    # verb. This is the key distinction: DEPLOY alone must NOT change code (that was the bug where
    # "deploy the plugin" silently re-ran Claude Code and rebuilt an unchanged plugin).
    change_code = bool(_AUTHOR_TERMS_RE.search(text) or _BUILD_TERMS_RE.search(text) or "zip" in low)

    # 1) Scaffold: explicit "start from scratch".
    if _SCAFFOLD_TERMS_RE.search(text):
        proposal = propose_sensitive_action("wp_plugin_action", {"action": "scaffold", "version": "0.2.0"})
        return {"reply": _proposal_reply(proposal, action="scaffold"), "source": "wp_plugin_chat", "proposal": proposal}

    # 2) Author: change the source (optionally deploy after). Covers explicit author/build verbs.
    if change_code:
        proposal = propose_sensitive_action("wp_plugin_action", {
            "action": "author", "spec": text, "build_after": True,
            "deploy_after": deploy, "version_suffix": "beta"})
        return {"reply": _proposal_reply(proposal, action="author", deploy_after=deploy),
                "source": "wp_plugin_chat", "proposal": proposal}

    # 3) Deploy-only: "deploy/upload/push it" with NO code-change verb. Ship what's already built; no
    # Claude Code, no rebuild.
    if deploy:
        proposal = propose_sensitive_action("wp_plugin_action", {"action": "deploy", "version_suffix": "beta"})
        return {"reply": _proposal_reply(proposal, action="deploy"), "source": "wp_plugin_chat", "proposal": proposal}

    # 4) Resolver-forced with no explicit signal (e.g. "implement the spec", "build RTM") defaults to
    # authoring the next iteration from the brief.
    if force:
        proposal = propose_sensitive_action("wp_plugin_action", {
            "action": "author", "spec": text, "build_after": True,
            "deploy_after": False, "version_suffix": "beta"})
        return {"reply": _proposal_reply(proposal, action="author"), "source": "wp_plugin_chat", "proposal": proposal}

    return None
