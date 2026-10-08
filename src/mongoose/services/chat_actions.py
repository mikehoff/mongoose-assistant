"""Chat handlers for the ACTION intents the resolver now recognises: capture a task, list tasks,
mark a flat paid, send a receipt. These are the concrete "do it from chat" behaviours whose ABSENCE
was the routing disaster, the classifier had no target to route to, so captures fell through and
"...rtm task" was swallowed by the arrears view. Kept deliberately small and deterministic; the
resolver decides WHICH, these decide HOW.

No em/en dashes anywhere (house rule). Lazy imports avoid load-time cycles.
"""
from __future__ import annotations

import re

# Strip a capture/reminder lead-in so "remind me to call X" / "add a task to call X" both become
# "call X". Applied repeatedly so stacked lead-ins ("please remind me to ...") fully unwrap.
_LEADIN = re.compile(
    r"^\s*(please\s+|can you\s+|could you\s+|would you\s+)?"
    r"(remind me(\s+to|\s+that|\s+about)?|remember(\s+to|\s+that)?|don'?t let me forget(\s+to|\s+about)?|"
    r"i need to|i have to|i must|i should|"
    r"note(\s+down)?(\s+that|\s+to)?|jot(\s+it|\s+this|\s+that)?(\s+down)?|write(\s+it|\s+this|\s+that)?\s+down|"
    r"make a note(\s+to|\s+that|\s+of)?|"
    r"add(\s+a| an| another| new)?\s+(task|to-?do|note)(\s+to|\s+for|\s+about|\s+of|\s+that)?|"
    r"(new|another)\s+(task|to-?do|note)|"
    r"keep\s+(it|this|that|these|them)\s+as\s+(an?\s+|my\s+)?(rtm\s+|outstanding\s+|open\s+)*(task|note|to-?do)(\s+to|\s+about|\s+of)?|"
    r"log|save|store|put\s+down)\b[:,]?\s*", re.I)

_ANAPHOR = re.compile(
    r"\bkeep\s+(it|this|that|these|them)\b"
    r"|\b(make|add|log|save|turn|set|put)\s+(it|that|this|these|them)\b"
    r"|^\s*(add|keep|note|log|save)\s+(that|this|it)\b", re.I)

# There is deliberately NO RTM regex here. Chat used to keep its own, it disagreed with the task
# module on the owner's real list, and chat is where he asked the question. See calendar_tasks.rtm_verdict.


def _flat_re() -> "re.Pattern[str]":
    """A typed/spoken flat reference ('3ECF9', '3 ec f 9', '3ec9' with the default profile), built from
    the site profile's blocks: a block number, the block-code letters, an optional F, the flat number."""
    from .. import profile as _profile
    bl = _profile.blocks()
    nums = "|".join(re.escape(n) for n in sorted({b["number"] for b in bl}, key=len, reverse=True))
    letters = "|".join(re.escape(x) for x in sorted({re.sub(r"^[0-9]+", "", b["code"]).lower() for b in bl},
                                                     key=len, reverse=True))
    return re.compile(rf"\b((?:{nums})\s*(?:{letters})\s*f?\s*\d{{1,2}})\b", re.I)


def _rtm_name_re() -> str:
    """The RTM company's name as a loose regex ('example\\s*court' with the default profile)."""
    from .. import profile as _profile
    return r"\s*".join(re.escape(w.lower()) for w in _profile.rtm_name().split())


