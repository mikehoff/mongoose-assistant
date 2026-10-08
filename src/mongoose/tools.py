from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from .db import audit
from .llm import ask_ollama, ask_persona
from .services.calendar_tasks import availability
from .services.email_index import search_email
from .services.home_router import HomeCommand, route_home_command
from .services.notifications import search_notifications
from .services.status_tools import (
    tool_system_status as _tool_system_status,
    tool_consistency_check as _tool_consistency_check,
    tool_rtm_statement as _tool_rtm_statement,
    tool_rtm_ontology as _tool_rtm_ontology,
    tool_rtm_knowledge as _tool_rtm_knowledge,
    tool_rtm_mark_paid as _tool_rtm_mark_paid,
    tool_rtm_send_receipt as _tool_rtm_send_receipt,
    tool_rtm_on_account as _tool_rtm_on_account,
    tool_rtm_demand_schedule as _tool_rtm_demand_schedule,
    tool_rtm_plan_letter as _tool_rtm_plan_letter,
    tool_rtm_demand_mailing as _tool_rtm_demand_mailing,
    tool_recent_activity as _tool_recent_activity,
)
from .services.weather import weather_summary
from . import profile as _profile
from .role_router import owner_name_rx as _owner_name_rx, rtm_name_rx as _rtm_name_rx

# --- Tool allowlist ---------------------------------------------------------
#
# A tool turn may only call functions registered here. This is the policy
# boundary: the model proposes a call, Mongoose decides whether to run it.
# Nothing destructive or externally visible is exposed. In particular there is
# no email send/delete, no calendar write, no purchase, matching the read-first
# security model. Home control is the only "act" tool and it inherits the
# home_router privacy gate (Alexa fallback blocked for non-low privacy).
#
# Each tool declares a JSON-able arg schema (for the prompt) and an async
# executor. Executors must be safe to call with model-supplied args: validate,
# never trust blindly.

ToolExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def _tool_weather(args: dict[str, Any]) -> dict[str, Any]:
    force = bool(args.get("force", False))
    data = await weather_summary(force=force)
    return {"voice": data.get("voice"), "current": data.get("current"), "rain": data.get("rain"), "uv": data.get("uv")}


async def _tool_home(args: dict[str, Any]) -> dict[str, Any]:
    # "All the lights / every light / all switches": HA already knows every entity, so act on the whole
    # domain via HA's live list (panel-safety entities excluded), instead of resolving one best-match.
    _room = str(args.get("room") or "").lower()
    _device = str(args.get("device_id") or "").lower()
    _blob = f"{_room} {_device}".strip()
    _action = str(args.get("action") or "").lower()
    if any(w in _blob for w in ("all ", "every", "all the", "all lights", "all the lights")) or _blob in ("all", "everything"):
        from .services.home_router import control_all_in_domain
        # infer domain from the words; default to light (the overwhelmingly common "turn everything off")
        domain = "light"
        if "switch" in _blob:
            domain = "switch"
        elif "fan" in _blob:
            domain = "fan"
        act = "off" if any(w in _action for w in ("off", "turn_off")) else "on"
        return await control_all_in_domain(domain, act)
    # INTENT VIA HA ASSIST (primary): if we have the user's raw prompt, hand it to Home Assistant's own
    # conversation engine, which reconstructs intent from ambiguous phrasing using its knowledge of
    # areas, device aliases and groups , far better than Mongoose guessing an entity_id. HA actuates and
    # tells us what it did. We fall back to the explicit resolver path if HA Assist can't help or flags a
    # panel-safety risk. The bulk 'all' path above already handled blanket sweeps panel-safely.
    raw_prompt = str(args.get("_raw_prompt") or "").strip()
    if raw_prompt:
        from .services.home_router import control_via_ha_assist
        assist = await control_via_ha_assist(raw_prompt)
        if assist.get("ok"):
            return assist
        # Only fall through to the resolver when HA Assist explicitly invites a fallback (unreachable,
        # no match, panel-safety). A genuine HA error that isn't fall_back is returned as-is.
        if not assist.get("fall_back"):
            return assist
    # Build a HomeCommand from model args. Pydantic validates types and
    # supplies safe defaults; privacy defaults to "medium" when the model omits
    # it (the safer default), and the home_router blocks Alexa for non-low privacy.
    try:
        command = HomeCommand(**{
            k: v for k, v in args.items()
            if k in {"room", "device_id", "action", "entity_id", "domain", "service", "privacy", "payload"}
        })
    except Exception as exc:
        return {"ok": False, "error": f"invalid home command: {exc}"}
    # The tool model usually gives room/device words, not a real entity_id. If no
    # entity_id was supplied, resolve it against the live HA entity list and fill
    # in the correct domain/service. A low-confidence match is reported back with
    # candidates instead of firing the wrong device.
    if not command.entity_id:
        from .services.entity_resolver import resolve_entity_with_model, HAUnreachable
        try:
            match = await resolve_entity_with_model(command.room, command.device_id, command.action)
        except HAUnreachable as exc:
            return {"ok": False, "error": f"home_assistant_unreachable: {exc}"}
        if match is None:
            return {"ok": False, "error": "no Home Assistant entities available to match"}
        if not match.get("entity_id"):
            return {"ok": False, "error": "no confident device match",
                    "candidates": match.get("candidates", [])}
        command.entity_id = match["entity_id"]
        command.domain = match["domain"]
        command.service = match["service"]
    else:
        # entity_id was supplied directly (e.g. via MCP, whose control_home schema exposes
        # entity_id + action but NOT domain/service). If domain/service are still the bare
        # defaults, infer them: domain from the entity_id prefix, service from the action.
        # Without this, "{entity_id: light.lounge, action: turn_off}" stays
        # homeassistant.turn_on and fails the allowlist + the live call. (QA-audit P0.2)
        command = _infer_domain_service(command)
    return await route_home_command(command)


# Map a friendly action word to a Home Assistant service. Conservative: unknown actions are
# left to the explicit service (or the default), and only safe control verbs are mapped.
_ACTION_TO_SERVICE = {
    "turn_on": "turn_on", "on": "turn_on", "turn off": "turn_off", "turn_off": "turn_off",
    "off": "turn_off", "toggle": "toggle", "open": "open_cover", "close": "close_cover",
    "lock": "lock", "unlock": "unlock", "start": "turn_on", "stop": "turn_off",
}


def _infer_domain_service(command: HomeCommand) -> HomeCommand:
    """When entity_id is supplied but domain/service weren't (still the defaults), infer them
    from the entity_id prefix and the action. Returns the command (possibly updated). Leaves
    an explicitly-set non-default domain/service untouched."""
    eid = command.entity_id or ""
    if "." in eid:
        inferred_domain = eid.split(".", 1)[0]
        # only override the bare default, never an explicit choice
        if command.domain == "homeassistant":
            command.domain = inferred_domain
    # map the action to a service only if service is still the bare default
    if command.service == "turn_on":
        act = (command.action or "").strip().lower()
        mapped = _ACTION_TO_SERVICE.get(act)
        if mapped:
            command.service = mapped
    return command


async def _tool_calendar_availability(args: dict[str, Any]) -> dict[str, Any]:
    start_at = args.get("start_at")
    end_at = args.get("end_at")
    if not start_at or not end_at:
        return {"ok": False, "error": "start_at and end_at are required ISO timestamps"}
    return availability(str(start_at), str(end_at))


