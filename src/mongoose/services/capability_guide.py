"""What can I say? Natural-language discovery of Mongoose's capabilities and workflows.

the owner's requirement, verbatim in spirit: "I should be able to ask Mongoose chat, in any natural
language, for the right utterances to build it out (plugin, self heal, anything else in future)
as I won't remember this lingo by heart."

So this module is the single place the lingo lives. The chat brain routes any "how do I…",
"what do I say to…", "co mam powiedzieć…", "jak zmienić…" question to the capability_guide
tool, which answers with the concrete things to say and the order to say them in.

Two anti-drift properties, both enforced by tests:
  1. Every workflow step that names a tool action must name one that really exists in the live
     TOOLS registry, so the guide can never teach utterances that stopped working.
  2. The guide's search also covers the registry's own descriptions, so NEW tools added later
     are discoverable here automatically even before anyone writes them a recipe.

The person asks in any language; the brain translates intent and calls guide(topic). Keywords
below include Polish alongside English as an example of a second language,
but matching is best-effort, not a gate: an unmatched topic returns the overview rather than
nothing.
"""
from __future__ import annotations

from typing import Any

# Each step: say = a natural phrase that works verbatim in chat; does = what happens;
# tool = the registry tool it exercises (contract-checked), "" for purely conversational steps.
# The workflows as written: copy may carry site-profile placeholders ("{{site.domain}}"). Read them as
# WORKFLOWS (module attribute, below) or _workflows(), which fill them in.
_WORKFLOWS: list[dict[str, Any]] = [
    {
        "id": "plugin_build_out",
        "title": "Change or build out the {{site.domain}} WordPress plugin",
        "when": "You want the RTM website plugin to do something new or different.",
        "keywords": ["plugin", "wordpress", "website", "site", "{{RTM_COMPACT}}", "wtyczka",
                     "strona", "page", "service charges page", "notices"],
        "steps": [
            {"say": "Refresh the plugin template",
             "does": "Regenerates the template files in your plugin source dir (with backup) so "
                     "it picks up shipped template improvements. Custom files are preserved.",
             "tool": "wp_plugin_action"},
            {"say": "Make the Service Charges page show a total arrears figure at the top",
             "does": "Any sentence describing the change. Mongoose proposes the action and gives "
                     "you a confirmation token; nothing changes yet.",
             "tool": "wp_plugin_action"},
            {"say": "confirm <token>",
             "does": "Runs Claude Code on the plugin source with backup, PHP validation and "
                     "automatic rollback if the result is broken, then builds the zip. Add "
                     "'and deploy it' to your original request to also upload the beta; "
                     "activate/promote always stays a separate ask.",
             "tool": "wp_plugin_action"},
            {"say": "Build the plugin, then deploy it",
             "does": "Zips the source and uploads it to {{site.domain}} (you activate/promote "
                     "when happy).",
             "tool": "wp_plugin_action"},
            {"say": "Push the portal snapshot",
             "does": "Sends the live flats/demands data to the site so the Service Charges "
                     "page has real content.",
             "tool": ""},
        ],
    },
    {
        "id": "self_heal",
        "title": "Report a bug and let Mongoose try to fix itself",
        "when": "Something misbehaves and you want it diagnosed and repaired.",
        "keywords": ["bug", "fix", "broken", "self heal", "heal", "repair", "błąd", "napraw",
                     "nie działa", "diagnose", "problem"],
        "steps": [
            {"say": "Log a bug: the phone launchpad shows a blank music card",
             "does": "Records the report with context so it isn't lost.", "tool": "log_bug"},
            {"say": "Diagnose that bug",
             "does": "Investigates and reports the likely cause.", "tool": "diagnose_bug"},
            {"say": "Attempt a fix",
             "does": "Proposes/attempts a code fix. Sensitive: Mongoose asks you to confirm "
                     "before anything durable changes.", "tool": "attempt_bug_fix"},
            {"say": "Apply the fix",
             "does": "Applies it after your confirmation.", "tool": "apply_bug_fix"},
            {"say": "What's the status of my bugs?",
             "does": "Lists open reports and where each one stands.", "tool": "bug_status"},
        ],
    },
    {
        "id": "update_mongoose",
        "title": "Update Mongoose itself",
        "when": "A new build zip exists and you want it checked and installed.",
        "keywords": ["update", "upgrade", "new version", "aktualizacja", "install", "build zip"],
        "steps": [
            {"say": "Check for an update",
             "does": "Compares the newest staged/known build against what's running.",
             "tool": "check_update"},
            {"say": "Apply the update",
             "does": "Backs up, applies, verifies and restarts. Sensitive: asks you to confirm "
                     "first.", "tool": "apply_update"},
        ],
    },
    {
        "id": "rtm_paperwork",
        "title": "RTM service charges, statements and the leaseholder site",
        "when": "{{rtm.name}} money/paperwork: statements, demands, what's on the site.",
        "keywords": ["rtm", "service charge", "demand", "statement", "arrears", "leaseholder",
                     "czynsz", "opłaty", "portal"],
        "steps": [
            {"say": "Show me the RTM statement for flat 3",
             "does": "The per-flat balance and history from the ledger.", "tool": "rtm_statement"},
            {"say": "Push the portal snapshot",
             "does": "Publishes the current flats/demands data to {{site.domain}}.", "tool": ""},
            {"say": "What's on the RTM site right now?",
             "does": "Plugin/deployment status of {{site.domain}}.", "tool": "wp_plugin_status"},
        ],
    },
    {
        "id": "guests",
        "title": "Invite a guest to Mongoose",
        "when": "You want a friend or a visitor to have their own phone access.",
        "keywords": ["guest", "invite", "gość", "zaproś", "pair", "phone access"],
        "steps": [
            {"say": "Create a guest invite for Sam",
             "does": "Checks readiness (DDNS/HTTPS/network binding) and produces a one-time "
                     "invite link; tells you honestly what's missing if something is.",
             "tool": ""},
        ],
    },
]