def _clean_subject(text: str) -> str:
    t = (text or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _LEADIN.sub("", t, count=1).strip()
    t = re.sub(r"\s+(please|thanks|thank you|asap|when you can)\s*[.!]*$", "", t, flags=re.I).strip()
    t = t.strip(" .").strip()
    return t or (text or "").strip()


def resolve_capture(prompt: str, prior_user_text: str = "") -> tuple[str, str]:
    """(task_title, list_name). An anaphoric capture ("keep it as a task") takes its subject from the
    PREVIOUS user turn; a direct capture ("add a task to X") strips the lead-in from the prompt."""
    anaphoric = bool(_ANAPHOR.search(prompt or ""))
    base = prior_user_text if (anaphoric and (prior_user_text or "").strip()) else prompt
    title = _clean_subject(base)
    from .calendar_tasks import guess_is_rtm
    hay = (prompt or "") + " " + (prior_user_text if anaphoric else "")
    list_name = "RTM" if guess_is_rtm(hay) else "Tasks"
    return (title[:200] or "task", list_name)


def capture_task(prompt: str, prior_user_text: str = "", *, untimed_reminder: bool = False) -> str:
    from .calendar_tasks import add_task, TaskIn, update_task, guess_is_rtm
    title, list_name = resolve_capture(prompt, prior_user_text)
    res = add_task(TaskIn(source="mongoose", list_name=list_name, title=title, status="open"))
    # Seed the RTM flag from the same signal that chose the list, plus a title check, so a chat-created
    # task arrives classified instead of joining the unclassified pile. Advisory: the owner can flip it
    # on the Tasks screen. Never fatal , a failed label must not lose the task itself.
    try:
        tid = int((res or {}).get("id") or 0)
        if tid:
            update_task(tid, is_rtm=(list_name == "RTM") or guess_is_rtm(title))
    except Exception:
        pass
    tag = "RTM " if list_name == "RTM" else ""
    if untimed_reminder:
        return (f"I could not spot a time, so I have kept “{title}” as an open {tag}task on your "
                f"list. Give me a time (like ‘at 9am tomorrow’) and I will turn it into a reminder.")
    return (f"Done. Kept it as an open {tag}task: “{title}”. It sits on your list now; say a "
            f"time whenever you want a nudge.")


# Tokens that make up a bare nudge with NO task subject ("nudge me next week", "remind me tomorrow").
_NUDGE_ONLY = set((
    "nudge remind reminder me us my again please soon later then that this it about on at in a an the "
    "next week weeks tomorrow today tonight morning afternoon evening month months fortnight day days "
    "hour hours minute minutes mins am pm oclock noon midday midnight and or of by "
    "monday tuesday wednesday thursday friday saturday sunday weekend weekday").split())


def attach_nudge_to_recent_task(prompt: str) -> str | None:
    """A BARE 'nudge me next week' (a time phrase with no task subject) attaches its reminder to the most
    recent open task, rather than creating an orphan alarm or, the bug M caught, confabulating a nudge
    that was never set. Returns an honest confirmation, or None if this is not a bare nudge (so the normal
    reminder/capture path runs, e.g. 'remind me to call Bob next week')."""
    from .time_parse import parse_when
    parsed = parse_when(prompt or "")
    if not parsed or not parsed.get("fire_at"):
        return None
    residual = _clean_subject(prompt)   # strip capture/reminder lead-ins
    toks = re.findall(r"[a-z]+", residual.lower())
    if any(t not in _NUDGE_ONLY for t in toks):
        return None                     # a real subject is present: not a bare nudge
    from .calendar_tasks import most_recent_open_task, set_task_reminder
    t = most_recent_open_task()
    if not t:
        return None                     # nothing to attach to; let the normal path make a plain reminder
    fire = parsed["fire_at"]
    set_task_reminder(t["id"], fire.isoformat())
    when = fire.strftime("%a %d %b at %H:%M").replace(" 0", " ")
    return (f"Set. I will nudge you about “{t.get('title')}” on {when}. "
            f"That reminder is really on the calendar now, not just promised.")


def list_tasks_reply(prompt: str) -> str:
    """The open list, ordered by what actually needs attention.

    The old reply printed the first twelve rows and "(+60 more)", which told the owner nothing he could
    act on. Worse, it labelled everything "(due ...)" while reading reminder_at, so a self-set nudge
    read as a hard deadline: "confirmation statement by 31 july" showed "due 2026-07-24" because that
    is when he asked to be poked. A filing deadline and a poke are not the same fact.
    """
    from datetime import datetime, timedelta, timezone
    from .calendar_tasks import list_tasks
    rtm_only = bool(re.search(rf"\brtm\b|\b{_rtm_name_re()}\b", prompt or "", re.I))
    tasks = list_tasks(limit=500, scope="rtm" if rtm_only else "all")
    if not tasks:
        return "You have no outstanding RTM tasks." if rtm_only else "You have no outstanding tasks."

    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=7)

    def when(t):
        """(datetime, label) for a task, preferring a real deadline over a self-set nudge."""
        for field, word in (("due_at", "due"), ("reminder_at", "nudge")):
            raw = t.get(field)
            if not raw:
                continue
            try:
                d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)), word
            except Exception:
                continue
        return None, ""

    overdue, week, later, undated = [], [], [], []
    for t in tasks:
        d, word = when(t)
        title = str(t.get("title") or "(untitled)").strip()
        if d is None:
            undated.append(title)
        else:
            stamp = d.strftime("%a %d %b").replace(" 0", " ")
            line = f"{title} ({word} {stamp})"
            (overdue if d < now else week if d <= soon else later).append(line)

    what = "RTM task" if rtm_only else "task"
    head = f"{len(tasks)} open {what}" + ("s" if len(tasks) != 1 else "")
    parts = [head + ":"]
    for label, group, cap in (("Overdue", overdue, 8), ("Next 7 days", week, 8), ("Later", later, 5)):
        if group:
            parts.append(f"\n{label} ({len(group)}):")
            parts += ["• " + x for x in group[:cap]]
            if len(group) > cap:
                parts.append(f"  ...and {len(group) - cap} more")
    if undated:
        parts.append(f"\nNo date ({len(undated)}): " + ", ".join(undated[:6])
                     + (f", and {len(undated) - 6} more" if len(undated) > 6 else ""))
        parts.append("Ask me to set a deadline on any of these, or open Cockpit then Tasks.")
    return "\n".join(parts)