async def _tool_get_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Read synced Microsoft To Do/local task data. Supports optional list/folder filtering."""
    from .services.calendar_tasks import list_tasks, task_lists
    try:
        limit = int(args.get("limit") or 20)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 200))
    include_completed = bool(args.get("include_completed", False))
    list_name = str(args.get("list_name") or args.get("folder") or "").strip().lower()
    tasks = list_tasks(limit=200 if list_name else limit, include_completed=include_completed)
    lists = task_lists()
    if list_name:
        tasks = [t for t in tasks if str(t.get("list_name") or "Inbox").strip().lower() == list_name]
        lists = [l for l in lists if str(l.get("name") or "Inbox").strip().lower() == list_name]
    return {"ok": True, "lists": lists, "tasks": tasks[:limit],
            "count": len(tasks), "filtered_list": list_name or None}


async def _tool_list_tasks(args: dict[str, Any]) -> dict[str, Any]:
    return await _tool_get_tasks(args)


async def _tool_get_calendar(args: dict[str, Any]) -> dict[str, Any]:
    """Read synced calendar data from the local index."""
    from .services.calendar_tasks import events_today, upcoming_events
    try:
        days = int(args.get("days") or 14)
    except Exception:
        days = 14
    try:
        limit = int(args.get("limit") or 20)
    except Exception:
        limit = 20
    today = events_today()
    upcoming = upcoming_events(days=days, limit=limit)
    return {"ok": True, "today": today, "upcoming": upcoming,
            "today_count": len(today), "upcoming_count": len(upcoming)}


async def _tool_list_calendar(args: dict[str, Any]) -> dict[str, Any]:
    return await _tool_get_calendar(args)


async def _tool_search_email(args: dict[str, Any]) -> dict[str, Any]:
    q = args.get("query") or args.get("q")
    if not q:
        return {"ok": False, "error": "query is required"}
    return {"results": search_email(str(q), int(args.get("limit", 10)))}


async def _tool_search_files(args: dict[str, Any]) -> dict[str, Any]:
    q = args.get("query") or args.get("q")
    if not q:
        return {"ok": False, "error": "query is required"}
    # Use the unified find so the chat tool loop searches files (all devices),
    # attachments AND email, matching the CLI `find` and the MCP tools. This keeps
    # every surface behaving the same instead of chat being file-only.
    from .services.find import find
    return find(str(q), limit=int(args.get("limit", 10)), surface="chat")


async def _tool_item_actions(args: dict[str, Any]) -> dict[str, Any]:
    """Describe what can be done with a persisted search result.

    Accepts item_ref (e.g. E1A2B3C), ordinal/result_number (e.g. first/2), and optional
    result_set_id. It is read-only: it does not open, download, delete or mutate anything.
    """
    from .services.item_model import describe_actions
    ref = args.get("item_ref") or args.get("ref")
    ordinal = args.get("ordinal") or args.get("result_number") or args.get("number")
    try:
        ordinal_i = int(ordinal) if ordinal not in (None, "") else None
    except (TypeError, ValueError):
        text = str(ordinal or "").lower()
        ordinal_i = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}.get(text)
    return describe_actions(str(ref) if ref else None, ordinal=ordinal_i,
                            result_set_id=args.get("result_set_id") or None)


async def _tool_read_document(args: dict[str, Any]) -> dict[str, Any]:
    """Read the FULL extracted text of ONE specific document/attachment, so any figure (amount, sort code,
    account number, date, reference) is quoted from the real file instead of guessed. This is the fix for
    the incident where the model INVENTED an invoice's bank details (sort 00-00-00 / account 12345678) that
    were nowhere in the actual PDF. Resolve by: item_ref or ordinal from a prior search (natural chat flow
    'open the first one'), OR directly by attachment_id / document_id, OR by filename."""
    from .services import item_model, attachments as A
    from .services import rtm_docs

    def _bank_check(text: str, payee_hint: str | None) -> dict[str, Any] | None:
        """Diff any bank details in the document against the trusted-payee registry (shared assessment), so
        the model warns from REAL history: new payee, unconfirmed, or CHANGED-since-confirmed (redirection
        fraud). Best-effort; never breaks the read."""
        try:
            from .services import rtm_management as RM
            return RM.assess_invoice_bank_details(text, payee_hint or "")
        except Exception:
            return None

    def _payload(kind: str, filename: str, content: str, **extra: Any) -> dict[str, Any]:
        content = (content or "").strip()
        out = {"ok": True, "kind": kind, "filename": filename, "content": content[:20000], **extra}
        if not content:
            out["note"] = ("No extracted text is stored for this item. Do NOT infer or invent its contents "
                           "(amounts, bank details, references); say you could not read it and offer to open "
                           "the source.")
            return out
        bc = _bank_check(content, extra.get("sender"))
        if bc:
            out["bank_check"] = bc
        return out

    # 1) direct attachment id
    aid = args.get("attachment_id")
    if aid is not None:
        try:
            rec = A.get_attachment_full(int(aid))
        except (TypeError, ValueError):
            rec = None
        if not rec:
            return {"ok": False, "error": f"no attachment with id {aid!r}"}
        return _payload("attachment", rec.get("filename") or "", rec.get("content_text") or "",
                        attachment_id=rec.get("id"), sender=rec.get("sender"), email_date=rec.get("email_date"))

    # 2) direct uploaded-document id
    did = args.get("document_id") or args.get("doc_id")
    if did is not None:
        try:
            d = rtm_docs.get_document(int(did))
        except (TypeError, ValueError):
            d = None
        if not d:
            return {"ok": False, "error": f"no document with id {did!r}"}
        return _payload("document", d.get("filename") or "", d.get("extracted_text") or "",
                        document_id=d.get("id"), doc_type=d.get("doc_type"))

    # 3) a persisted search result, by item_ref or ordinal
    ref = args.get("item_ref") or args.get("ref")
    ordinal = args.get("ordinal") or args.get("result_number") or args.get("number")
    try:
        ordinal_i = int(ordinal) if ordinal not in (None, "") else None
    except (TypeError, ValueError):
        ordinal_i = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}.get(str(ordinal).lower())
    if ref or ordinal_i is not None:
        item = item_model.resolve_item(str(ref) if ref else None, ordinal=ordinal_i,
                                       result_set_id=args.get("result_set_id") or None)
        if not item:
            return {"ok": False, "error": "item not found",
                    "hint": "Run a search first, then refer to a result number or item_ref."}
        snap = item.get("snapshot") or {}
        kind = item.get("kind") or "item"
        if kind == "attachment" and snap.get("attachment_id") is not None:
            rec = A.get_attachment_full(int(snap["attachment_id"]))
            if rec:
                return _payload("attachment", rec.get("filename") or item.get("title") or "",
                                rec.get("content_text") or "", attachment_id=rec.get("id"),
                                sender=rec.get("sender"), email_date=rec.get("email_date"))
        # file/email/other: fall back to whatever text the search snapshot captured, clearly labelled.
        return _payload(kind, item.get("title") or "", snap.get("content") or snap.get("snippet") or "",
                        item_ref=item.get("item_ref"), where=item.get("where_text"),
                        note="Only a search snippet is available for this item; open the source for full content.")

    # 4) by filename (best match among attachments)
    filename = (args.get("filename") or args.get("name") or "").strip()
    if filename:
        hits = A.search_attachments(filename, limit=5)
        if hits:
            rec = A.get_attachment_full(int(hits[0]["id"]))
            if rec:
                return _payload("attachment", rec.get("filename") or filename, rec.get("content_text") or "",
                                attachment_id=rec.get("id"), sender=rec.get("sender"),
                                email_date=rec.get("email_date"))
        return {"ok": False, "error": f"no document or attachment matching {filename!r}"}

    return {"ok": False, "error": "give me an item_ref, ordinal, attachment_id, document_id, or filename to read"}


async def _tool_search_notifications(args: dict[str, Any]) -> dict[str, Any]:
    q = args.get("query") or args.get("q")
    if not q:
        return {"ok": False, "error": "query is required"}
    return {"results": search_notifications(str(q), int(args.get("limit", 10)))}


async def _tool_set_reminder(args: dict[str, Any]) -> dict[str, Any]:
    """Create a reminder Mongoose actually owns: a stored notification the owner will see on the
    dashboard/phone. (Distinct from a client-side device alarm, which Mongoose can't set remotely.)
    An acting tool: it creates real, owner-visible state: so the injection guard applies."""
    text = (args.get("text") or args.get("message") or args.get("body") or "").strip()
    if not text:
        return {"ok": False, "error": "reminder text is required"}
    when = (args.get("when") or args.get("time") or "").strip()
    title = "Reminder" + (f": {when}" if when else "")
    body = text + (f"\n(for: {when})" if when else "")
    from .services.notifications import NotificationIn, store_notification
    res = store_notification(NotificationIn(
        source="mongoose", app="reminder", title=title, body=body,
        privacy="medium", urgency="normal", device="mongoose"))
    return {"ok": True, "created": res.get("inserted", True), "title": title,
            "detail": "Reminder saved; it'll show on the dashboard." if res.get("inserted")
                      else "A matching reminder already exists."}


async def _tool_message_person(args: dict[str, Any]) -> dict[str, Any]:
    """Send a message between the owner and a guest via the mailbox. An acting tool: it sends real
    messages to people: so the injection guard prevents untrusted content from triggering it.

    The loop passes the sender's profile in _sender; routines run as the owner. Recipient resolves:
    'owner' -> owner; 'everyone'/'all'/'guests' -> guest:* broadcast; otherwise the literal
    participant id (e.g. 'guest:<device>')."""
    body = (args.get("text") or args.get("message") or args.get("body") or "").strip()
    if not body:
        return {"ok": False, "error": "message text is required"}
    raw_to = (args.get("to") or args.get("recipient") or args.get("who") or "").strip().lower()
    from .services.mailbox import OWNER, send_message
    sender = args.get("_sender") or OWNER  # default owner (routines/cockpit run as owner)
    if raw_to in ("owner", "me", "") or raw_to == _profile.owner_name().strip().lower():
        recipient = OWNER
    elif raw_to in ("everyone", "all", "guests", "guest:*"):
        recipient = "guest:*"
    elif raw_to.startswith("guest:"):
        recipient = args.get("to") or args.get("recipient")  # already a participant id
    else:
        # A human name like "Jordan": resolve it to a paired guest device. Match the name
        # case-insensitively (device names are often "Jordan's iPhone", so substring-match the token).
        resolved = _resolve_person_to_participant(raw_to)
        if not resolved:
            return {"ok": False, "error": f"no paired device matches '{raw_to}'. "
                    "Pair their phone first, or send to 'everyone'."}
        recipient = resolved
    res = send_message(sender=str(sender), recipient=str(recipient), body=body)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "could not send")}
    # Honest delivery semantics: the message is STORED and the recipient notified. Only say it
    # will arrive now if the recipient's app is actually reachable; otherwise it waits for them
    # to open Mongoose. Never claim "delivered" for something that was only saved.
    from .services.mailbox import recipient_is_active, name_for
    rname = name_for(str(recipient)) if recipient != "guest:*" else "everyone"
    if recipient == "guest:*":
        detail = f"Saved for {rname}; each will see it next time they open Mongoose."
    elif recipient_is_active(str(recipient)):
        detail = f"Saved and {rname} notified; should reach them now."
    else:
        detail = f"Saved for {rname}; they'll see it next time they open Mongoose."
    return {"ok": True, "sent_to": recipient, "saved": True, "detail": detail}


async def _tool_check_messages(args: dict[str, Any]) -> dict[str, Any]:
    """Read this person's unread direct messages from the mailbox. The loop passes the
    caller's identity in _sender ('owner' or 'guest:<device>'). This is the READ side of
    person-to-person messaging: it returns the actual stored messages so the brain never
    has to guess whether someone wrote, and marks them read so they don't re-alert."""
    from .services import mailbox as mb
    who = args.get("_sender") or mb.OWNER
    unread = mb.unread_for(str(who))
    if not unread:
        return {"ok": True, "count": 0, "messages": [], "detail": "No new messages."}
    items = [{"from": mb.name_for(m["sender"]), "body": m["body"], "at": m.get("created_at")}
             for m in unread]
    mb.mark_read(participant=str(who))
    return {"ok": True, "count": len(items), "messages": items}


def _resolve_person_to_participant(name: str) -> str | None:
    """Map a human name (e.g. 'sam') to a paired guest's participant id ('guest:<device_id>').
    Matches the name token against each paired device's stored name, case-insensitively. Returns
    None if there's no unambiguous match (so the tool reports it rather than guessing wrong)."""
    name = (name or "").strip().lower()
    if not name:
        return None
    try:
        from .devices import list_devices
        devices = list_devices()
    except Exception:
        return None
    matches = []
    for d in devices:
        if d.get("revoked"):
            continue
        # Only trusted guests have private mailbox threads, so only they are valid message targets.
        if d.get("profile") != "trusted_guest":
            continue
        dev_name = (d.get("name") or "").lower()
        # match if the requested name is a word/substring of the device name ("sam" in "sam's iphone")
        if name and (name in dev_name or dev_name.startswith(name)):
            matches.append(d.get("device_id"))
    # Only resolve when exactly one device matches, to avoid messaging the wrong person.
    if len(matches) == 1 and matches[0]:
        return f"guest:{matches[0]}"
    return None


def _recommender_identity(sender: str) -> tuple[str, str, str]:
    """Who is making the recommendation. Returns (scope, uid, display_name).
    sender is the tool loop's _sender: 'owner' for the owner, else 'guest:<device_id>'. A guest is
    resolved to their account (by device link, then by the device's owner_label) so we can attribute
    the recommendation by name and exclude them from an 'everyone' fan-out."""
    from .services.auth_accounts import owner_account, account_for_device, get_account, find_user
    from .services.music_taste import OWNER_SCOPE
    sender = (sender or "owner").strip()
    if sender in ("", "owner"):
        acct = owner_account() or {}
        return OWNER_SCOPE, (acct.get("user_id") or ""), (acct.get("display_name") or _profile.owner_name())
    device_id = sender.split(":", 1)[1] if ":" in sender else sender
    uid = ""
    try:
        uid = account_for_device(device_id) or ""
    except Exception:
        uid = ""
    if not uid:
        # Unlinked guest device: recover the person via the owner_label the phone thread is keyed on.
        try:
            from .db import connect
            with connect() as con:
                row = con.execute("SELECT owner_label FROM guest_profiles WHERE device_id=?",
                                  (device_id,)).fetchone()
            label = (row["owner_label"] if row else "") or ""
            acct = find_user(label) if label else None
            if acct:
                return (acct.get("user_id") or ""), (acct.get("user_id") or ""), (acct.get("display_name") or label)
            if label:
                return f"guest:{device_id}", "", label
        except Exception:
            pass
        return f"guest:{device_id}", "", "a guest"
    acct = get_account(uid) or {}
    return uid, uid, (acct.get("display_name") or "a guest")


def _rec_target_for_name(name: str) -> dict[str, Any] | None:
    """Resolve a person's NAME to a music-delivery target: {scope, uid, display}. Owner or a named
    guest. The caller turns scope into the exact chat thread via _conversation_for_scope, so we land
    on the person thread their phone actually reads (guest:p:<label>). None if no confident match."""
    from .services.auth_accounts import owner_account, find_user, list_users
    from .services.music_taste import OWNER_SCOPE
    name_l = (name or "").strip().casefold()
    if not name_l:
        return None
    owner = owner_account() or {}
    owner_uid = owner.get("user_id") or ""
    owner_display = owner.get("display_name") or _profile.owner_name()
    if name_l in ("owner", "me", _profile.owner_name().strip().casefold()) or name_l == owner_display.casefold() \
            or owner_display.casefold().startswith(name_l):
        return {"scope": OWNER_SCOPE, "uid": owner_uid, "display": owner_display}
    # Exact account match first (username or display name), then a confident fuzzy fall-back.
    acct = find_user(name)
    if acct and acct.get("user_id"):
        uid = acct["user_id"]
        if uid == owner_uid:
            return {"scope": OWNER_SCOPE, "uid": owner_uid, "display": owner_display}
        return {"scope": uid, "uid": uid, "display": acct.get("display_name") or name}
    # Fuzzy: a single account whose display name shares the token/prefix (handles 'jor' for 'Jordan').
    hits = []
    for u in list_users():
        disp = (u.get("display_name") or "").casefold()
        if not disp:
            continue
        if disp == name_l or disp.startswith(name_l) or name_l.startswith(disp) or name_l in disp.split():
            hits.append(u)
    if len(hits) == 1 and hits[0].get("user_id"):
        u = hits[0]
        if u["user_id"] == owner_uid:
            return {"scope": OWNER_SCOPE, "uid": owner_uid, "display": owner_display}
        return {"scope": u["user_id"], "uid": u["user_id"], "display": u.get("display_name") or name}
    return None


def _rec_everyone_targets(exclude_scope: str, exclude_uid: str) -> list[dict[str, Any]]:
    """Every known paired person (owner + accounts) as delivery targets, minus the recommender.
    Used when the recommendation is 'to everyone/all/guests'."""
    from .services.auth_accounts import owner_account, list_users
    from .services.music_taste import OWNER_SCOPE
    targets: list[dict[str, Any]] = []
    owner = owner_account() or {}
    owner_uid = owner.get("user_id") or ""
    if exclude_scope != OWNER_SCOPE:
        targets.append({"scope": OWNER_SCOPE, "uid": owner_uid,
                        "display": owner.get("display_name") or _profile.owner_name()})
    for u in list_users(exclude_user_id=exclude_uid or ""):
        uid = u.get("user_id") or ""
        if not uid or uid == owner_uid or uid == exclude_uid:
            continue
        targets.append({"scope": uid, "uid": uid, "display": u.get("display_name") or u.get("username") or "someone"})
    return targets


async def _tool_recommend_song(args: dict[str, Any]) -> dict[str, Any]:
    """Pass a song someone recommends on to the person (or people) it is FOR. See the TOOLS entry for
    trigger phrasing. Never raises: any failure returns a friendly {ok: False, error}."""
    try:
        song = (args.get("song") or args.get("track") or args.get("title") or "").strip()
        if not song:
            return {"ok": False, "error": "which song is it? give me something like 'Blackbird by The Beatles'."}
        raw_to = (args.get("to") or args.get("recipient") or args.get("who") or args.get("for") or "").strip()
        if not raw_to:
            return {"ok": False, "error": "who is it for? a name, or 'everyone'."}
        note = (args.get("note") or args.get("message") or "").strip()
        sender = args.get("_sender") or "owner"

        from .services import music_taste as MT
        from .services import song_exchange as SX
        from .services.memory import add_message
        from .services.scheduler import _conversation_for_scope

        from_scope, from_uid, from_name = _recommender_identity(sender)

        to_l = raw_to.casefold()
        if to_l in ("everyone", "all", "everybody", "guests", "the guests", "everyone else"):
            targets = _rec_everyone_targets(from_scope, from_uid)
            fanout = True
        else:
            one = _rec_target_for_name(raw_to)
            if not one:
                return {"ok": False, "error": f"I could not find anyone called '{raw_to}' to pass that to. "
                        "Try their name as you paired them, or say 'everyone'."}
            targets = [one]
            fanout = False

        # Never recommend a song back to the person who is recommending it.
        targets = [t for t in targets if t.get("scope") != from_scope]
        if not targets:
            return {"ok": False, "error": "there was no one else to share that with."}

        delivered: list[str] = []
        for t in targets:
            scope = t["scope"]
            display = t.get("display") or "them"
            conv = None
            try:
                conv = _conversation_for_scope(scope)
            except Exception:
                conv = None
            if not conv:
                # No resolvable thread means the message would strand where nobody looks. Skip, do not
                # pretend it landed.
                continue
            msg = MT.direct_recommendation_text(from_name, song, note)
            try:
                add_message(conv, "assistant", msg)
            except Exception:
                continue  # could not place it in their thread, so do not claim delivery or push
            # Record it durably so it shows in history and cannot silently vanish.
            try:
                SX.record_direct_recommendation(from_user=from_uid or from_scope, from_name=from_name,
                                                to_scope=scope, to_name=display, song=song, note=note)
            except Exception:
                pass
            # Best-effort personal push. No account-bound subscription just means it waits in-chat.
            uid = t.get("uid") or ""
            if uid:
                try:
                    from .services.notifications import send_to_phone
                    await send_to_phone(
                        MT.direct_recommendation_push_title(),
                        MT.direct_recommendation_push_body(from_name, song),
                        target_user_id=uid, click="/device/launchpad#talk",
                        tags=["musical_note"], priority="default",
                        log_source="music", log_app="Mongoose")
                except Exception:
                    pass
            delivered.append(display)

        if not delivered:
            return {"ok": False, "error": f"I could not reach anyone to pass '{song}' to just now."}
        if fanout or len(delivered) > 1:
            names = ", ".join(delivered)
            return {"ok": True, "song": song, "delivered_to": delivered,
                    "detail": f"Shared '{song}' with {len(delivered)} "
                              f"{'people' if len(delivered) != 1 else 'person'} ({names})."}
        return {"ok": True, "song": song, "delivered_to": delivered,
                "detail": f"Passed '{song}' to {delivered[0]}."}
    except Exception as e:
        return {"ok": False, "error": f"could not pass that song on ({e})."}


# Tools that read potentially untrusted content (email/files/notifications).
# After any of these run in a turn, an acting tool is only allowed if the user's
# original prompt explicitly asked for it (prevents tool-result prompt injection
# steering the planner into a home-control action).
READ_TOOLS = {"web_search", "search_email", "search_files", "search_notifications", "knowledge_imports",
              "list_tasks", "list_calendar", "get_tasks", "get_calendar",
              "what_changed", "whatsapp_search", "ritual", "research", "item_actions", "read_document",
              "system_status", "connector_status", "home_state", "search_voicemails", "wp_plugin_status", "self_reflection", "v3_coordination", "consistency_check", "rtm_statement", "rtm_documents", "rtm_bank_feed_playbook", "rtm_build_source", "body_data", "knowledge_imports", "guest_privacy", "conversation_continuity", "recent_activity", "check_messages", "bug_status", "capability_guide", "capability_search"}
ACTING_TOOLS = {"control_home", "wp_plugin_action", "set_reminder", "message_person", "recommend_song", "whatsapp_draft", "wellness_checkins", "log_bug", "add_bug_detail", "diagnose_bug", "attempt_bug_fix", "apply_bug_fix", "data_dump"}
# Sensitive tools MUTATE durable state (apply an update, run a scan, restart a loop, rebuild a pack).
# They are NEVER executed directly by the planner: the loop returns a proposal + confirmation token and
# stops; the user must explicitly confirm in a follow-up turn (see confirm_sensitive_action). Each calls
# the SAME gated function the CLI uses , chat gets no weaker path.
SENSITIVE_TOOLS = {"self_patch", "run_hardening_scan", "check_update", "apply_update", "run_due_jobs", "scheduler_control", "wp_plugin_action", "attempt_bug_fix", "apply_bug_fix", "data_dump", "rtm_demand_mailing", "rtm_plan_letter", "rtm_on_account", "rtm_demand_schedule",
                   # Outward/social actions: sharing a track to a real person, sending a message, drafting a
                   # WhatsApp. These reach OTHER people, so a model misfire must be a harmless "no", not a
                   # broadcast. They go through the same propose + confirm-token gate as the rest, so nothing
                   # is sent until the owner explicitly confirms.
                   "recommend_song", "message_person", "whatsapp_draft"}

async def _tool_research(args: dict[str, Any]) -> dict[str, Any]:
    """Call the SILENT research worker (a specialist model exposed as a tool, per the
    supervisor/specialist pattern). It returns STRUCTURED NOTES for the conversation
    owner to phrase, never a finished user-facing answer. Read-only: it cannot speak,
    send, or act. The owner (this chat loop) turns the notes into the reply."""
    q = args.get("query") or args.get("q") or args.get("question")
    if not q:
        return {"ok": False, "error": "query is required"}
    from .research_worker import research_worker, format_notes_for_owner
    notes = await research_worker(str(q), context=args.get("context"))
    # Return both the structured notes and an owner-facing evidence block. The block
    # is explicitly labelled as notes-to-phrase, not a reply to show verbatim.
    return {"ok": True, "notes": notes, "for_owner": format_notes_for_owner(notes),
            "worker_model": notes.get("worker_model")}


def _tool_what_changed(args: dict[str, Any]) -> dict[str, Any]:
    from .services.journal_layer import what_changed
    return what_changed(acknowledge=not bool(args.get("peek")))


def _tool_ritual(args: dict[str, Any]) -> dict[str, Any]:
    from .services.rituals import start_ritual, list_rituals
    name = args.get("name")
    if not name:
        return {"rituals": list_rituals()}
    return start_ritual(str(name))


def _tool_whatsapp_search(args: dict[str, Any]) -> dict[str, Any]:
    from .services.whatsapp import search_messages
    q = args.get("query") or args.get("q")
    if not q:
        return {"ok": False, "error": "query is required"}
    # Tool loop is a local authenticated surface, so unmasked; the model phrasing
    # layer is responsible for not reading private content onto shared outputs.
    return {"results": search_messages(str(q), int(args.get("limit", 10)))}


def _tool_whatsapp_draft(args: dict[str, Any]) -> dict[str, Any]:
    # DRAFT ONLY. There is deliberately no send tool in the allowlist, so the model
    # can compose a check-in but can never send it; sending needs human approval.
    from .services.whatsapp import draft_message
    contact = args.get("contact")
    body = args.get("body")
    if not contact or not body:
        return {"ok": False, "error": "contact and body are required"}
    d = draft_message(str(contact), str(body))
    d["note"] = "draft only; the user must approve before this is sent"
    return d


def _user_asked_for_home_control(prompt: str) -> bool:
    # Single source of truth: the same detector the role classifier uses, so
    # /assist, /llm/chat and `mongoose chat` agree on what counts as home control.
    from .role_router import looks_like_home_control
    return looks_like_home_control(prompt)


def _user_explicitly_requested_action(prompt: str, tool_name: str) -> bool:
    """Whether an acting tool after a read tool was asked for by the user, not injected by content.

    The old guard only understood home-control actions. That was safe but too narrow once Mongoose had
    other acting-but-gated tools (message drafts, reminders, RTM plugin work). Keep the guard strict,
    but make the allowed case match the actual acting tool.
    """
    import re as _re
    low = (prompt or "").lower()
    if tool_name == "control_home":
        return _user_asked_for_home_control(prompt)
    if tool_name == "set_reminder":
        return bool(_re.search(r"\b(remind me|set (a )?(timer|alarm|reminder)|reminder for)\b", low))
    if tool_name == "message_person":
        return bool(_re.search(r"\b(send|message|text|tell|let|leave|pass on|pass along)\b", low))
    if tool_name == "whatsapp_draft":
        return bool(_re.search(r"\b(whatsapp|message|text)\b.{0,60}\b(draft|compose|write)\b|\b(draft|compose|write)\b.{0,60}\b(whatsapp|message|text)\b", low))
    if tool_name == "wp_plugin_action":
        _rtm = _rtm_name_rx(" ?")
        return bool(_re.search(rf"\b(build|deploy|activate|deactivate|promote|scaffold)\b.{{0,80}}\b(rtm|{_rtm}|wordpress|wp|plugin|site)\b|\b(rtm|{_rtm}|wordpress|wp|plugin|site)\b.{{0,80}}\b(build|deploy|activate|deactivate|promote|scaffold)\b", low))
    if tool_name == "wellness_checkins":
        return bool(_re.search(r"\b(self[- ]?journal|mindfulness|wellness|check[- ]?in|journal)\b.{0,120}\b(remind|nudge|set up|schedule|guests?)\b|\b(remind|nudge|set up|schedule)\b.{0,120}\b(self[- ]?journal|mindfulness|wellness|check[- ]?in|journal|guests?)\b", low))
    if tool_name in {"log_bug", "add_bug_detail", "diagnose_bug", "attempt_bug_fix", "apply_bug_fix"}:
        return bool(_re.search(r"\b(log bug|add bug detail|diagnose bug|attempt fix|apply fix|bug status)\b", low) or _re.search(r"\b[a-z]\d{2}\b", low))
    return False


async def _sensitive_placeholder(args: dict[str, Any]) -> dict[str, Any]:
    # Sensitive tools never reach their executor: run_tool_turn intercepts them before execution and
    # returns a proposal instead (see SENSITIVE_TOOLS guard). This exists only so the registry entry is
    # well-formed; reaching it would be a logic error.
    return {"ok": False, "error": "sensitive tool must go through propose/confirm, not direct execution"}


async def _tool_confirm_sensitive(args: dict[str, Any]) -> dict[str, Any]:
    from .services.sensitive_tools import confirm_sensitive_action_async
    return await confirm_sensitive_action_async(args.get("token") or "")




async def _tool_rtm_build_source(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only RTM product-build provenance and source-of-truth map for UI/chat/v3/Claude paths."""
    from .services.rtm_build_source import source_of_truth_status
    return source_of_truth_status()


async def _tool_body_data(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only: what training/nutrition data Mongoose has, and what (if anything) it's asking for.

    Use when the owner asks 'what data do you need?', 'do you have my training data?', 'how do I give
    you my Alpha export / MyFitnessPal data?'. Returns the data status plus the current upload request
    (forced, so it answers even if the chat nudge timer hasn't elapsed). It does NOT import anything;
    importing happens through the cockpit Body data card or a phone upload.
    """
    from .services.body_data_nudge import nudge_status
    return nudge_status()

async def _tool_wellness_checkins(args: dict[str, Any]) -> dict[str, Any]:
    """Configure/run private structured mindfulness and wellness self-journal check-ins."""
    from .services.wellness_checkins import configure_checkins, log_checkin_response, recent, run_checkin_cycle, status
    action = str(args.get("action") or "configure").strip().lower().replace(" ", "_")
    if action in {"configure", "enable", "set_up", "setup"}:
        return configure_checkins(
            enabled=not bool(args.get("disabled", False)),
            owner=bool(args.get("owner", True)),
            guests=bool(args.get("guests", True)),
            hour=args.get("hour"),
            minute=args.get("minute"),
            owner_prompt=args.get("owner_prompt") or args.get("prompt"),
            guest_prompt=args.get("guest_prompt"),
            source="chat",
        )
    if action in {"status", "show"}:
        out = status()
        out["recent"] = recent(10)
        return out
    if action in {"run", "remind_now", "nudge_now"}:
        return await run_checkin_cycle(source="chat", force=True)
    if action in {"log", "complete", "done"}:
        return log_checkin_response(audience=str(args.get("audience") or "owner"), text=str(args.get("text") or "completed"))
    return {"ok": False, "error": "action must be configure/status/run/log"}


async def _tool_log_bug(args: dict[str, Any]) -> dict[str, Any]:
    from .services.bug_workflow import log_bug
    return log_bug(str(args.get("text") or args.get("description") or ""))


async def _tool_add_bug_detail(args: dict[str, Any]) -> dict[str, Any]:
    from .services.bug_workflow import add_bug_detail, parse_bug_id
    bug_id = str(args.get("bug_id") or parse_bug_id(str(args.get("text") or "")) or "")
    return add_bug_detail(bug_id, str(args.get("detail") or args.get("text") or ""))


async def _tool_diagnose_bug(args: dict[str, Any]) -> dict[str, Any]:
    from .services.bug_workflow import diagnose_bug, parse_bug_id
    text = str(args.get("detail") or args.get("text") or "")
    bug_id = str(args.get("bug_id") or parse_bug_id(text) or "")
    return await diagnose_bug(bug_id, text)


async def _tool_data_dump(args: dict[str, Any]) -> dict[str, Any]:
    """Create an owner-scoped data dump and return its id + download link. The 'kind' narrows what's
    exported (emails/tasks/notifications/diary/facts/bugs/whatsapp/events/journal) or 'everything'.
    Natural words like 'journal dump' or 'support dump' are understood."""
    from .services.data_dump import create_dump, available_kinds
    raw = str(args.get("kind") or args.get("text") or "").lower()
    kind = "everything"
    # recognise natural kind words first (journal/support/wellness), then the canonical kind names
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


async def _tool_bug_status(args: dict[str, Any]) -> dict[str, Any]:
    from .services.bug_workflow import bug_status, parse_bug_id
    text = str(args.get("text") or "")
    return bug_status(str(args.get("bug_id") or parse_bug_id(text) or "") or None, int(args.get("limit", 10)))


async def _tool_guest_privacy(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only, redacted report on guest isolation and owner/guest privacy boundaries."""
    from .services.guest_privacy import guest_privacy_report
    return guest_privacy_report()


async def _tool_conversation_continuity(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only, redacted report on owner/guest conversation continuity and replay health."""
    from .services.conversation_continuity import continuity_report
    return continuity_report()


async def _tool_rtm_bank_feed_playbook(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only RTM Metro bank-feed playbook/status/provider guidance.

    Modes:
      - status/overview: account inventory, tests, next actions and guardrails
      - provider: one provider's playbook
      - providers: ranked provider list
      - messages: support-message templates
    """
    from .services import rtm_bank_feed_playbook as P
    mode = str(args.get("mode") or args.get("view") or "status").strip().lower()
    provider = args.get("provider")
    if provider:
        return {"ok": True, "mode": "provider", "playbook": P.provider_playbook(str(provider))}
    if mode in {"provider", "providers", "list"}:
        return {"ok": True, "mode": "providers", **P.provider_playbook()}
    if mode in {"messages", "templates"}:
        return {"ok": True, "mode": "messages", "templates": P.list_message_templates()}
    if mode in {"evidence", "evidence_pack"}:
        return {"ok": True, "mode": "evidence", "requirements": P.evidence_pack_requirements()}
    if mode in {"matrix", "accounts"}:
        return {"ok": True, "mode": "matrix", "account_type_matrix": P.account_type_matrix(), "accounts": P.list_accounts()}
    return {"ok": True, "mode": "status", "status": P.playbook_status(), "summary": P.format_status()}


async def _tool_connector_status(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only LIVE connector health: actually probes each connector (Gmail, Calendar, Drive,
    Microsoft, IMAP, Home Assistant, Ollama) to report whether it WORKS right now, not just whether
    it's configured. Pass name=<connector> to probe one, or omit for all. Use for 'is Gmail working',
    'are my connectors ok', 'why isn't my calendar syncing', 'check home assistant'."""
    from .connector_probes import (connector_journey, probe_all, probe_one, probe_names,
                                   verify_connector, sync_connector, sync_all_read_only)
    action = (args.get("action") or "").strip().lower()
    name = (args.get("name") or args.get("connector") or "").strip().lower()
    # alias resolution (shared with the action path)
    alias = {"calendar": "google_calendar", "google calendar": "google_calendar",
             "drive": "google_drive", "google drive": "google_drive", "backups": "google_drive",
             "to do": "microsoft", "todo": "microsoft", "outlook": "microsoft",
             "ha": "home_assistant", "home assistant": "home_assistant",
             "mail": "imap", "email": "imap", "brain": "ollama", "llm": "ollama"}
    if name:
        name = alias.get(name, name)
    # ACTIONS: re-verify or pull fresh read-only data , this is how the brain ACTS, not just reports.
    if action in {"sync", "refresh", "pull", "update"}:
        return sync_all_read_only() if not name else sync_connector(name)
    if action in {"verify", "recheck", "re-check", "test", "check"}:
        if not name:
            return probe_all()
        return verify_connector(name)
    if action in {"journey", "journeys", "lifecycle", "setup", "walkthrough", "flow"}:
        return connector_journey(only=[name] if name else None)
    if name:
        if name not in probe_names():
            return {"ok": False, "error": f"unknown connector '{name}'", "known": probe_names()}
        return {"ok": True, "connector": probe_one(name).to_dict()}
    agg = probe_all()
    sm = agg["summary"]
    return {"ok": True, "summary": sm, "connectors": agg["connectors"],
            "working": [c["name"] for c in agg["connectors"] if c["usable"] is True],
            "broken": sm.get("broken_names", [])}


async def _tool_self_reflection(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only self-audit of the single brain's own wiring: are tools that exist actually reachable +
    read-only-registered, in the planner catalogue, and are all status areas callable. Detects a
    capability that's present in code but not wired to chat (the exact class of gap that needed a human
    to spot before). With apply=true it ALSO applies bounded, reversible auto-fixes (only runtime
    read-only-tool registration), it never writes code. Use for 'check your own wiring', 'are all your
    tools reachable', 'self-reflect', 'audit yourself'."""
    from .services.self_reflection import reflect
    apply = bool(args.get("apply") or args.get("fix"))
    return reflect(apply_fixes=apply)


_V3_EXPLAINER = (
    "Protocol v3 is the household coordination protocol that lets three agents (Chat Claude, Code "
    "Claude and Mongoose) work on the same Home Assistant configuration on the Raspberry Pi without "
    "colliding. They share an append-only YAML message bus (/config/claude_bus.yaml) and a lock "
    "directory, so each can announce work, hand off, and take a lock before any shared write. Each "
    "agent owns part of the config (Chat owns automations and scripts; Code owns packages and "
    "dashboards; Mongoose owns the mongoose namespace) and configuration.yaml is shared via a lock. It "
    "is M-gated: until PROTOCOL_V3_ENABLED is true, Mongoose only READS the bus and never writes. It "
    "coordinates the three agents but must never bypass build approval or treat a relayed approval as "
    "{{owner.name}}'s approval."
)


async def _tool_v3_coordination(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only view of household coordination (protocol v3): what it IS, plus the shared-bus catch-up state, the RTM's protocol-v3 voicemail staging, the protocol-version check, and an explicit dry-run handshake test.
    mode: status (default) / explain / bus / voicemail / reconcile / version / handshake. 'explain' says what protocol v3 is in plain language. 'reconcile' catches up +
    acks + heartbeats but takes NO household action (those stay M-approval-gated). 'handshake' tests the
    Pi transport/peer contract without changing household state. Use for 'what is v3', 'do you know what v3 is', 'explain protocol v3', 'check the bus', 'check the v3 voicemail staging', 'are the other agents there', 'test v3 readiness', 'catch up on coordination'."""
    from .services import protocol_v3_runtime as RT
    from .services.protocol_v3 import handshake_test, supports_v3_report
    mode = str(args.get("mode") or "status").strip().lower()
    if mode in {"explain", "what", "about", "define", "meaning", "help"}:
        rep = supports_v3_report()
        from .settings import render_profile_copy
        return {"ok": True, "mode": "explain", "explanation": render_profile_copy(_V3_EXPLAINER),
                "enabled": rep.get("enabled"), "supports": rep}
    if mode in {"bus", "messages"}:
        return RT.read_bus()
    if mode in {"voicemail", "voicemails", "vm"}:
        return RT.drain_voicemail(ack=False)  # peek without acking from a chat query
    if mode in {"reconcile", "catchup", "catch-up"}:
        return RT.reconcile_on_wake()
    if mode in {"version", "protocol"}:
        return RT.protocol_version_check()
    if mode in {"handshake", "handshake_test", "test", "dry_run", "readiness"}:
        import asyncio as _asyncio
        return await _asyncio.to_thread(handshake_test)
    # status: capability + transport + a non-acking peek at what's waiting
    rep = supports_v3_report()
    return {"ok": True, "mode": "status", "supports": rep,
            "version_check": RT.protocol_version_check() if rep["pi_transport"].get("reachable") else None}


def _tool_home_state(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only snapshot of live Home Assistant state: what's on/off/open/playing right now, optionally
    filtered to what was asked about (a room word, a device word, or a domain like 'lights'/'heating').
    Never calls a service, GET only. Use for 'are the lounge lights on', 'what's on at home', 'is the
    heating running', 'is the garage open', 'what's playing'."""
    from .services.ha_state import read_home_state
    q = str(args.get("query") or args.get("q") or args.get("room") or "").strip()
    return read_home_state(q)


async def _tool_search_voicemails(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only search over detected voicemails (caller numbers, notice text, audio filenames,
    transcripts). Reconciles IMAP + Pi/HA sources into one canonical voicemail each."""
    from .services.voicemail import list_voicemails, search_voicemails, voicemail_stats
    q = args.get("query") or args.get("q")
    try:
        limit = int(args.get("limit") or 10)
    except Exception:
        limit = 10
    if not q:
        return {"ok": True, "voicemails": list_voicemails(limit=limit), "stats": voicemail_stats()}
    return {"ok": True, "voicemails": search_voicemails(str(q), limit), "stats": voicemail_stats()}


async def _tool_rtm_portal(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only portal status tool for the brain."""
    from .services.rtm_portal import portal_readiness, build_portal_snapshot, portal_brain_summary
    action = str(args.get("action") or "summary").lower()
    if action == "readiness":
        return portal_readiness()
    if action == "snapshot":
        return build_portal_snapshot(full=False)
    return portal_brain_summary()

async def _tool_rtm_documents(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only: the RTM documents Mongoose has ingested and what they correlate to."""
    from .services import rtm_docs as RD
    action = str(args.get("action") or "context").lower()
    flat = str(args.get("flat") or "").strip()
    if action == "flat" and flat:
        return RD.flat_situation(flat)
    if action == "list":
        return {"ok": True, "context": RD.context_summary(),
                "documents": RD.list_documents(
                    doc_type=(str(args.get("doc_type")).strip() or None) if args.get("doc_type") else None,
                    flat_id=(flat or None), limit=int(args.get("limit") or 20))}
    return {"ok": True, "context": RD.context_summary()}


async def _tool_self_patch(args: dict[str, Any]) -> dict[str, Any]:
    from .services import self_patch as SP
    action = str(args.get("action") or "start")
    if action == "status":
        return {"ok": True, "jobs": SP.list_jobs()}
    goal = str(args.get("goal") or "")
    res = SP.request_self_patch(goal, requested_by="owner")
    if res.get("ok"):
        import asyncio as _a
        _a.create_task(SP.run_job(res["job_id"]))
    return res


async def _tool_capability_search(args: dict[str, Any]) -> dict[str, Any]:
    from .services.capability_index import search, gap_report
    action = str(args.get("action") or "search")
    if action == "gaps":
        return {"ok": True, "gap_report": gap_report()}
    q = str(args.get("query") or "")
    return {"ok": True, "results": search(q, top_k=int(args.get("top_k") or 8))}


async def _tool_capability_guide(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only: the 'what can I say?' map. Answers any-language questions about how to drive
    Mongoose (plugin build-out, self-heal, updates, RTM paperwork, guests) with the concrete
    utterances and their order."""
    from .services.capability_guide import guide
    return guide(str(args.get("topic") or ""))


async def _tool_wp_plugin(args: dict[str, Any]) -> dict[str, Any]:
    """WordPress plugin build, deploy, and management workflow for the RTM site."""
    from .services.wp_plugin import (
        plugin_source_status, list_builds, wp_list_plugins,
        build_plugin_zip, deploy_plugin_beta, wp_set_plugin_status, promote_plugin,
        scaffold_example_court_plugin
    )
    from .connectors.wordpress import wp_status
    action = str(args.get("action") or "status").lower()
    if action == "source":
        return plugin_source_status()
    if action == "builds":
        return {"ok": True, "builds": list_builds()}
    if action == "plugins":
        return wp_list_plugins()
    if action == "build":
        return build_plugin_zip(str(args.get("version_suffix") or "beta"))
    if action == "deploy":
        # Build then upload; overwrite=True so redeploy just works
        return deploy_plugin_beta(str(args.get("version_suffix") or "beta"),
                                   args.get("zip_path"))
    if action in ("activate", "deactivate"):
        return wp_set_plugin_status(str(args.get("plugin") or ""),
                                     "active" if action == "activate" else "inactive")
    if action == "promote":
        return promote_plugin(str(args.get("beta_slug") or ""),
                              str(args.get("production_slug") or ""))
    if action == "scaffold":
        return scaffold_example_court_plugin(str(args.get("version") or "0.1.0"))
    if action == "refresh":
        from .services.wp_plugin import refresh_scaffold
        return refresh_scaffold(str(args.get("version") or "0.2.0"))
    if action == "author":
        # Spec -> real code change in the plugin source dir, via headless Claude Code with
        # backup + validation + rollback. Dry-run unless apply is truthy; ships nothing.
        from .services.plugin_author import author_plugin_change
        return await __import__("asyncio").to_thread(
            author_plugin_change,
            str(args.get("spec") or ""),
            route=str(args.get("route") or "auto"),
            apply=bool(args.get("apply")),
        )
    # default: full status: include domain hint for the brain
    conn = wp_status()
    src = plugin_source_status()
    builds = list_builds()[:3]
    plugins = []
    if conn.get("ok"):
        pl = wp_list_plugins()
        plugins = pl.get("plugins", [])
    domain = _profile.site_domain()
    return {"ok": True, "site": conn.get("url") or domain,
            "connection": conn, "source": src, "builds": builds, "installed_plugins": plugins,
            "next_steps": (
                [f"Run scaffold to generate the starter {_profile.rtm_name()} RTM plugin"] if not src.get("ok") else
                ["Build the plugin zip, then deploy to the RTM site"] if not builds else
                [f"Deploy the latest build to {domain}"] if conn.get("ok") else
                [f"Configure WP_URL=https://{domain} in .env to enable deployment"]
            )}


async def _tool_knowledge_imports(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only report on imported thread dumps, knowledge claims and insight candidates."""
    mode = str(args.get("mode") or "summary").strip().lower().replace("-", "_")
    if mode in {"summary", "dashboard"}:
        from .knowledge_import.store import dashboard
        return dashboard(limit=int(args.get("limit") or 12))
    if mode in {"imports", "list"}:
        from .knowledge_import import list_imports
        return list_imports(limit=int(args.get("limit") or 10))
    if mode in {"claims", "claim"}:
        from .knowledge_import.store import list_claims
        return {"ok": True, "claims": list_claims(domain=args.get("domain"), status=args.get("status"), limit=int(args.get("limit") or 30))}
    if mode in {"insights", "insight"}:
        from .knowledge_import.insight_engine import list_insights
        return {"ok": True, "insights": list_insights(args.get("domain"), status=args.get("status"), limit=int(args.get("limit") or 20))}
    if mode in {"explain", "why"}:
        from .knowledge_import.insight_engine import explain_insight
        return explain_insight(str(args.get("insight_id") or ""))
    if mode in {"review", "queue"}:
        from .knowledge_import.store import list_review_queue
        return {"ok": True, "review": list_review_queue(status=args.get("status") or "open", severity=args.get("severity"), limit=int(args.get("limit") or 20))}
    if mode in {"research", "research_requests"}:
        from .knowledge_import.research_broker import list_research_requests
        return {"ok": True, "research": list_research_requests(limit=int(args.get("limit") or 20))}
    if mode in {"reflections", "reflection"}:
        from .knowledge_import.store import recent_reflections
        return {"ok": True, "reflections": recent_reflections(limit=int(args.get("limit") or 20))}
    if mode in {"search", "find"}:
        from .knowledge_import.store import search_knowledge
        return {"ok": True, "results": search_knowledge(str(args.get("query") or args.get("q") or ""), limit=int(args.get("limit") or 20))}
    if mode in {"care", "care_suggestions"}:
        from .knowledge_import.care_planner import rank_care_candidates
        from .knowledge_import.store import list_care_plans
        context = args.get("context") if isinstance(args.get("context"), dict) else {"prompt": args.get("query") or args.get("q") or ""}
        return {"ok": True, "plans": list_care_plans(limit=int(args.get("limit") or 10)), "care": rank_care_candidates(context, limit=int(args.get("limit") or 5))}
    if mode in {"fusions", "entity_fusions", "entities"}:
        from .knowledge_import.store import list_entity_fusions, list_fusion_suggestions
        return {"ok": True, "fusions": list_entity_fusions(limit=int(args.get("limit") or 30)), "suggestions": list_fusion_suggestions(limit=int(args.get("limit") or 30))}
    if mode in {"synthesis", "synthesis_cards"}:
        from .knowledge_import.store import list_synthesis_cards
        return {"ok": True, "synthesis": list_synthesis_cards(domain=args.get("domain"), status=args.get("status"), limit=int(args.get("limit") or 20))}
    return {"ok": False, "error": "mode must be dashboard/imports/claims/insights/explain/review/research/reflections/search/care/fusions/synthesis"}


async def _tool_web_search(args: dict[str, Any]) -> dict[str, Any]:
    """Live web search via the local SearXNG instance. Exists so the interactive chat can look things
    up on request instead of claiming it has no web access (the research worker had SearXNG, but the
    chat tool loop never exposed it)."""
    from . import web_search as _ws
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"ok": False, "error": "a search query is required"}
    try:
        limit = max(1, min(int(args.get("limit", 5)), 8))
    except Exception:
        limit = 5
    tr = args.get("time_range")
    time_range = tr if tr in ("day", "month", "year") else None
    try:
        results = await _ws.search_web(query, limit=limit, time_range=time_range)
    except _ws.SearxngError as exc:
        return {"ok": False, "error": str(exc),
                "note": "The web search backend (local SearXNG) did not answer. Say so honestly rather than inventing facts."}
    return {"ok": True, "query": query, "count": len(results), "results": results,
            "note": ("Live web snippets from SearXNG (snippets only, pages not fetched). Ground any claim "
                     "in these sources and attribute to the titles; if they do not cover the question, say so.")}


TOOLS: dict[str, dict[str, Any]] = {
    "web_search": {
        "executor": _tool_web_search,
        "args": {"query": "str, what to look up on the web",
                 "limit": "int, optional 1-8 (default 5)",
                 "time_range": "str, optional one of day/month/year to bias toward recent results"},
        "description": ("Search the live web via the local SearXNG instance and return grounded source "
                        "snippets (title, url, snippet). Use whenever the owner asks you to look something "
                        "up, web search, check a current fact, or update your knowledge about a person or "
                        "topic. Local-first and private. Snippets only, does not fetch full pages."),
    },
    "knowledge_imports": {
        "executor": _tool_knowledge_imports,
        "args": {"mode": "dashboard|imports|claims|insights|explain|review|research|reflections|search|care", "domain": "str optional", "status": "str optional", "severity": "str optional", "insight_id": "str optional", "query": "str optional", "limit": "int optional"},
        "description": "Read-only: imported thread dumps, provenance-preserving claims, review queue, research queue, reflections and insight candidates. Use for 'what did you import', 'what insights came from my dumps', 'what needs review', or 'why are you suggesting this'.",
    },
    "get_weather": {
        "executor": _tool_weather,
        "args": {"force": "bool, optional, refresh cache"},
        "description": "Current weather, rain window and UV from local cache.",
    },
    "control_home": {
        "executor": _tool_home,
        "args": {
            "room": "str", "device_id": "str", "action": "str",
            "entity_id": "str, Home Assistant entity id e.g. light.kitchen",
            "service": "str, e.g. turn_on / turn_off",
            "privacy": "str, one of low/medium/high, default medium",
        },
        "description": "Turn devices on/off or run a Home Assistant service. Verifies state.",
    },
    "set_reminder": {
        "executor": _tool_set_reminder,
        "args": {"text": "str, what to be reminded of",
                 "when": "str, optional human time e.g. '7am tomorrow'"},
        "description": "Save a reminder the owner will see on the dashboard/phone.",
    },
    "message_person": {
        "executor": _tool_message_person,
        "args": {"to": "str, '{{OWNER_NAME_LOWER}}'/'owner', 'everyone', or a guest id",
                 "text": "str, the message body"},
        "description": "Send a short message to {{owner.name}} or a guest via the mailbox.",
    },
    "check_messages": {
        "executor": _tool_check_messages,
        "args": {},
        "description": "Read-only: read MY unread direct messages (from {{owner.name}} or a guest). Use whenever the user asks if they have messages, new notifications, or whether a named person wrote/said/sent anything. Returns the real stored messages; never guess that there's nothing without calling this.",
    },
    "recommend_song": {
        "executor": _tool_recommend_song,
        "args": {"song": "str, the track, e.g. 'Blackbird by The Beatles'",
                 "to": "str, a person's name (e.g. 'Jordan', 'Riley'), or 'everyone'/'all'/'guests'",
                 "note": "str, optional, a personal line to pass along with it"},
        "description": "Pass a song someone recommends ON to the person, or people, it is FOR, delivering it into THEIR own chat plus a push and recording it. Use this WHENEVER anyone recommends, suggests, or shares a song for SOMEONE ELSE (not asking to play it here). Trigger phrasings, any language: 'tell Jordan to listen to Bohemian Rhapsody', 'recommend Blackbird by the Beatles to Riley', 'Riley would love this, Weightless by Marconi Union', 'send Casey this song ...', 'everyone should hear Blackbird', 'share this track with the guests', 'let Jordan know he'd like ...'. Put the track in `song`, the recipient's name (or everyone/all/guests) in `to`, and any personal line in `note`. Works whether the owner OR a guest is the one recommending. CRITICAL GUARDS: (1) Do NOT trigger when someone is merely stating a favourite, answering a question about their own taste, or naming an artist/track in passing (e.g. 'my go-to was Madonna', 'Ray of Light obviously', 'I love Blackbird'), that is a stated PREFERENCE, not a request to pass a track to anyone. Only fire on a clear instruction to give/send/recommend a SPECIFIC track TO a named person or explicitly to everyone. (2) NEVER assume `to='everyone'` on your own. If the message does not name a recipient, do NOT fanout to all guests, either leave `to` for a real named recipient in the message, or if truly none is given, do not call this tool.",
    },
    "check_availability": {
        "executor": _tool_calendar_availability,
        "args": {"start_at": "ISO8601 str", "end_at": "ISO8601 str"},
        "description": "Whether the calendar is busy or free in a time window.",
    },
    "search_email": {
        "executor": _tool_search_email,
        "args": {"query": "str", "limit": "int, optional"},
        "description": "Read-only full-text search over indexed email. Never sends or deletes.",
    },
    "list_tasks": {
        "executor": _tool_list_tasks,
        "args": {"limit": "int, optional", "include_completed": "bool, optional", "list_name": "str, optional"},
        "description": "Read-only Microsoft To Do / synced task list, including folders/lists and open tasks. Use for To Do, tasks and folder/list questions.",
    },
    "get_tasks": {
        "executor": _tool_get_tasks,
        "args": {"limit": "int, optional", "include_completed": "bool, optional", "list_name": "str, optional; To Do list/folder name"},
        "description": "Read-only: Alias for list_tasks. Read synced To Do tasks and lists/folders from the real local task index, optionally filtered by list_name.",
    },
    "list_calendar": {
        "executor": _tool_list_calendar,
        "args": {"days": "int, optional horizon", "limit": "int, optional"},
        "description": "Read-only local synced calendar: today's events and upcoming events. Use for calendar/event questions.",
    },
    "get_calendar": {
        "executor": _tool_get_calendar,
        "args": {"days": "int, optional", "limit": "int, optional"},
        "description": "Read-only: Alias for list_calendar. Read today's and upcoming synced calendar events from the real local calendar index.",
    },
    "rtm_build_source": {
        "executor": _tool_rtm_build_source,
        "args": {},
        "description": "Read-only: explains the {{rtm.name}} RTM product-build source of truth across UI builds, chat-triggered builds, WordPress plugin work, staged updates, Claude/Code patches and protocol-v3 peer coordination. Use for 'where is the RTM spec kept', 'what is canonical', 'can I build through chat or the UI'.",
    },
    "body_data": {
        "executor": _tool_body_data,
        "args": {},
        "description": "Read-only: what training/nutrition data Mongoose has (Alpha Progression sessions/sets, MyFitnessPal) and what it is currently asking the owner to upload. Use for 'what data do you need', 'do you have my training data', 'how do I give you my Alpha export or MyFitnessPal data'. Importing happens via the cockpit Body data card or a phone upload, not this tool.",
    },
    "guest_privacy": {
        "executor": _tool_guest_privacy,
        "args": {},
        "description": "Read-only: privacy-preserving report on guest boundaries, owner-vs-guest isolation, guest counts/freshness and checks. Does not expose guest message text, notes, reminders, memory fact text, tokens or hashes. Use for 'can {{owner.name}} read guest state', 'are guests isolated', 'audit guest privacy'.",
    },
    "conversation_continuity": {
        "executor": _tool_conversation_continuity,
        "args": {},
        "description": "Read-only: redacted health report for load-bearing chat context across owner surfaces, guest sessions and restarts. Reports counts, timestamps, replay budget and invariants without returning message text. Use for 'will chat context survive', 'check conversation continuity', 'audit memory context'.",
    },
    "rtm_bank_feed_playbook": {
        "executor": _tool_rtm_bank_feed_playbook,
        "args": {"mode": "str, optional: status/providers/evidence/messages/matrix", "provider": "str, optional; xero/crunch/plaid/pandle/gocardless/moneyhub/yapily/clearbooks/truelayer/finverse"},
        "description": "Read-only RTM Metro bank-feed verification playbook: account inventory state, provider test order, Xero/Crunch/Plaid/Pandle/GoCardless/Moneyhub/Yapily/Clear Books/TrueLayer/Finverse playbooks, evidence pack requirements and read-only trust-fund guardrails.",
    },
    "search_files": {
        "executor": _tool_search_files,
        "args": {"query": "str", "limit": "int, optional"},
        "description": "Read-only unified item search across files, email, attachments, voicemails and notifications. Returns ranked results with item_ref, result_set_id, actions and correlation keys. Use for natural requests like 'find/search/look for/show me X'.",
    },
    "item_actions": {
        "executor": _tool_item_actions,
        "args": {"item_ref": "str optional", "ordinal": "int optional, e.g. 1 for first result", "result_set_id": "str optional"},
        "description": "Read-only: explain what actions are available for a search result and its correlation keys. Use for 'what can I do with the first result', 'what actions are available for E123AB', or 'tell me about that search item'.",
    },
    "read_document": {
        "executor": _tool_read_document,
        "args": {"item_ref": "str optional (from a prior search)", "ordinal": "int optional, e.g. 1 for the first search result",
                 "attachment_id": "int optional", "document_id": "int optional", "filename": "str optional"},
        "description": ("Read-only: return the FULL extracted text of one specific document or email attachment "
                        "(e.g. an invoice PDF). ALWAYS use this before stating any figure from a document, an "
                        "amount, a sort code, an account number, a due date, a reference number: quote only what "
                        "it returns, never invent. Resolve by item_ref/ordinal from a search, or by "
                        "attachment_id/document_id/filename. Use after search_files finds a document and the user "
                        "asks what is in it."),
    },
    "search_notifications": {
        "executor": _tool_search_notifications,
        "args": {"query": "str", "limit": "int, optional"},
        "description": "Read-only search over cached notifications.",
    },
    "what_changed": {
        "executor": _tool_what_changed,
        "args": {"peek": "bool, optional; if true, don't reset the checkpoint"},
        "description": "Read-only: Cross-domain delta since you last looked: new WhatsApps, email, files, notifications.",
    },
    "ritual": {
        "executor": _tool_ritual,
        "args": {"name": "str, optional; one of physio/proprioception/breathing/journal/resilience/friend_checkin. Omit to list."},
        "description": "Read-only: Start a useful micro-practice and return its short script. Omit name to list rituals.",
    },
    "whatsapp_search": {
        "executor": _tool_whatsapp_search,
        "args": {"query": "str", "limit": "int, optional"},
        "description": "Read-only search over locally-indexed WhatsApp messages. Answers 'what did X say?'.",
    },
    "whatsapp_draft": {
        "executor": _tool_whatsapp_draft,
        "args": {"contact": "str, contact name", "body": "str, the message text"},
        "description": "DRAFT a WhatsApp message (does NOT send). Creates a pending draft the user must approve. There is no send tool: Mongoose can never send WhatsApp autonomously.",
    },
    "research": {
        "executor": _tool_research,
        "args": {"query": "str, the question to research", "context": "str, optional background"},
        "description": "Read-only: Ask the silent research worker (a specialist model) to gather and structure evidence for a factual/analytical question. Returns NOTES for you to phrase, not a finished answer. Read-only: it cannot speak, send, or act. Use for research/Q&A/analysis the user asked about, then phrase the reply yourself.",
    },
    "system_status": {
        "executor": _tool_system_status,
        "args": {"area": "str, optional; one of all/scheduler/resources/hardening/consistency/updates/connectivity/connectors/rtm/benchmark/tasks/calendar/home. Omit or 'all' for a roll-up."},
        "description": "Read-only health status of Mongoose's own subsystems: background scheduler jobs, OS/process memory and loop pressure, self-hardening, claim consistency, staged updates, connectivity (DDNS/LAN), RTM accounts, model benchmarks, synced tasks/calendar, Home Assistant reachability. Use for 'how's X doing', 'is X working', 'what's the status of X', or 'is the model/process stuck or using too much memory'.",
    },
    "connector_status": {
        "executor": _tool_connector_status,
        "args": {"name": "str, optional; one connector (gmail/google_calendar/google_drive/microsoft/imap/home_assistant/ollama). Omit for all.", "action": "str, optional; 'verify' to re-probe, 'sync' to pull fresh read-only data, 'journey' for setup/enable/disable lifecycle guidance. Omit to just report status."},
        "description": "Read-only: LIVE connector status AND actions. Reports whether each connector actually WORKS now (Gmail, Calendar, Drive, Microsoft To Do, IMAP, Home Assistant, Ollama), surfacing set-up-but-broken ones. With action='verify' re-probes; with action='sync' pulls fresh read-only data (gmail/calendar/imap/microsoft, never sends/deletes); with action='journey' explains setup/verify/sync lifecycle and next actions. Use for 'is Gmail working', 'sync my email now', 're-check home assistant', 'are my integrations set up properly'.",
    },
    "self_patch": {
        "executor": _tool_self_patch,
        "args": {"action": "str: start|status", "goal": "str: what to harden/improve, e.g. 'harden the QR code logic'"},
        "description": "Sensitive: owner asks Mongoose to improve ITS OWN code ('try to harden your QR logic'). Builds a candidate patch in a background workspace via Claude Code, runs compile/lint/test gates, gets independent peer reviews, then stages it in the normal update Approvals flow. NOTHING is applied without the owner approving the staged candidate. action=status reports job progress from receipts.",
    },
    "capability_search": {
        "executor": _tool_capability_search,
        "args": {"action": "str: search|gaps", "query": "str: what the user wants, any phrasing/language", "top_k": "int, optional"},
        "description": "Read-only: semantic+keyword search over EVERYTHING Mongoose can do (52 chat tools, 250 CLI commands, 394 routes, guide workflows), built live from the registries. Use when the user describes a need without the exact lingo ('the QR thing is refused again', 'make the website regenerate'), or asks 'can you do everything the Cockpit can?' (action=gaps returns chat-native vs cockpit-only vs CLI-only counts with examples).",
    },
    "capability_guide": {
        "executor": _tool_capability_guide,
        "args": {"topic": "str, optional: what the user wants to do, in any language (e.g. 'plugin', 'fix a bug', 'wtyczka', 'naprawa'). Empty for the full overview."},
        "description": "Read-only: ALWAYS use this when the user asks HOW to do something with Mongoose, what to say, what commands exist, or how to build something out -- in ANY language or phrasing: 'how do I change the website plugin', 'what do I tell you to fix bugs', 'co mam powiedzieć', 'jak zaktualizować', 'what can you do', 'remind me the steps for deploying'. Returns the concrete utterances that work and the order to say them in, for: plugin build-out/authoring, self-healing bugs, updating Mongoose, RTM paperwork, guest invites, and any other registered tool.",
    },
    "wp_plugin_status": {
        "executor": _tool_wp_plugin,
        "args": {"action": "str: status|source|builds|plugins (read-only actions only)"},
        "description": "Read-only: check the WordPress plugin build status for {{site.domain}}. See connection status, plugin source directory, list of builds, and installed plugins. Use for: 'what plugins are on the RTM site', 'is the plugin deployed', 'what build do I have'.",
    },
    "wp_plugin_action": {
        "executor": _tool_wp_plugin,
        "args": {"action": "str: build|deploy|activate|deactivate|promote|scaffold|refresh|author",
                 "version_suffix": "str: e.g. 'beta' or '1.2.3'",
                 "plugin": "str: plugin slug 'folder/main.php' for activate/deactivate",
                 "beta_slug": "str: beta plugin slug for promote",
                 "production_slug": "str: production plugin slug to deactivate on promote",
                 "spec": "str: for author -- plain English of what the plugin should do differently",
                 "apply": "bool: for author -- false (default) previews the plan; true makes the change",
                 "route": "str: for author -- auto|local|pi executor choice"},
        "description": "Build, deploy, activate, promote, refresh, or AUTHOR the {{rtm.name}} RTM WordPress plugin. Actions: scaffold (generate starter plugin), refresh (regenerate the template files in an existing source dir so it picks up template improvements, with backup), author (turn a written spec into a real code change in the plugin source via headless Claude Code, with backup, php validation and rollback; dry-run unless apply=true; never deploys by itself), build (create zip), deploy (upload to {{site.domain}}, replaces existing), activate/deactivate, promote (swap beta to production). Use for: 'build the RTM plugin', 'deploy to {{site.domain}}', 'make the Service Charges page show arrears totals', 'change the plugin so notices get a PDF attachment', 'refresh the plugin template'.",
    },
    "search_voicemails": {
        "executor": _tool_search_voicemails,
        "args": {"query": "str, optional; caller number, name, or words from the message. Omit to list recent.", "limit": "int, optional"},
        "description": "Read-only search over detected voicemail notices: caller numbers, notice text, audio filenames, and transcripts (IMAP + Pi/HA sources reconciled into one voicemail each). Use for 'any voicemails from <number>', 'what voicemails do I have', missed-call message questions.",
    },
    "home_state": {
        "executor": _tool_home_state,
        "args": {"query": "str, optional; narrow to a room/device/domain, e.g. 'lounge', 'lights', 'heating'. Omit for a whole-home snapshot."},
        "description": "Read-only snapshot of live Home Assistant state: what's on, off, open, playing, the current temperature, etc. GET only, never actuates. Use for 'are the lights on', 'what's on at home', 'is the heating running', 'is the garage open'.",
    },
    "self_reflection": {
        "executor": _tool_self_reflection,
        "args": {"apply": "bool, optional; if true, apply bounded reversible auto-fixes (read-only-tool registration only). Default false (audit only)."},
        "description": "Read-only self-audit of Mongoose's OWN wiring: detects capabilities that exist in code but aren't reachable from the single brain (e.g. a tool not registered read-only, or missing from the planner catalogue). Reports gaps; with apply=true closes the bounded, reversible ones (never writes code). Use for 'check your own wiring', 'audit yourself', 'are all your tools reachable'.",
    },
    "v3_coordination": {
        "executor": _tool_v3_coordination,
        "args": {"mode": "str, optional: status (default) / bus / voicemail / reconcile / version / handshake."},
        "description": "Read-only household coordination (protocol v3): EXPLAINS what protocol v3 is (mode=explain) and shows shared-bus catch-up, {{rtm.name}} protocol-v3 voicemail staging, peer presence, protocol-version check, and dry-run handshake testing. ALWAYS use this for any question about 'v3' or 'protocol v3', including definitional ones: 'what is v3', 'do you know what v3 is', 'explain v3', 'what does v3 mean', as well as 'check the bus', 'are chat and code there', 'test v3 readiness', 'catch up on coordination', 'sync with v3 peers'. mode=reconcile catches up + acks + heartbeats but takes NO household action (M-approval-gated). Never guess what v3 means: call this.",
    },
    "consistency_check": {
        "executor": _tool_consistency_check,
        "args": {},
        "description": "Read-only. Check whether any two subsystems make the same claim using different logic (e.g. the installer vs `mongoose stop` on 'is this my process'). Reports each claim and, on divergence, which authorities disagree.",
    },
    "rtm_portal": {
        "executor": _tool_rtm_portal,
        "args": {"action": "str: readiness|snapshot|summary (default: summary)"},
        "description": "Read-only: {{rtm.name}} leaseholder portal status. Check portal readiness (bank details, WordPress connection, claim keypair, PDF storage), view minimised snapshot, get overall status. Use for: 'is the portal ready', 'what\'s missing from the leaseholder portal', 'portal readiness check'. Does NOT move money, publish data, or process claims: use the cockpit or CLI for publish/process actions.",
    },
    "rtm_statement": {
        "executor": _tool_rtm_statement,
        "args": {"flat": "str, optional; a flat identifier like {{RTM_BLOCK_A}}F1.",
                 "leaseholder": "str, optional; a leaseholder name or surname like 'Morgan', resolved to their flat. Omit both for arrears across all flats."},
        "description": "Read-only RTM accounts: a flat's statement (balance, demands, payments) AND how each demand splits across the two service-charge schedules / 'subaccounts' (Building service costs + Undercroft parking). Accepts a flat id OR a leaseholder name/surname. Use for 'what does flat 3 owe', 'what does Morgan owe', 'how does Morgan's payment / the 1080.21 split into the 2 subaccounts', 'who's behind on service charges'.",
    },
    "rtm_mark_paid": {
        "executor": _tool_rtm_mark_paid,
        "args": {"flat": "str optional, a flat id like {{RTM_BLOCK_B}}F9", "leaseholder": "str optional, a leaseholder name/surname like 'Okafor'", "document_number": "str optional, a demand doc number", "amount": "str optional, defaults to the full outstanding", "received_date": "str optional, YYYY-MM-DD (defaults to today)"},
        "description": "Record a leaseholder's service-charge payment as RECEIVED, a bank credit plus allocation, which clears their balance and marks the demand Paid. Records a receipt of money already in the bank; it NEVER initiates or moves money. Idempotent: an already-paid flat is a safe no-op. Use for 'mark Morgan paid', 'record that flat 5 has paid', 'Raw paid 1080.21 on 3 July'. Resolve by flat id, leaseholder name, or document number.",
    },
    "rtm_send_receipt": {
        "executor": _tool_rtm_send_receipt,
        "args": {"flat": "str optional, a flat id like {{RTM_BLOCK_B}}F9", "leaseholder": "str optional, a leaseholder name/surname", "receipt_no": "str optional, a specific receipt number (defaults to the latest)"},
        "description": "Email a leaseholder their payment RECEIPT (already on file) with a branded cover note, sent from {{site.rtm_email}} with the receipt PDF attached. Use for 'send Jo her receipt', 'email Okafor his receipt'. Resolve by flat id or leaseholder name. Requires a receipt already attached for that flat.",
    },
    "rtm_plan_letter": {
        "executor": _tool_rtm_plan_letter,
        "args": {"flat": "str optional, a flat id like {{RTM_BLOCK_B}}F1", "leaseholder": "str optional, a leaseholder name/surname like 'Hale'", "action": "str: preview (default, composes and returns the letter, sends nothing) or send", "erratum": "str optional, a correction notice rendered in red at the top, for retracting a letter sent in error"},
        "description": "The payment-plan letter for a leaseholder on an agreed plan: what was agreed, what they have paid, what is payable now and by when, and the standing order for the rest. action=preview (default) returns the full text to read and sends NOTHING; action=send emails it from the RTM address (sensitive: only after the owner has read the preview and confirms). The letter states the position as at today, so instalments already paid are credited and anything overdue is summed into one catch-up figure payable within 7 days. Use for 'show me the payment plan letter for Hale', 'what would Kathryn's plan letter say', 'send {{RTM_BLOCK_B}}F1 her payment plan letter'.",
    },
    "rtm_demand_schedule": {
        "executor": _tool_rtm_demand_schedule,
        "args": {"action": "str: status (default) | preview | issue | cancel", "confirm": "bool: required true to actually issue"},
        "description": "The next SCHEDULED half-year service-charge demand run: view it, dry-run the exact per-flat figures (preview), or confirm-issue it to the ledger. Each half is an already-approved instalment (half the annual budget, lease clause 12.11), payable 1 January / 1 July (12.10) - it needs issuing at the right time, not re-approving. Mongoose previews it on its prepare-date and alerts you; 'issue' (confirm=true) writes the demands, then the separate demand mailer sends them. Use for 'what demands are scheduled', 'preview the January demands', 'issue the scheduled demands'.",
    },
    "rtm_on_account": {
        "executor": _tool_rtm_on_account,
        "args": {"flat": "str optional, a flat id like {{RTM_BLOCK_B}}F1", "leaseholder": "str optional, a name/surname like 'Hale'", "action": "str: status (default) | record | assign", "amount": "str optional, e.g. '900.00' (required for record; optional for assign)", "fund": "str optional for assign: 'reserve' or 'service'", "note": "str optional"},
        "description": "Payment on account: record an ADVANCE or overpayment as standing credit (no refunds; carried forward to the next demand), split that credit to the reserve or service fund, or read what a flat holds. Use for 'record 900 on account for Hale', 'Hale paid next year early', 'put {{RTM_BLOCK_B}}F1 credit in the reserve', 'how much has Hale paid in advance'. An overpayment of an existing demand does NOT need this - record_payment/mark-paid now carries the excess to account automatically.",
    },
    "rtm_demand_mailing": {
        "executor": _tool_rtm_demand_mailing,
        "args": {"action": "str: preview (default, dry-run) or send", "period": "str optional like '2026-H2' (defaults to the current outstanding period)", "force": "bool optional, resend even if already sent this period", "priority": "str optional: 'high' flags the emails high-importance (for final chasers), default 'normal'"},
        "description": "The BATCH 'payment now due' demand mailing: emails every flat with an outstanding service-charge demand AND an email on file, individually (never BCC), each with its own demand PDF attached and a dash-free cover repeating the amount, bank details and payment reference. action=preview is a dry-run showing who would be emailed, the amounts, and who is excluded and why. action=send performs the sends (sensitive: only after the owner confirms). Idempotent: a demand already emailed this period is skipped. Use for 'prepare the payment-due batch', 'who would the demand mailing go to', 'send the payment-due emails'.",
    },
    "rtm_knowledge": {
        "executor": _tool_rtm_knowledge,
        "args": {"action": "str: index|search|section|semantics|budget (default index)",
                 "query": "str, for action=search, a term to find across the base",
                 "section": "str, for action=section, a file path like 'legal/matters.yaml' or a kind like 'operations/suppliers'"},
        "description": "Read-only {{rtm.name}} RTM knowledge base, the authoritative facts: the budget, the flats/leaseholders and external parties (solicitors, agents, bank, tribunal), live legal matters (tribunal cases, applications), suppliers/contracts (cleaning, electricity, insurance), the calendar of deadlines, digital infra, and history. Use for RTM FACTUAL questions: 'who are the solicitors', 'when is the FTT hearing', 'what is the cleaning contract', 'what deadlines are coming', 'what is the reserve line'. Grounds answers in the owner's canonical records; never invents. For a leaseholder's balance/split use rtm_statement; for the meaning of a term use rtm_ontology.",
    },
    "rtm_ontology": {
        "executor": _tool_rtm_ontology,
        "args": {"action": "str: describe|explain|value|audit (default describe)",
                 "key": "str optional; a concept or rule key like 'sub_account', 'schedule', 'reserve_fraction', 'subaccount_split'"},
        "description": "Read-only: the RTM domain ontology, the canonical MEANING of RTM terms and rules and how they are layered/overridden, plus a self-audit. Use to ground an RTM answer in the authoritative definition rather than guessing, and to check the knowledge is internally consistent. For 'what does sub-account mean vs schedule', 'explain the reserve split rule', 'audit the RTM ontology', 'what is the reserve fraction'.",
    },
    "rtm_documents": {
        "executor": _tool_rtm_documents,
        "args": {"action": "str: context|list|flat (default context)", "flat": "str optional, a flat id like {{RTM_BLOCK_A}}F1", "doc_type": "str optional, one of receipt/invoice/contract/ticket/feedback/complaint/photo_evidence/other_evidence", "limit": "int optional"},
        "description": "Read-only: the RTM documents Mongoose has ingested (receipts, invoices, contracts, tickets, complaints, evidence) and what each correlates to (flats, demands, voicemails, emails). Use for 'what documents do we have for flat {{RTM_BLOCK_A}}F1', 'show ingested receipts', 'what do we know about this flat from paperwork', 'any complaints on file'. Returns actual stored documents and their links; it never invents document content.",
    },
    "wellness_checkins": {
        "executor": _tool_wellness_checkins,
        "args": {"action": "str: configure|status|run|log", "owner": "bool", "guests": "bool", "hour": "int optional", "minute": "int optional", "prompt": "str optional"},
        "description": "Configure or run structured mindfulness/wellness self-journal check-in reminders for the owner and trusted guests. Uses scheduler, Surpriser and durable memory; guest nudges go through the guest mailbox, not shared phone push.",
    },
    "log_bug": {
        "executor": _tool_log_bug,
        "args": {"text": "str, minimal bug description from the user's words"},
        "description": "Create a durable bug report with a new short memorable id like A42 from a minimal user description. Use for exact phrase 'log bug ...'.",
    },
    "data_dump": {
        "executor": _tool_data_dump,
        "args": {"kind": "str optional: emails|tasks|notifications|diary|facts|bugs|whatsapp|events|everything"},
        "description": "Export the owner's data as a downloadable zip and return a dump id + download link. Use when the user asks for a 'data dump', 'export my data', 'download my emails/tasks/notes', etc. Owner-scoped: never includes guest-private content.",
    },
    "add_bug_detail": {
        "executor": _tool_add_bug_detail,
        "args": {"bug_id": "str, short id like A42", "detail": "str, additional evidence/context"},
        "description": "Append more evidence/context to an existing bug report. Use for 'add bug detail BUG-... ...'.",
    },
    "diagnose_bug": {
        "executor": _tool_diagnose_bug,
        "args": {"bug_id": "str, short id like A42", "detail": "str optional"},
        "description": "Diagnose an existing bug from its accumulated detail, producing a grounded hypothesis and strength score. Mutates only the bug record.",
    },
    "bug_status": {
        "executor": _tool_bug_status,
        "args": {"bug_id": "str optional", "limit": "int optional"},
        "description": "Read-only view of one bug report or recent bug reports, including events, diagnosis, candidate paths and peer/verifier approvals.",
    },
    "attempt_bug_fix": {
        "executor": _sensitive_placeholder,
        "args": {"bug_id": "str, short id like A42", "run_codegen": "bool optional", "request_peers": "bool optional"},
        "description": "SENSITIVE: from a strongly diagnosed bug, create a candidate workspace/patch, run local verifier checks and request peer/v3 review. Proposes first; does not apply live code.",
    },
    "apply_bug_fix": {
        "executor": _sensitive_placeholder,
        "args": {"bug_id": "str, short id like A42"},
        "description": "SENSITIVE: apply a bug fix only after at least one peer/verifier OK, using the existing staged update system. Proposes first and still obeys update-stage gates.",
    },
    "recent_activity": {
        "executor": _tool_recent_activity,
        "args": {"limit": "int, optional; how many recent items (default 20)."},
        "description": "Read-only. What Mongoose has actually done lately, from receipts/audit including failures and skips. For 'what have you done', 'what happened recently', 'show your activity'.",
    },
    "run_hardening_scan": {
        "executor": _sensitive_placeholder,
        "args": {},
        "description": "Run a self-hardening scan (records observations + checks claim consistency). SENSITIVE: proposes first; runs only after the user confirms.",
    },
    "run_due_jobs": {
        "executor": _sensitive_placeholder,
        "args": {},
        "description": "Run any background scheduler jobs that are due, once. SENSITIVE: proposes first; runs only after the user confirms. Use for 'kick the scheduler', 'run due jobs now'.",
    },
    "scheduler_control": {
        "executor": _sensitive_placeholder,
        "args": {"action": "str: status|disable|enable|free_memory", "jobs": "list[str] or comma string, optional", "reason": "str, optional"},
        "description": "Pause/resume selected background scheduler jobs, or action=free_memory to pause deferrable model/IO-heavy jobs while leaving safety/continuity jobs running. SENSITIVE: proposes first; runs only after the user confirms. Use for 'stop scheduler jobs to free memory', 'pause benchmarks', 'resume email sync'.",
    },
    "check_update": {
        "executor": _sensitive_placeholder,
        "args": {"stage_id": "str, the staged candidate id", "runtime_smoke": "bool, optional; also boot the candidate on a temp port to verify it serves"},
        "description": "Run pre-apply checks on a staged update (manifest/compile/lint, optional runtime smoke). SENSITIVE: proposes first; applies nothing.",
    },
    "apply_update": {
        "executor": _sensitive_placeholder,
        "args": {"stage_id": "str, the staged candidate id"},
        "description": "Apply a checked+approved staged update (replaces the running version; backup taken first). SENSITIVE: proposes first; runs only after the user confirms, and still refuses an unapproved/unchecked stage.",
    },
    "confirm_sensitive_action": {
        "executor": _tool_confirm_sensitive,
        "args": {"token": "str, the confirm_token from a prior proposal"},
        "description": "Execute a previously proposed sensitive action after the user has explicitly confirmed it. Pass the confirm_token from the proposal.",
    },
}


_RENDERED_TOOLS: dict[Any, dict[str, dict[str, Any]]] = {}


def rendered_tools() -> dict[str, dict[str, Any]]:
    """{name: {"description", "args"}} with the site-profile placeholders in tool copy filled in (the
    organisation, domain, owner and example flat ids the model sees). Cached per profile. Anything that
    shows a tool description to a model or a person should read it from here, not raw TOOLS."""
    from .settings import profile_copy_key, render_profile_copy
    key = (profile_copy_key(), len(TOOLS))
    hit = _RENDERED_TOOLS.get(key)
    if hit is None:
        hit = {name: {"description": render_profile_copy(spec["description"]),
                      "args": {k: render_profile_copy(v) if isinstance(v, str) else v
                               for k, v in spec["args"].items()}}
               for name, spec in TOOLS.items()}
        _RENDERED_TOOLS.clear()
        _RENDERED_TOOLS[key] = hit
    return hit


def tool_catalogue() -> list[dict[str, Any]]:
    """Structured view of the allowlisted tools, for the /tools/catalogue endpoint."""
    return [
        {"name": name, "description": spec["description"], "args": spec["args"]}
        for name, spec in rendered_tools().items()
    ]


def _tool_catalogue() -> str:
    lines = []
    for name, spec in rendered_tools().items():
        args = ", ".join(f"{k} ({v})" for k, v in spec["args"].items())
        lines.append(f"- {name}: {spec['description']} args: {args}")
    return "\n".join(lines)


_PLANNER_SYSTEM = (
    "You are Mongoose's tool planner. You can call tools one at a time to "
    "satisfy the user's request, then finish. Reply with ONLY a JSON object, "
    "no prose, no markdown fences.\n"
    "To call a tool: {\"tool\": \"<name>\", \"args\": { ... }}.\n"
    "When the request is fully handled, or no tool fits, or it is just "
    "conversation: {\"tool\": null}.\n"
    "You will be shown the results of tools you already ran this turn. Use them "
    "to decide the next step. Do not repeat a call you have already made with "
    "the same arguments. Never invent a tool name not in the list. There are no "
    "destructive tools.\n"
    "CONDITIONAL LOGIC: if the user said 'do X only if Y is mentioned in the results', "
    "you MUST read the actual result content and evaluate the condition. "
    "If Y is not present in the results, output {\"tool\": null}: do NOT call the action tool. "
    "If a transcript field is null or transcript_status is 'pending_no_audio', you cannot evaluate "
    "the condition: output {\"tool\": null} and let the phrasing step explain why.\n"
    "Available tools:\n"
)

# Hard ceiling on tool calls in a single turn. The loop stops here regardless
# of what the model wants, so a confused or adversarial plan cannot run away.
# Configurable via MONGOOSE_MAX_TOOL_STEPS for deeper agentic chains (e.g. multi-step household
# routines), but bounded to an absolute maximum so a runaway plan can never exceed it. Default 4
# stays gentle; raise it when you want the local Gemma loop to chain more actions autonomously.
def _resolve_max_steps() -> int:
    import os as _os
    _ABSOLUTE_MAX = 12  # never allow more than this, whatever the env says
    try:
        v = int(_os.getenv("MONGOOSE_MAX_TOOL_STEPS", "4"))
    except (TypeError, ValueError):
        v = 4
    return max(1, min(v, _ABSOLUTE_MAX))


MAX_STEPS = _resolve_max_steps()


def _extract_search_query(prompt: str) -> str:
    """Best-effort natural-language query extraction for deterministic item search.

    It keeps this deliberately simple: chat can say "find the service charge email" and the actual
    query becomes "service charge" rather than the whole instruction. The model can still plan more
    sophisticated searches when this shortcut doesn't match.
    """
    text = (prompt or "").strip()
    low = text.lower()
    patterns = [
        r"^(?:please\s+)?(?:find|search(?:\s+for)?|look\s+for|look\s+up|show\s+me|where\s+(?:is|are))\s+(?:my\s+)?(.+)$",
        r"^(?:do\s+i\s+have|have\s+i\s+got|is\s+there|are\s+there)\s+(?:any\s+)?(.+)$",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            text = m.group(1).strip(" ?.!\n\t")
            break
    # Remove common scope words but preserve meaningful content.
    text = re.sub(r"\b(in|from|across)\s+(my\s+)?(files?|emails?|e-?mails?|attachments?|voicemails?|notifications?|documents?|docs?|mongoose|everything)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(file|email|e-mail|attachment|voicemail|notification|document|doc|message|result)s?\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ?.!\n\t")
    return text or low


def _ordinal_from_prompt(prompt: str) -> int | None:
    t = (prompt or "").lower()
    for word, num in {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}.items():
        if re.search(rf"\b{word}\b", t):
            return num
    m = re.search(r"\b(?:result|item)\s+(\d{1,2})\b", t)
    if m:
        return int(m.group(1))
    if re.search(r"\b(that|this|it|them)\b", t):
        return 1
    return None


def _has_voicemail_word(text: str) -> bool:
    return bool(re.search(r"\bvoice\s*mails?\b|\bvoicemails?\b|\bmissed\s+calls?\b", text or ""))


def _looks_like_v3_voicemail_lookup(text: str) -> bool:
    """Protocol-v3 voicemail is the HA/Pi staging queue, not the user's ordinary voicemail index."""
    t = (text or "").lower()
    if not _has_voicemail_word(t):
        return False
    return bool(
        re.search(r"\b(protocol\s*v?3|v3|claude[_ -]?bus|bus|coordination|staged|staging|drain|pi|home assistant|\bha\b)\b", t)
        and re.search(r"\b(check|read|peek|drain|reconcile|catch\s*up|waiting|queued|staged)\b", t)
    )


def _looks_like_voicemail_lookup(text: str) -> bool:
    """Generic voicemail lookups must use the canonical voicemail index, never files or v3 coordination."""
    t = (text or "").lower()
    if not _has_voicemail_word(t) or _looks_like_v3_voicemail_lookup(t):
        return False
    if re.search(r"\b(delete|send|forward|archive|apply|fix|patch|code|regex|route|routing|build|deploy)\b", t):
        return False
    _rtm = _rtm_name_rx(r"\s?")
    if re.search(rf"\b({_rtm}|rtm)\b.{{0,30}}(?:voice\s*mails?|voicemails?|missed\s+calls?)", t):
        return True
    return bool(re.search(
        r"\b(any|new|latest|recent|unread|check|show|list|read|search|find|look\s+up|do i have|have i got|are there|is there|did|has|missed)\b",
        t,
    ))


def _extract_voicemail_query(prompt: str) -> str | None:
    text = (prompt or "").strip()
    # Phone numbers survive best as compact digits in the voicemail FTS index.
    phone = re.search(r"(?:\+?\d[\d\s().-]{5,}\d)", text)
    if phone:
        digits = re.sub(r"\D", "", phone.group(0))
        if digits:
            return digits
    m = re.search(r"\bfrom\s+([A-Za-z][\w .'-]{1,48})", text)
    if m:
        who = re.sub(r"\b(?:please|today|yesterday|recent|new|latest)$", "", m.group(1).strip(" ?.!,"), flags=re.I).strip()
        if who:
            return who
    query = _extract_search_query(text)
    # Category qualifiers ("rtm", the RTM's name) are routing hints, not words that appear in the
    # voicemail email body; strip them so a generic ask does not become a dead FTS term that matches
    # nothing. Once stripped to noise, return None so search_voicemails lists RECENT voicemails instead.
    _compact = re.sub(r"[^a-z0-9]", "", _profile.rtm_name().lower())
    query = re.sub(rf"\b({_rtm_name_rx()}|{re.escape(_compact)}|rtm)\b", " ", query, flags=re.I)
    query = re.sub(r"\b(what|whats|what's|any|new|latest|most|recent|unread|read|previously|check|show|list|search|find|look\s+up|do i have|have i got|are there|is there|there|that|thats|that's|not|but|already|either|in|inbox|my|the|a)\b", " ", query, flags=re.I)
    query = re.sub(r"\b(voice\s*mails?|voicemails?|voice\s*messages?|missed\s+calls?|calls?|messages?)\b", " ", query, flags=re.I)
    query = re.sub(r"\s+", " ", query).strip(" ?.!,")
    if not query or len(query) < 2 or query.lower() in {"any", "new", "latest", "recent", "unread", "most"}:
        return None
    return query


def _local_status_plan(prompt: str, *, already_ran: bool = False) -> dict[str, Any] | None:
    """Deterministic pre-plans for tiny status/audit turns where using the model to choose a
    tool is unnecessary and brittle. Keep this deliberately narrow: it only covers the redacted
    reports added for the third-pass underserved features, so ordinary tool planning still goes
    through the model catalogue.
    """
    if already_ran:
        return None
    t = (prompt or "").lower()
    if _looks_like_v3_voicemail_lookup(t):
        return {"tool": "v3_coordination", "args": {"mode": "voicemail"}}
    if _looks_like_voicemail_lookup(t):
        q = _extract_voicemail_query(prompt)
        args: dict[str, Any] = {"limit": 10}
        if q:
            args["query"] = q
        return {"tool": "search_voicemails", "args": args}
    # Natural-language item search should not depend on the local model guessing the right tool;
    # this is the file/item model's front door. Keep acting verbs out so "delete/forward/send" never
    # turns into an accidental search-and-act chain.
    if re.search(r"\b(find|search(?: for)?|look for|look up|show me|where (?:is|are)|do i have|have i got|is there|are there)\b", t) \
            and re.search(r"\b(file|doc|document|email|e-mail|attachment|voicemail|notification|message|invoice|demand|service charge|rtm|bug|install|watchdog|scheduler)\b", t) \
            and not re.search(r"\b(delete|send|forward|archive|apply|fix|open the garage|turn on|turn off)\b", t):
        return {"tool": "search_files", "args": {"query": _extract_search_query(prompt), "limit": 10}}
    if re.search(r"\b(what can i do with|actions? (?:for|on)|available actions|tell me about)\b", t) and re.search(r"\b(result|item|that|this|it|first|second|third|[a-z]\d{2}|[a-z][0-9a-f]{6})\b", t):
        return {"tool": "item_actions", "args": {"ordinal": _ordinal_from_prompt(prompt)}}

    if re.search(r"(guest|guests).{0,60}(privacy|boundary|boundaries|isolation|private|leak|state|access)", t) \
            or re.search(rf"(can|could).{{0,40}}({_owner_name_rx()}|owner).{{0,40}}(read|see|access).{{0,40}}(guest|guests)", t):
        return {"tool": "guest_privacy", "args": {}}
    if re.search(r"(conversation|chat|context|memory).{0,70}(continuity|health|persist|persistence|survive|load.?bearing|reload|restart)", t) \
            or re.search(r"(check|audit|verify).{0,50}(conversation|chat).{0,50}(context|continuity|history)", t):
        return {"tool": "conversation_continuity", "args": {}}
    if re.search(r"\b(memory|ram|rss|cpu|process|thread|loop|event[- ]?loop|ollama|model)\b", t) and re.search(r"\b(status|health|pressure|stuck|looping|exhaust|exhausting|high|runaway|hung|hanging|check|audit)\b", t):
        return {"tool": "system_status", "args": {"area": "resources"}}
    if re.search(r"\b(log bug)\b", t):
        return {"tool": "log_bug", "args": {"text": prompt}}
    if re.search(r"\b(add bug detail)\b", t):
        from .services.bug_workflow import parse_bug_id
        return {"tool": "add_bug_detail", "args": {"bug_id": parse_bug_id(prompt) or "", "text": prompt}}
    if re.search(r"\b(diagnose bug)\b", t):
        from .services.bug_workflow import parse_bug_id
        return {"tool": "diagnose_bug", "args": {"bug_id": parse_bug_id(prompt) or "", "text": prompt}}
    if re.search(r"\b(attempt fix)\b", t):
        from .services.bug_workflow import parse_bug_id
        return {"tool": "attempt_bug_fix", "args": {"bug_id": parse_bug_id(prompt) or ""}}
    if re.search(r"\b(apply fix)\b", t):
        from .services.bug_workflow import parse_bug_id
        return {"tool": "apply_bug_fix", "args": {"bug_id": parse_bug_id(prompt) or ""}}
    if re.search(r"\b(bug status|show bug|list bugs)\b", t):
        from .services.bug_workflow import parse_bug_id
        return {"tool": "bug_status", "args": {"bug_id": parse_bug_id(prompt) or "", "text": prompt}}
    if (re.search(r"(data dump|mongoose dump|journal dump|bug dump|support dump|wellness dump|dump my|export my data|export my|download my (data|emails?|tasks?|notes?|notifications?|diary|whatsapp|journal))", t)
            or re.search(r"\bgive me a (data |journal |bug |support |mongoose )?dump\b", t)):
        return {"tool": "data_dump", "args": {"text": prompt}}
    if re.search(r"(self[- ]?journal|mindfulness|wellness|structured.{0,20}check[- ]?in|brief.{0,20}check[- ]?in)", t) and re.search(r"(remind|nudge|schedule|guests?|deep brain|important)", t):
        return {"tool": "wellness_checkins", "args": {"action": "configure", "owner": True, "guests": True, "prompt": prompt}}
    return None


def _parse_plan(raw: str) -> dict[str, Any]:
    text = raw.strip()
    # Strip accidental code fences.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        plan = json.loads(text)
        if isinstance(plan, dict):
            return plan
    except Exception:
        pass
    # Tolerant fallback: a model may wrap the JSON in a sentence ("Sure, here's the
    # plan: {...}"). Extract the first balanced {...} object and try that, so a
    # correct plan with a little surrounding prose still parses. This matches what a
    # forgiving real router should accept, not just the benchmark.
    obj = _extract_first_json_object(text)
    if obj is not None:
        try:
            plan = json.loads(obj)
            if isinstance(plan, dict):
                return plan
        except Exception:
            pass
    return {"tool": None}


def _extract_first_json_object(text: str) -> str | None:
    """Return the substring of the first balanced top-level {...} object, honouring
    quotes/escapes so braces inside strings don't confuse the matcher."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _compact_result(result: Any, *, max_chars: int = 600) -> Any:
    """Shrink a tool result for inclusion in the planner prompt. The planner needs the GIST of what a
    prior step returned (did it work, what kind of thing came back), not the full payload: a 500-row
    search result re-serialized into every later step is what made the prompt grow unboundedly across
    a chain. Keep status/error and a short preview; truncate long bodies and large lists."""
    if isinstance(result, dict):
        out: dict[str, Any] = {}
        # Preserve the decision-relevant keys in full (they're small and the planner reasons on them).
        for k in ("ok", "error", "stop_reason", "verified", "created", "sent_to", "unchanged"):
            if k in result:
                out[k] = result[k]
        # Summarise list-shaped payloads by count + a tiny preview rather than dumping every item.
        for k, v in result.items():
            if k in out:
                continue
            if isinstance(v, list):
                out[k] = {"count": len(v), "preview": v[:2]}
            elif isinstance(v, (dict,)):
                out[k] = {"keys": sorted(v.keys())[:8]}
            else:
                out[k] = v
        blob = json.dumps(out, default=str)
        if len(blob) > max_chars:
            blob = blob[:max_chars] + "…"
        return blob
    blob = json.dumps(result, default=str)
    return blob if len(blob) <= max_chars else blob[:max_chars] + "…"


def _summarise_steps_for_planner(steps: list[dict[str, Any]], *, total_budget: int = 2400) -> str:
    """Serialise prior steps for the planner prompt under a hard total character budget, newest-last.
    Each step keeps its tool + args + a COMPACT result. If the budget is exceeded we drop the OLDEST
    steps (keeping the recent ones the planner most needs), so a long side-quest can't grow the prompt
    without bound."""
    rendered = [
        {"tool": s.get("tool"), "args": s.get("args"), "result": _compact_result(s.get("result"))}
        for s in steps
    ]
    # Walk newest-first, keep within budget, then restore chronological order.
    kept: list[dict[str, Any]] = []
    used = 0
    for item in reversed(rendered):
        cost = len(json.dumps(item, default=str))
        if kept and used + cost > total_budget:
            break
        used += cost
        kept.append(item)
    kept.reverse()
    return json.dumps(kept, default=str)


def _is_data_lookup(prompt: str) -> bool:
    """Is the user asking to look up stored data (voicemails, messages, emails, reminders, calendar,
    files)? Such questions must be answered from a tool, never free-associated, or Mongoose fabricates
    items that don't exist (the 'one voicemail from Sam' hallucination)."""
    import re as _re
    p = (prompt or "").lower()
    nouns = (r"voicemail", r"message", r"email", r"e-mail", r"reminder", r"calendar", r"event",
             r"appointment", r"meeting", r"notification", r"draft", r"text from", r"missed call",
             r"unread", r"inbox", r"to-?do", r"task")
    asky = (r"\bany\b", r"\bdo i have\b", r"\bare there\b", r"\bwhat'?s? on\b", r"\bcheck\b",
            r"\bshow\b", r"\bhow many\b", r"\bdid .* (call|message|email|text|send)\b",
            r"\banything from\b", r"\bwhat did\b")
    has_noun = any(_re.search(n, p) for n in nouns)
    has_ask = any(_re.search(a, p) for a in asky)
    # Some phrasings ARE lookups on their own, even without a separate data noun: asking whether a
    # named person contacted you ("did Sam call", "anything from Jordan", "has Casey messaged me").
    standalone = (r"\bdid \w+ (call|message|email|text|write|ring)\b",
                  r"\banything from \w+", r"\bhas \w+ (called|messaged|emailed|texted|written)\b",
                  r"\bheard from \w+")
    if any(_re.search(s, p) for s in standalone):
        return True
    return has_noun and has_ask


def _salient_result_facts(res: dict[str, Any]) -> str:
    """Extract the load-bearing facts from a tool result (counts, emptiness) so the AUTHORITATIVE
    OUTCOME states what was actually found, not just that the tool ran. This is what stops the model
    inventing content for an empty list: e.g. 'you have one voicemail from Sam' when there are zero."""
    parts: list[str] = []
    list_keys = ("voicemails", "messages", "results", "emails", "events", "reminders", "drafts",
                 "items", "matches", "candidates", "pending_drafts", "notifications")
    for k in list_keys:
        v = res.get(k)
        if isinstance(v, list):
            n = len(v)
            noun = k.replace("_", " ")
            if n == 0:
                parts.append(f"EXACTLY 0 {noun} (none: do not invent any)")
            else:
                parts.append(f"EXACTLY {n} {noun}")
    stats = res.get("stats")
    if isinstance(stats, dict) and "total" in stats:
        try:
            if int(stats["total"]) == 0 and not any(p.startswith("EXACTLY 0") for p in parts):
                parts.append("total is 0 (nothing found; do not invent any)")
        except (TypeError, ValueError):
            pass
    for k in ("count", "unread"):
        if isinstance(res.get(k), int):
            parts.append(f"{k}={res[k]}")
    return "; ".join(parts)


async def run_tool_turn(prompt: str, sender: str = "owner") -> dict[str, Any]:
    """Multi-step plan -> validate -> execute loop, then phrase.

    `sender` is the identity of the person whose turn this is ('owner' or 'guest:<device>').
    It is injected into the messaging tools so a guest's send/read acts as the guest, never
    as the owner (which would both misroute and leak the owner's messages).

    The tool model may call tools one at a time, seeing each result before the
    next call, up to MAX_STEPS. Guardrails: a hard step ceiling, dedupe of
    identical (tool, args) calls, and an audit entry per step. If no tool is
    ever chosen the turn degrades to a normal persona reply.

    Backwards compatible: a request needing one tool runs the loop once and
    finishes, producing the same shape as before plus a `steps` list.
    """
    # Chat-activity ping: the proactive music schedule only advances while the owner is actively
    # chatting (it freezes during silence and resumes on return). Owner turns only; never guests.
    if sender == "owner":
        try:
            from .services import music_taste as _mt
            _mt.note_chat_activity(_mt.OWNER_SCOPE)
        except Exception:
            pass
    system = _PLANNER_SYSTEM + _tool_catalogue()
    steps: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    stop_reason = "done"

    for _ in range(_resolve_max_steps()):
        # Build a planner prompt that includes the work done so far.
        if steps:
            history = _summarise_steps_for_planner(steps)
            planner_input = (
                f"User request: {prompt}\n"
                f"Tools already run this turn (JSON): {history}\n"
                "Decide the next tool call, or finish with {\"tool\": null}."
            )
        else:
            planner_input = prompt

        local_plan = _local_status_plan(prompt, already_ran=bool(steps))
        local_plan_used = local_plan is not None
        if local_plan_used:
            plan = local_plan or {"tool": None}
        else:
            raw_plan = await ask_ollama(planner_input, system=system, role="tool")
            plan = _parse_plan(raw_plan)
        tool_name = plan.get("tool")

        if not tool_name:
            stop_reason = "model_finished"
            break

        if tool_name not in TOOLS:
            audit("tools", "plan_unknown_tool", False, reason=str(tool_name),
                  payload={"prompt_len": len(prompt or "")})
            steps.append({"tool": tool_name, "args": {}, "result": {"ok": False, "error": "unknown tool, rejected"}, "rejected": True})
            stop_reason = "rejected_tool"
            break

        args = plan.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        # Prompt-injection guard: if a read tool has already run this turn, its
        # results may contain untrusted content. Refuse an acting tool unless the
        # user's ORIGINAL prompt explicitly requested this kind of action.
        read_tool_used = any(s.get("tool") in READ_TOOLS for s in steps)
        if tool_name in ACTING_TOOLS and read_tool_used and not _user_explicitly_requested_action(prompt, tool_name):
            audit("tools", "acting_blocked_after_read", False, route=tool_name,
                  reason="acting tool blocked after read tool (injection guard)",
                  payload={"step": len(steps) + 1})
            steps.append({"tool": tool_name, "args": args,
                          "result": {"ok": False, "error": "blocked: acting tool after read tool without explicit user request"},
                          "rejected": True})
            stop_reason = "acting_blocked"
            break

        # Dedupe: refuse to run the exact same call twice in one turn.
        call_sig = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
        if call_sig in seen_calls:
            stop_reason = "repeat_call"
            break
        seen_calls.add(call_sig)

        # Give the home-control tool the user's RAW prompt so it can hand ambiguous phrasing to HA's own
        # Assist (which knows areas/aliases/devices) instead of guessing an entity_id. Only the acting
        # home tool gets this; other tools receive exactly the planned args.
        if tool_name == "control_home":
            args = {**args, "_raw_prompt": prompt}
        # Messaging tools act AS the person whose turn this is. A guest must send/read as their own
        # participant, never as the owner. This is applied BEFORE the sensitive-tier check so a proposed
        # message_person/recommend_song captures _sender in its confirmation token and executes as the
        # right person when confirmed.
        if tool_name in ("message_person", "check_messages", "recommend_song"):
            args = {**args, "_sender": sender}

        # Sensitive tier: never execute inline. Return a PROPOSAL (what would happen + a confirmation
        # token) and stop. The user must explicitly confirm via confirm_sensitive_action in a follow-up.
        if tool_name in SENSITIVE_TOOLS:
            from .services.sensitive_tools import propose_sensitive_action
            proposal = propose_sensitive_action(tool_name, args)
            audit("tools", "sensitive_proposed", True, route=tool_name,
                  payload={"arg_keys": sorted(args.keys()), "step": len(steps) + 1})
            steps.append({"tool": tool_name, "args": args, "result": proposal, "proposal": True})
            stop_reason = "needs_confirmation"
            break

        executor: ToolExecutor = TOOLS[tool_name]["executor"]
        result = await executor(args)
        # Audit which tool ran and its arg keys, but not raw query/content values
        # (search queries can contain sensitive email/file terms).
        audit("tools", "execute", bool(result.get("ok", True)), route=tool_name,
              payload={"arg_keys": sorted(args.keys()), "step": len(steps) + 1})
        steps.append({"tool": tool_name, "args": args, "result": result})
        if local_plan_used and tool_name in {"guest_privacy", "conversation_continuity", "wellness_checkins", "log_bug", "add_bug_detail", "diagnose_bug", "bug_status", "attempt_bug_fix", "apply_bug_fix", "search_files", "item_actions"}:
            stop_reason = "local_status_complete"
            break
    else:
        # Loop exhausted MAX_STEPS without the model finishing.
        stop_reason = "max_steps"

    executed = any(not s.get("rejected") for s in steps)

    if not executed:
        # No real tool ran. For DATA-LOOKUP questions (voicemails, messages, emails, reminders,
        # calendar, etc.) the model must NOT free-answer: that's exactly how 'you have one voicemail
        # from Sam' gets fabricated when there are none. If the prompt is asking to look something up
        # and no tool ran, the model has no grounding, so we must not let it invent. Instead, answer
        # from a constrained prompt that forbids inventing specifics.
        if _is_data_lookup(prompt):
            guarded = (
                f"The user asked: {prompt}\n"
                "You did NOT retrieve any data for this. You therefore have NO information about "
                "specific voicemails, messages, emails, reminders, or events. Do NOT invent any "
                "specifics (no names, no counts, no senders, no contents). Reply briefly that you "
                "couldn't check just now / nothing was retrieved, and offer to look. Never claim there "
                "is or isn't a specific item."
            )
            reply = await ask_persona(guarded, role="chat")
        else:
            # Non-lookup chit-chat: a normal persona reply is fine.
            reply = await ask_persona(prompt, role="chat")
        rejected = next((s["tool"] for s in steps if s.get("rejected")), None)
        out = {"tool": None, "reply": reply, "executed": False, "steps": steps, "stop_reason": stop_reason}
        if rejected:
            out["rejected_tool"] = rejected
        return out

    # Phrase the combined results in Mongoose's voice.
    ran = [s for s in steps if not s.get("rejected")]
    summary = json.dumps([{"tool": s["tool"], "result": s["result"]} for s in ran])
    # GROUND TRUTH: compute the success/failure of each step DETERMINISTICALLY from the result, and
    # state it as an authoritative fact the model must not contradict. Without this, the model can
    # misread a successful result as a failure (e.g. report a home-control action failed when it
    # returned ok:True): exactly the hallucinated-failure bug. The model phrases; it does not get to
    # re-decide whether the action worked.
    outcome_lines = []
    for s in ran:
        res = s.get("result") or {}
        ok = res.get("ok", True)
        tool = s.get("tool")
        if ok:
            detail = res.get("detail") or res.get("message") or ""
            facts = _salient_result_facts(res)
            line = f"- {tool}: SUCCEEDED"
            if facts:
                line += f": {facts}"
            elif detail:
                line += f": {detail}"
            outcome_lines.append(line)
        else:
            err = res.get("error") or "failed"
            outcome_lines.append(f"- {tool}: FAILED: {err}")
    ground_truth = "\n".join(outcome_lines)
    phrasing_prompt = (
        f"The user said: {prompt}\n"
        f"You ran these tools and got these results (JSON): {summary}\n\n"
        f"AUTHORITATIVE OUTCOME (computed from the results: you MUST NOT contradict this):\n{ground_truth}\n\n"
        "Reply to the user directly and honestly.\n"
        "Rules:\n"
        "- The AUTHORITATIVE OUTCOME above is the truth about what happened. If it says SUCCEEDED, the "
        "action worked: say so; never claim it failed. If it says FAILED, say it failed and why.\n"
        "- Do NOT let any earlier turn, prior error, or remembered state override the outcome above. "
        "Judge ONLY this turn's results.\n"
        "- If the results contain full transcript text, quote it verbatim: do NOT summarise it into one sentence.\n"
        "- If transcript_status is 'pending_no_audio' or 'pending', say the transcript is not available yet "
        "(voice transcription may not be set up) and do not guess at the content.\n"
        "- If a conditional action was requested ('turn on X if Y is mentioned') and Y was not found in the results, "
        "say clearly that the condition was not met and the action was not taken.\n"
        "- If a conditional action was taken, say what you found and what you did.\n"
        "- State only what the results support. Do not invent details, content, or outcomes."
    )
    reply = await ask_persona(phrasing_prompt, role="chat")
    last = ran[-1]
    return {
        "tool": last["tool"],
        "args": last["args"],
        "result": last["result"],
        "steps": steps,
        "reply": reply,
        "executed": True,
        "stop_reason": stop_reason,
    }
