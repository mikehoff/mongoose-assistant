from __future__ import annotations

from typing import Any

# Named Home-Assistant assistant prompts: the equivalent of the HA "Add from Home
# Assistant" conversation starters (Automation review, Setup guide, Energy report,
# etc.), but routed through MONGOOSE's brain (its persona + memory + local model +
# tools), not a cloud assistant. Each prompt is a small, fixed spec: a system
# framing plus a default instruction. Calling one runs a chat/tool turn so the
# answer is grounded in your actual setup, not generic.
#
# These are deterministic definitions (no model needed to LIST them), so the UI /
# CLI / MCP can show the menu instantly; only RUNNING one calls the model.

# Each prompt: key -> {title, description, system, prompt, kind}
# kind "chat" routes to persona+memory; "tool" routes to a tool turn (home control
# / data lookups). The home-state and energy ones lean on tools; advisory ones are
# chat.
HA_PROMPTS: dict[str, dict[str, str]] = {
    "automation_review": {
        "title": "Automation review",
        "description": "Review your automations for clashes, redundancy, and fragility.",
        "kind": "chat",
        "system": "You are Mongoose reviewing the user's Home Assistant automations. Be specific and practical: flag conflicting triggers, automations that fight each other, missing conditions, and fragile assumptions. Prefer concrete fixes over generic advice.",
        "prompt": "Review my Home Assistant automations and tell me what's risky, redundant, or likely to misfire, with concrete fixes.",
    },
    "automation_builder": {
        "title": "Automation builder",
        "description": "Draft a new automation from a plain-language description.",
        "kind": "chat",
        "system": "You are Mongoose helping build a Home Assistant automation. Ask only for what you truly need, then produce a clean YAML automation with a clear trigger/condition/action structure and sensible mode. Note any entities the user must confirm.",
        "prompt": "Help me build a new Home Assistant automation. I'll describe what I want; produce the YAML.",
    },
    "automation_debugger": {
        "title": "Automation debugger",
        "description": "Work out why an automation isn't firing or behaves wrongly.",
        "kind": "chat",
        "system": "You are Mongoose debugging a Home Assistant automation. Reason from triggers, conditions, state, and timing (remember HA schedule times are local; logbook/history are UTC). Give the most likely cause first and how to confirm it.",
        "prompt": "An automation isn't behaving as expected. Help me find out why and how to confirm the cause.",
    },
    "automation_audit": {
        "title": "Automation audit",
        "description": "A thorough safety/quality pass over all automations.",
        "kind": "chat",
        "system": "You are Mongoose auditing the user's whole automation set for safety and quality: unsafe actions, missing failsafes, privacy leaks (e.g. announcing private content on shared speakers), and maintainability. Be a hard but constructive auditor.",
        "prompt": "Audit all my automations for safety, privacy, and maintainability, and rank the issues by severity.",
    },
    "troubleshoot_device": {
        "title": "Troubleshoot device",
        "description": "Diagnose a device that's offline, unavailable, or misbehaving.",
        "kind": "chat",
        "system": "You are Mongoose troubleshooting a Home Assistant device. Work from the device's domain, integration, and likely failure modes (network, power, pairing, assumed_state). Give an ordered checklist, most likely fix first.",
        "prompt": "A device is offline or misbehaving. Walk me through diagnosing and fixing it.",
    },
    "setup_guide": {
        "title": "Setup guide",
        "description": "Step-by-step help setting up an integration or device.",
        "kind": "chat",
        "system": "You are Mongoose giving Home Assistant setup guidance. Be concrete and current: exact steps, where in the UI, what to enter, and the common gotcha for that integration. Avoid vague filler.",
        "prompt": "Guide me through setting up a new Home Assistant integration or device, step by step.",
    },
    "schedule_optimizer": {
        "title": "Schedule optimizer",
        "description": "Tune schedules and timers around your routine.",
        "kind": "chat",
        "system": "You are Mongoose optimizing Home Assistant schedules (lights, heating, etc.) around the user's actual routine and goals (comfort, cost, sleep). Be specific about times and entities; respect the local timezone.",
        "prompt": "Help me optimize my home schedules and timers around my routine.",
    },
    "naming_conventions": {
        "title": "Naming conventions",
        "description": "A consistent naming/area scheme for entities and devices.",
        "kind": "chat",
        "system": "You are Mongoose proposing a consistent Home Assistant naming and area convention. Give a clear scheme (area-device-function), examples, and how to migrate without breaking automations.",
        "prompt": "Propose a clean, consistent naming convention for my entities and areas, with migration tips.",
    },
    "daily_summary": {
        "title": "Daily summary",
        "description": "A cross-domain summary of what matters today (the brief).",
        "kind": "brief",
        "system": "",
        "prompt": "",
    },
    "energy_report": {
        "title": "Energy report",
        "description": "Summarize energy use and suggest savings.",
        "kind": "chat",
        "system": "You are Mongoose producing a Home Assistant energy report. Summarize usage from available energy sensors, flag the biggest consumers, and give concrete, prioritized savings. Be honest if the data isn't there to support a claim.",
        "prompt": "Give me an energy report: biggest consumers and prioritized ways to cut usage.",
    },
    "dashboard_builder": {
        "title": "Dashboard builder",
        "description": "Design a Lovelace dashboard for a room or purpose.",
        "kind": "chat",
        "system": "You are Mongoose designing a Home Assistant Lovelace dashboard. Propose a layout (views, cards) for the stated room/purpose and produce YAML the user can paste, noting any custom cards needed.",
        "prompt": "Help me design a Lovelace dashboard for a room or purpose, and give the YAML.",
    },
    "change_validator": {
        "title": "Change validator",
        "description": "Sanity-check a config change before you apply it.",
        "kind": "chat",
        "system": "You are Mongoose validating a proposed Home Assistant config change before it's applied. Check for syntax, breaking references, and side effects on existing automations. Say clearly whether it's safe to apply and what to watch.",
        "prompt": "I'm about to make a Home Assistant config change. Validate it and tell me if it's safe to apply.",
    },
    "security_review": {
        "title": "Security review",
        "description": "Review exposure, access, and privacy of your setup.",
        "kind": "chat",
        "system": "You are Mongoose performing a Home Assistant security review: remote exposure, tokens, default-open ports, third-party integrations, and privacy of announcements/recordings. Prioritize by real risk and give concrete hardening steps.",
        "prompt": "Review the security and privacy of my Home Assistant setup and give prioritized hardening steps.",
    },
}