def _extract_target(prompt: str, *, extra_strip: str = "") -> tuple[str, str]:
    """Return ('flat', '1ECF3') or ('name', 'barnes') from a mark-paid / send-receipt command."""
    t = prompt or ""
    m = _flat_re().search(t)
    if m:
        return ("flat", re.sub(r"\s+", "", m.group(1)).upper())
    strip = (r"\b(mark|record|set|log|register|note|flag|send|email|resend|please|as|the|a|an|flat|"
             r"payment|paid|received|receipt|his|her|their|to|in|it|that|this|of|for|off|over|"
             + extra_strip + r")\b")
    s = re.sub(strip, " ", t, flags=re.I)
    s = re.sub(r"'s\b", "", s)
    s = re.sub(r"[^a-zA-Z\s]", " ", s)
    name = " ".join(w for w in s.split() if len(w) > 1).strip()
    return ("name", name)


def _match_leaseholders(prompt: str) -> "list[dict[str, str]]":
    """Every real leaseholder the message names, via the shared EntityMatcher mechanism (allowlist match
    against the real roster, multi-person aware). Marking the WRONG flat paid is the risk the matcher's
    distinctiveness threshold guards against."""
    from .entity_matcher import LeaseholderMatcher
    return [{"flat_id": e.id, "name": e.label} for e in LeaseholderMatcher().match(prompt)]


async def _mark_one_paid(target: dict, wants_split: bool) -> str:
    from .status_tools import tool_rtm_mark_paid
    res = await tool_rtm_mark_paid(target)
    who = target.get("leaseholder") or target.get("flat") or "that"
    if not res.get("ok"):
        if res.get("ambiguous"):
            cands = ", ".join(f"{c.get('flat_id')} ({c.get('leaseholder_name')})" for c in res.get("candidates", []))
            return f"{who}: matches more than one flat ({cands}); which one?"
        return f"{who}: could not mark paid ({res.get('error', 'unknown')})."
    fid = res.get("flat_id")
    if res.get("already_paid"):
        line = f"{fid} is already fully paid (balance {res.get('balance', '£0.00')})."
    else:
        rc = res.get("receipt_ok")
        rmsg = (" Receipt generated." if rc is True
                else f" (receipt not generated: {res.get('receipt_error')})" if rc is False else "")
        line = (f"Recorded {res.get('amount')} for {fid} against {res.get('document_number')}, dated today. "
                f"Balance now {res.get('balance', '£0.00')}.{rmsg}")
    if wants_split and fid:
        try:
            from .rtm_trust import subaccount_split
            sp = subaccount_split(fid)
            split = sp.get("subaccount_split") if isinstance(sp, dict) else None
            if isinstance(split, dict):
                line += (f" Subaccount split: reserve {split.get('reserve_fund_trust')}, "
                         f"service charge {split.get('service_charge_trust')} (total {sp.get('total')}).")
        except Exception:
            pass
    return line


async def mark_paid_from_chat(prompt: str) -> str:
    kind, val = _extract_target(prompt)
    people = _match_leaseholders(prompt)
    wants_split = bool(re.search(r"(?i)\b(split|sub-?account|reserve)\b", prompt or ""))
    if kind == "flat":
        targets = [{"flat": val}]
    elif people:
        targets = [{"leaseholder": p["name"]} for p in people]   # one or more real people, matched robustly
    elif val:
        targets = [{"leaseholder": val}]
    else:
        return "Who paid? Tell me the flat or leaseholder, for example 'mark Barnes as paid'."
    lines = [await _mark_one_paid(t, wants_split) for t in targets]
    tail = ("" if not any("Recorded" in ln for ln in lines)
            else " Say 'send <flat> receipt' to email any of these.")
    return "\n".join(lines) + tail


async def _send_one_receipt(target: dict) -> str:
    from .status_tools import tool_rtm_send_receipt
    who = target.get("leaseholder") or target.get("flat") or "that"
    res = await tool_rtm_send_receipt(target)
    if not res.get("ok"):
        if res.get("ambiguous"):
            cands = ", ".join(f"{c.get('flat_id')} ({c.get('leaseholder_name')})" for c in res.get("candidates", []))
            return f"{who}: matches more than one flat ({cands}); which one?"
        return f"{who}: could not send receipt ({res.get('error', 'no receipt on file yet')})."
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    email = data.get("email") or res.get("email") or "the leaseholder"
    return f"Sent {who}'s receipt to {email}."


async def send_receipt_from_chat(prompt: str) -> str:
    # Same allowlist mechanism as mark-paid, so "send Patil and O'Brien their receipts" resolves to the
    # real people instead of one bogus name.
    kind, val = _extract_target(prompt)
    people = _match_leaseholders(prompt)
    if kind == "flat":
        targets = [{"flat": val}]
    elif people:
        targets = [{"leaseholder": p["name"]} for p in people]
    elif val:
        targets = [{"leaseholder": val}]
    else:
        return "Whose receipt? Tell me the flat or leaseholder, for example 'send Barnes his receipt'."
    # Serialise multi-recipient sends with a small gap: two receipts fired in the SAME second let PHP mail()
    # silently drop the second one. A short spacing (plus SMTP + the delivery-verification job) prevents it.
    import asyncio as _aio
    lines = []
    for i, t in enumerate(targets):
        if i:
            await _aio.sleep(2)
        lines.append(await _send_one_receipt(t))
    return "\n".join(lines)
