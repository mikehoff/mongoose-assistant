"""Mongoose MCP server: exposes the message bus and home/search tools to
Claude Code and Claude Desktop as local MCP tools (stdio transport).

Run it directly:  python -m mongoose.mcp_server
Or wire it into Claude Desktop / Claude Code config (see docs/MCP_SETUP.md).

This is a LOCAL server (stdio). It runs on the same machine as Mongoose and
talks to Mongoose's own services in-process, so nothing is exposed to the
network and the local-first guarantee holds.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

# The MCP SDK is only needed to RUN the server, not to import the tool logic,
# so tests can import the registry without the SDK installed.
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    _HAVE_MCP = True
except Exception:  # pragma: no cover - exercised only when SDK absent
    _HAVE_MCP = False


# --- Tool registry: name -> (description, json schema, async handler) ---------
# Handlers return a JSON-serialisable dict. They call Mongoose services directly.

def _tools() -> dict[str, dict[str, Any]]:
    from .services import bus
    from .tools import _tool_home, _tool_weather, _tool_calendar_availability
    from .services.email_index import search_email

    async def send(args):
        return bus.send_message(
            sender=args.get("from", "claude_code"), to=args["to"],
            subject=args["subject"], body=args.get("body", ""),
            type=args.get("type", "note"), re_id=args.get("re"))

    async def inbox(args):
        return {"messages": bus.list_messages(
            to=args.get("to"), sender=args.get("from"),
            type=args.get("type"), unread_only=args.get("unread_only", True))}

    async def read(args):
        m = bus.read_message(args["id"], archive=args.get("archive", True))
        return m or {"error": "no such message"}

    async def search_bus(args):
        return {"messages": bus.search_messages(args["query"])}

    async def share(args):
        return bus.share_file(args["path"], name=args.get("name"))

    async def control_home(args):
        # SAFETY (QA-audit P1.4): MCP exposes tools as independent callables, so Mongoose
        # cannot tell whether a control_home call was genuinely user-requested or INDUCED by a
        # tool result Claude just read (a prompt-injected email/file/bus item). The in-process
        # tool loop blocks read->act escalation; MCP has no such context. So acting via MCP is
        # OPT-IN: off unless MONGOOSE_MCP_ENABLE_ACTING is set. Reading tools stay available.
        import os
        if os.environ.get("MONGOOSE_MCP_ENABLE_ACTING", "").strip().lower() not in ("1", "true", "yes"):
            return {"ok": False,
                    "error": "control_home is disabled over MCP by default (it can act on your "
                             "home). To enable it, set MONGOOSE_MCP_ENABLE_ACTING=true in the "
                             "environment running the Mongoose MCP server."}
        return await _tool_home(args)

    async def weather(args):
        return await _tool_weather(args)

    async def availability(args):
        return await _tool_calendar_availability(args)

    async def email(args):
        return {"results": search_email(args["query"], int(args.get("limit", 10)))}

    async def files(args):
        # Unified search: files (all devices) + attachments + email, so "search my
        # files" finds everything, not just local files.
        from .services.find import find
        return find(args["query"], limit=int(args.get("limit", 10)))

    async def find_all(args):
        from .services.find import find
        kinds = args.get("kinds")
        kind_tuple = tuple(kinds) if isinstance(kinds, list) else None
        return find(args["query"], limit=int(args.get("limit", 10)), kinds=kind_tuple)

    async def attachments(args):
        from .services.attachments import search_attachments
        return {"results": search_attachments(args["query"], int(args.get("limit", 20)))}

    async def ha_prompts(args):
        from .services.ha_prompts import list_prompts, run_prompt
        key = args.get("key")
        if not key:
            return {"prompts": list_prompts()}
        return await run_prompt(key, extra=str(args.get("extra", "")))

    async def push_notification(args):
        from .services.notifications import NotificationIn, ingest_notification
        n = NotificationIn(
            source=str(args.get("source", "mcp")),
            app=str(args.get("app", "unknown")),
            title=str(args.get("title", "")),
            body=str(args.get("body", "")),
            privacy=str(args.get("privacy", "medium")),
            urgency=str(args.get("urgency", "normal")),
            device=str(args.get("device", "unknown")),
        )
        return await ingest_notification(n)

    async def send_phone(args):
        from .services.notifications import send_to_phone
        return await send_to_phone(
            str(args.get("title", "Mongoose")),
            str(args.get("body", "")),
            priority=str(args.get("priority", "default")),
            tags=args.get("tags"),
            click=args.get("click"),
        )

    async def what_changed_tool(args):
        from .services.journal_layer import what_changed
        return what_changed(acknowledge=bool(args.get("acknowledge", True)))

    async def ritual_tool(args):
        from .services.rituals import start_ritual, list_rituals
        name = args.get("name")
        if not name:
            return {"rituals": list_rituals()}
        return start_ritual(str(name))

    async def whatsapp_search_tool(args):
        from .services.whatsapp import search_messages
        return {"results": search_messages(str(args["query"]), int(args.get("limit", 10)))}

    async def whatsapp_draft_tool(args):
        # DRAFT ONLY. There is deliberately no MCP tool that sends; sending requires
        # explicit human approval of a specific draft, so the model can't autosend.
        from .services.whatsapp import draft_message
        d = draft_message(str(args["contact"]), str(args["body"]))
        d["note"] = "draft only; the user must approve before this is sent"
        return d

    async def rtm_search_tool(args):
        from .services.rtm_intake import search_intake
        return {"results": search_intake(str(args["query"]), int(args.get("limit", 20)))}

    async def rtm_list_tool(args):
        from .services.rtm_intake import list_intake
        return {"items": list_intake(int(args.get("limit", 50)))}

    async def rtm_draft_tool(args):
        # DRAFT ONLY , mirrors the WhatsApp discipline. There is deliberately no MCP tool that
        # sends an RTM reply; sending requires explicit human approval of a specific draft.
        from .services.rtm_intake import generate_draft
        d = generate_draft(str(args["intake_id"]))
        if d.get("ok"):
            d["note"] = "draft only; the user must approve before this is sent"
        return d

    return _render_tool_copy({
        "bus_send": {
            "description": "Send a message (brief/task/alert/reply/note) to another agent or to the human on the Mongoose bus.",
            "schema": {"type": "object", "required": ["to", "subject"], "properties": {
                "from": {"type": "string", "description": "sender id, e.g. claude_code"},
                "to": {"type": "string", "description": "recipient id, e.g. {{owner.id}}, mongoose, ha"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "type": {"type": "string", "enum": ["brief", "task", "alert", "reply", "note"]},
                "re": {"type": "string", "description": "id of the message this replies to"}}},
            "handler": send,
        },
        "bus_inbox": {
            "description": "List bus messages, newest first. Defaults to unread. Filter by to/from/type.",
            "schema": {"type": "object", "properties": {
                "to": {"type": "string"}, "from": {"type": "string"},
                "type": {"type": "string"}, "unread_only": {"type": "boolean"}}},
            "handler": inbox,
        },
        "bus_read": {
            "description": "Read a bus message by id and (by default) archive it as read.",
            "schema": {"type": "object", "required": ["id"], "properties": {
                "id": {"type": "string"}, "archive": {"type": "boolean"}}},
            "handler": read,
        },
        "bus_search": {
            "description": "Full-text search across bus messages.",
            "schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            "handler": search_bus,
        },
        "bus_share_file": {
            "description": "Copy a local file into the shared bus files/ surface so other agents can read it.",
            "schema": {"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string"}, "name": {"type": "string"}}},
            "handler": share,
        },
        "control_home": {
            "description": "Control a Home Assistant device by room + device name (e.g. room='study', device_id='cat neon', action='turn_on'). Resolves to the real entity safely.",
            "schema": {"type": "object", "properties": {
                "room": {"type": "string"}, "device_id": {"type": "string"},
                "action": {"type": "string"}, "entity_id": {"type": "string"}}},
            "handler": control_home,
        },
        "get_weather": {
            "description": "Current weather, rain window and UV from Mongoose's cache.",
            "schema": {"type": "object", "properties": {"force": {"type": "boolean"}}},
            "handler": weather,
        },
        "check_availability": {
            "description": "Whether the calendar is busy or free in an ISO8601 time window.",
            "schema": {"type": "object", "required": ["start_at", "end_at"], "properties": {
                "start_at": {"type": "string"}, "end_at": {"type": "string"}}},
            "handler": availability,
        },
        "search_email": {
            "description": "Read-only full-text search over indexed email.",
            "schema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"}}},
            "handler": email,
        },
        "search_files": {
            "description": "Search EVERYTHING at once: files across all devices, email attachments, and email text. Each result is labelled by kind (file/attachment/email).",
            "schema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"}}},
            "handler": files,
        },
        "find": {
            "description": "Unified search across files (all devices), email attachments, and email text. Optionally restrict with kinds (file/attachment/email).",
            "schema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"},
                "kinds": {"type": "array", "items": {"type": "string"}}}},
            "handler": find_all,
        },
        "search_attachments": {
            "description": "Find email attachments by filename, subject, sender, or extracted content, with which email each came from.",
            "schema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"}}},
            "handler": attachments,
        },
        "ha_prompts": {
            "description": "List or run named Home Assistant assistant prompts (automation_review, energy_report, setup_guide, etc.). Omit 'key' to list; pass 'key' (and optional 'extra') to run one through Mongoose's brain.",
            "schema": {"type": "object", "properties": {
                "key": {"type": "string"}, "extra": {"type": "string"}}},
            "handler": ha_prompts,
        },
        "push_notification": {
            "description": "Send a notification into Mongoose (from a phone, tablet, HA, or any source). Routed and privacy-gated like any ingested notification.",
            "schema": {"type": "object", "required": ["app", "title"], "properties": {
                "source": {"type": "string"}, "app": {"type": "string"},
                "title": {"type": "string"}, "body": {"type": "string"},
                "privacy": {"type": "string"}, "urgency": {"type": "string"},
                "device": {"type": "string"}}},
            "handler": push_notification,
        },
        "send_to_phone": {
            "description": "Send a push notification OUT to the user's phone via ntfy (Mongoose telling the user something). priority max|high|default|low|min; optional tags and a click URL.",
            "schema": {"type": "object", "required": ["title"], "properties": {
                "title": {"type": "string"}, "body": {"type": "string"},
                "priority": {"type": "string"}, "tags": {"type": "string"},
                "click": {"type": "string"}}},
            "handler": send_phone,
        },
        "what_changed": {
            "description": "Cross-domain delta since the user last looked: new WhatsApps, email, indexed files, notifications. Set acknowledge=false to peek without resetting.",
            "schema": {"type": "object", "properties": {"acknowledge": {"type": "boolean"}}},
            "handler": what_changed_tool,
        },
        "ritual": {
            "description": "Start a useful micro-practice (physio, proprioception, breathing, journal, resilience, friend_checkin) and get its short script. Omit 'name' to list.",
            "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
            "handler": ritual_tool,
        },
        "whatsapp_search": {
            "description": "Search the user's locally-indexed WhatsApp messages (private content masked). Use to answer 'what did X say?' or 'who have I not replied to?'.",
            "schema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"}}},
            "handler": whatsapp_search_tool,
        },
        "whatsapp_draft": {
            "description": "DRAFT a WhatsApp message to a contact. This does NOT send: it creates a pending draft the user must explicitly approve. Mongoose can compose check-ins but never sends autonomously.",
            "schema": {"type": "object", "required": ["contact", "body"], "properties": {
                "contact": {"type": "string"}, "body": {"type": "string"}}},
            "handler": whatsapp_draft_tool,
        },
        "rtm_search": {
            "description": "Search {{rtm.name}} RTM intake (emails + voicemail transcripts). Read-only.",
            "schema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"}}},
            "handler": rtm_search_tool,
        },
        "rtm_list": {
            "description": "List recent {{rtm.name}} RTM intake items. Read-only.",
            "schema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
            "handler": rtm_list_tool,
        },
        "rtm_draft_email": {
            "description": "Create a DRAFT-ONLY email response for an RTM intake item. Does NOT send: the user must explicitly approve the draft. There is deliberately no MCP tool that sends RTM email.",
            "schema": {"type": "object", "required": ["intake_id"], "properties": {
                "intake_id": {"type": "string"}}},
            "handler": rtm_draft_tool,
        },
    })


def _render_tool_copy(registry: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fill the site-profile placeholders in tool descriptions (the RTM's name, the owner's bus id)."""
    from .settings import render_profile_copy

    def walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return render_profile_copy(obj)
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return obj
    return {name: {k: (walk(v) if k in ("description", "schema") else v) for k, v in spec.items()}
            for name, spec in registry.items()}


def build_server():  # pragma: no cover - requires MCP SDK
    if not _HAVE_MCP:
        raise RuntimeError("The 'mcp' package is required to run the server: pip install mcp")
    from .db import init_db
    init_db()
    registry = _tools()
    server = Server("mongoose")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(name=n, description=t["description"], inputSchema=t["schema"])
                for n, t in registry.items()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        tool = registry.get(name)
        if not tool:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool {name}"}))]
        try:
            result = await tool["handler"](arguments or {})
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def _main():  # pragma: no cover
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