PROMPT_KEYS = tuple(HA_PROMPTS)


def list_prompts() -> list[dict[str, str]]:
    """The menu: key, title, description for each prompt. No model call."""
    return [{"key": k, "title": v["title"], "description": v["description"]}
            for k, v in HA_PROMPTS.items()]


def get_prompt(key: str) -> dict[str, str] | None:
    return HA_PROMPTS.get(key)


async def run_prompt(key: str, *, extra: str = "") -> dict[str, Any]:
    """Run a named prompt through Mongoose's brain. `extra` appends the user's
    specifics (e.g. which device, what change). Returns the reply plus metadata."""
    spec = HA_PROMPTS.get(key)
    if spec is None:
        return {"ok": False, "error": f"unknown prompt: {key!r}",
                "available": list(PROMPT_KEYS)}

    # The daily summary IS the cross-domain brief; reuse it rather than re-asking.
    if spec["kind"] == "brief":
        from .brief import generate_brief
        result = await generate_brief(post_to_bus=False)
        return {"ok": True, "key": key, "title": spec["title"],
                "reply": result["brief"], "kind": "brief"}

    prompt = spec["prompt"]
    if extra.strip():
        prompt = f"{prompt}\n\nSpecifics: {extra.strip()}"

    from ..llm import ask_ollama, LocalModelError
    try:
        reply = await ask_ollama(prompt, system=spec["system"], role="chat")
    except LocalModelError as exc:
        return {"ok": False, "key": key, "error": "local_model_unavailable",
                "detail": str(exc)}
    # HONESTY (QA-audit P1.5): prompts whose kind isn't a live-data kind (brief / home-state /
    # energy) are ADVISORY , the model reasons from general HA knowledge and whatever the user
    # typed in `extra`, NOT from the user's actual automations/logs/entities. Flag that so the
    # answer isn't mistaken for one grounded in their real setup.
    grounded_kinds = {"brief", "home_state", "energy"}
    advisory = spec["kind"] not in grounded_kinds and not extra.strip()
    result = {"ok": True, "key": key, "title": spec["title"],
              "reply": reply.strip(), "kind": spec["kind"], "advisory": advisory}
    if advisory:
        result["advisory_note"] = ("General guidance, not read from your live Home Assistant. "
                                   "For specifics, paste the relevant automation/config or ask "
                                   "via a tool that reads your real setup.")
    return result