_ASSISTANT_NAME = "Mongoose"


def _owner_lower() -> str:
    from .. import profile as _profile
    return _profile.owner_name().strip().lower()


def _workflows() -> list[dict[str, Any]]:
    """WORKFLOWS with the site-profile placeholders (the site domain, the RTM's name) filled in."""
    from ..settings import render_profile_copy

    def walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return render_profile_copy(obj)
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        return obj
    return walk(_WORKFLOWS)


def __getattr__(name: str) -> Any:
    # `capability_guide.WORKFLOWS` (and `from ... import WORKFLOWS`) always yields the rendered workflows,
    # so consumers such as the capability index never show a raw {{placeholder}}.
    if name == "WORKFLOWS":
        return _workflows()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _address(say: str) -> str:
    """Prefix an example utterance with the assistant's name so a copied/spoken command is addressed
    (natural for a wake word), unless it already opens with a name."""
    s = (say or "").strip()
    if not s:
        return s
    low = s.lower()
    if low.startswith(("mongoose", _owner_lower(), "hey ")):
        return s
    return f"{_ASSISTANT_NAME}, {s[0].lower() + s[1:]}"


def _address_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**st, "say": _address(st.get("say", ""))} for st in steps]


def _match(topic: str, wf: dict[str, Any]) -> bool:
    t = topic.lower()
    return any(k in t or t in k for k in wf["keywords"]) or t in wf["title"].lower()


def guide(topic: str = "") -> dict[str, Any]:
    """The capability guide. topic in ANY language (the chat brain translates intent; the
    keywords carry English + Polish directly). Empty topic = the overview."""
    topic = (topic or "").strip()
    workflows = _workflows()
    note = ("Ask in any language, any phrasing. These example utterances work verbatim, but "
            "natural variations of them work too; this list is the map, not a command syntax. They "
            "open with 'Mongoose,' so a copied line reads as a command; the name is optional in chat.")
    if not topic:
        return {"ok": True, "note": note,
                "workflows": [{"id": w["id"], "title": w["title"], "when": w["when"],
                               "first_thing_to_say": _address(w["steps"][0]["say"])} for w in workflows],
                "hint": "Ask about any of these by name or in your own words for the full steps."}
    hits = [w for w in workflows if _match(topic, w)]
    # Also search the live tool registry so anything added later is discoverable without a recipe.
    try:
        from ..tools import rendered_tools
        tool_hits = [{"tool": name, "description": spec.get("description", "")[:220]}
                     for name, spec in rendered_tools().items()
                     if topic.lower() in (spec.get("description", "") + name).lower()]
    except Exception:
        tool_hits = []
    if not hits and not tool_hits:
        return {"ok": True, "note": note, "matched": [],
                "workflows": [{"id": w["id"], "title": w["title"]} for w in workflows],
                "hint": f"Nothing matched '{topic}' directly; here is everything. Ask again in "
                        "other words and I'll map it."}
    return {"ok": True, "note": note,
            "matched": [{"id": w["id"], "title": w["title"], "when": w["when"],
                         "steps": _address_steps(w["steps"])} for w in hits],
            "related_tools": tool_hits[:6]}
