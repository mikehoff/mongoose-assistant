"""Claude Code integration: cost-aware, reuse-first, billing-honest.

Two distinct things people mean by "Claude Code integration", with very different economics:

  1. ORCHESTRATE THE LOCAL MODEL AGENTICALLY (free): Mongoose ALREADY does this: the tool loop in
     tools.py has Gemma (via Ollama) propose tool calls that Mongoose validates and executes, looping
     to max_steps. That runs entirely on the Mac mini: no tokens, no subscription touched. For
     agentic work over the household, this is the path: it costs nothing per run.

  2. REUSE A CLAUDE CODE INSTANCE (metered when automated): Claude Code is a separate product billed
     through a Claude subscription. Interactive use (a human at the terminal) stays on the flat plan.
     But HEADLESS use (`claude -p`, the Agent SDK, scheduled/automated calls) was the target of the
     June 15 2026 billing split that moves automated usage to a separate metered credit at API rates.
     That split is PAUSED as of June 2026, but Anthropic has said a revised version may follow, so we
     treat automated Claude Code calls as POTENTIALLY metered and make the exposure visible rather
     than spending silently.

This module:
  - discovers Claude Code locally AND on the Pi (reuse the instance that already lives there),
  - detects the single most common overspend trap: an ANTHROPIC_API_KEY in the environment, which
    makes Claude Code bill at API rates instead of using the subscription,
  - exposes a cost-aware recommendation (use the FREE local Gemma loop vs escalate to Claude Code),
  so the human decides when to spend rather than an automation deciding for them.

Read-only / advisory: it never invokes Claude Code itself. The existing builder owns invocation;
this layer informs the decision and surfaces the billing reality.
"""
from __future__ import annotations

import os
import shutil
from typing import Any

from ..settings import get_settings


def _local_claude_code() -> dict[str, Any]:
    """Is Claude Code present on THIS machine (the Mac mini), and how is it likely billed?"""
    s = get_settings()
    cmd = s.claude_code_cmd or "claude"
    path = shutil.which(cmd)
    api_key_set = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    return {
        "where": "local",
        "present": bool(path),
        "command": cmd,
        "resolved_path": path,
        # The billing trap: with ANTHROPIC_API_KEY set, Claude Code bills at API rates and ignores
        # the subscription entirely. Flag it loudly: it's the #1 way people overpay.
        "api_key_in_env": api_key_set,
        "billing_note": ("ANTHROPIC_API_KEY is set, so Claude Code here will bill at API rates and "
                         "NOT use your subscription. Unset it to use your plan."
                         if api_key_set else
                         "No ANTHROPIC_API_KEY in env: interactive use draws on your subscription."),
    }


def _pi_claude_code() -> dict[str, Any]:
    """Is Claude Code present on the Pi (the instance you may want to reuse)? Probes over the
    existing SSH transport; never raises. Also checks for the API-key trap in the Pi's environment."""
    from .protocol_v3 import _run_remote, transport_status
    t = transport_status()
    if not t.get("configured"):
        return {"where": "pi", "present": False, "reachable": False,
                "detail": "Pi SSH transport not configured (set PI_SSH_HOST)."}
    if not t.get("reachable"):
        return {"where": "pi", "present": False, "reachable": False,
                "detail": "Pi not reachable over SSH right now."}
    # `command -v claude` resolves the binary on the Pi without running it.
    probe = _run_remote("command -v claude 2>/dev/null || echo __none__")
    found = probe.ok and probe.stdout.strip() and "__none__" not in probe.stdout
    pi_path = probe.stdout.strip() if found else None
    # Check the Pi's login env for an API key (best-effort; a non-login shell may not source it).
    keyprobe = _run_remote("printenv ANTHROPIC_API_KEY >/dev/null 2>&1 && echo set || echo unset")
    api_key_set = keyprobe.ok and keyprobe.stdout.strip() == "set"
    return {
        "where": "pi",
        "present": bool(found),
        "reachable": True,
        "resolved_path": pi_path,
        "api_key_in_env": api_key_set,
        "billing_note": ("ANTHROPIC_API_KEY is set on the Pi: Claude Code there bills at API rates, "
                         "not your subscription. Unset it on the Pi to use your plan."
                         if api_key_set else
                         "No ANTHROPIC_API_KEY on the Pi: it can use your subscription."),
    }


def discover() -> dict[str, Any]:
    """Where is Claude Code available (local + Pi), and what's the billing posture of each?
    The 'reuse the Pi instance' answer: if the Pi has it, prefer that over installing another."""
    local = _local_claude_code()
    pi = _pi_claude_code()
    # Reuse guidance: a single instance the household shares beats one-per-machine.
    reuse = None
    if pi.get("present"):
        reuse = "pi"
    elif local.get("present"):
        reuse = "local"
    return {
        "local": local,
        "pi": pi,
        "reuse_target": reuse,
        "reuse_note": (
            "The Pi already has Claude Code: reuse that one instance over SSH rather than installing "
            "a second on the Mac. One shared instance is simpler and there's nothing to keep in sync."
            if reuse == "pi" else
            "Claude Code is on the Mac but not detected on the Pi. You can use the Mac instance, or "
            "install it on the Pi if you want the household to share one."
            if reuse == "local" else
            "Claude Code isn't detected locally or on the Pi. Install it where you'll drive it from "
            "(a Claude subscription includes it; the CLI install is free)."),
    }


# The cost model in one place, so the recommendation is explained, not asserted.
_COST_MODEL = {
    "local_gemma_loop": {
        "cost": "free",
        "why": "runs on the Mac mini via Ollama: no tokens, no subscription touched",
        "good_for": "household actions, search, drafting, anything the existing tools cover",
    },
    "claude_code_interactive": {
        "cost": "subscription (flat)",
        "why": "a human at the terminal draws on your Pro/Max plan's normal limits",
        "good_for": "you sitting down to pair on a real coding task",
    },
    "claude_code_headless": {
        "cost": "potentially metered (API rates)",
        "why": ("`claude -p` / automated runs were the target of the June 2026 billing split; it's "
                "PAUSED now so still on-plan, but a revised version may meter it at API rates"),
        "good_for": "genuine code changes a human reviews: not routine automation",
    },
}


def recommend_route(task_kind: str = "general") -> dict[str, Any]:
    """Cost-aware routing advice: should this go to the FREE local Gemma loop, or escalate to
    Claude Code (and at what billing exposure)? Advisory only: the human/route decides.

    task_kind: 'general' | 'household' | 'search' | 'code_change' | 'build'.
    """
    kind = (task_kind or "general").lower()
    # Anything the local tools can do should stay local: it's free and private.
    if kind in ("general", "household", "search", "draft", "qa"):
        return {
            "route": "local_gemma_loop",
            "billing": "free",
            "reason": "Mongoose's local tool loop (Gemma) handles this with no token cost.",
            "cost_model": _COST_MODEL["local_gemma_loop"],
        }
    # Real code changes are where Claude Code earns its keep. Flag the billing posture honestly.
    if kind in ("code_change", "build", "refactor"):
        disc = discover()
        billing_warning = []
        for node in ("local", "pi"):
            if disc[node].get("api_key_in_env"):
                billing_warning.append(f"{node}: ANTHROPIC_API_KEY set → bills at API rates")
        return {
            "route": "claude_code",
            "billing": "subscription if interactive; potentially metered if headless/automated",
            "reason": ("Genuine code changes benefit from Claude Code. Prefer INTERACTIVE use "
                       "(stays on your flat plan). Reserve headless `claude -p` for changes you'll "
                       "review: that's the path a future billing split would meter."),
            "reuse_target": disc["reuse_target"],
            "billing_warnings": billing_warning,
            "cost_model": {"interactive": _COST_MODEL["claude_code_interactive"],
                           "headless": _COST_MODEL["claude_code_headless"]},
        }
    return {
        "route": "local_gemma_loop",
        "billing": "free",
        "reason": "Default to the free local loop unless the task clearly needs Claude Code.",
        "cost_model": _COST_MODEL["local_gemma_loop"],
    }


def status_report() -> dict[str, Any]:
    """One call for the cockpit/CLI: where Claude Code is, the billing posture, reuse guidance, and
    the headline money-saving advice."""
    disc = discover()
    traps = []
    if disc["local"].get("api_key_in_env"):
        traps.append("ANTHROPIC_API_KEY is set locally: Claude Code will bill at API rates, "
                     "bypassing your subscription. Unset it to use your plan.")
    if disc["pi"].get("api_key_in_env"):
        traps.append("ANTHROPIC_API_KEY is set on the Pi: same API-rate trap there.")
    return {
        "discovery": disc,
        "billing_traps": traps,
        "headline": (
            "Agentic orchestration of your local model is already free (Mongoose's tool loop runs "
            "Gemma on the Mac). Reuse the Pi's Claude Code for real code changes, prefer interactive "
            "use to stay on your flat plan, and keep ANTHROPIC_API_KEY OUT of the environment so "
            "Claude Code uses your subscription rather than API-rate billing."),
    }
