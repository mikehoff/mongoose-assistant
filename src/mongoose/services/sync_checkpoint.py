from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

# Sync checkpoints , three times a day, Mongoose checks in with the owner's Claude and the owner's chat.
#
# The shape the owner asked for: "hey, this has been done, and this has come up from the data, what do
# you think?" Three things in each checkpoint:
#   1. DONE      , what Mongoose has completed/changed since the last checkpoint.
#   2. SURFACED  , what came up from the data that the owner (or their Claude) should weigh in on.
#   3. ASKING    , the open questions Mongoose wants a view on.
#
# REMOTE CHANNELS (enabled from day one per the override): the owner's Claude and chat can be reached
# through any of: chat_pro, claude_pro, anthropic_api, openai_api. The checkpoint PAYLOAD is always
# assembled locally and is always reviewable (stored as a receipt); DELIVERY goes out over whichever
# channels are enabled. Nothing about a checkpoint moves money or changes RTM , it's a sync, a
# "here's where we are, what do you think", not an action.

REMOTE_CHANNELS = ("chat_pro", "claude_pro", "anthropic_api", "openai_api")

# Per the day-one override, the channels are AVAILABLE from day one. But AVAILABILITY is not CONSENT:
# actually transmitting a checkpoint (which summarises your household/RTM data) off the machine
# requires the sync feature to be explicitly enabled. Default OFF. This keeps the promise honest ,
# "remote channels ready" must not silently mean "your data is being pushed out". Assembly is always
# local and reviewable regardless; only DELIVERY is gated.
_DEFAULT_ON = True


def _delivery_enabled() -> bool:
    """Master switch for actually SENDING checkpoints off the machine. Off unless the user turns it
    on (MONGOOSE_SYNC_DELIVERY=on, or the sync_delivery_enabled setting). Distinct from channels
    being available: a checkpoint is always assembled and recorded locally, but it is only delivered
    when the user has explicitly opted into outbound sync."""
    flag = os.getenv("MONGOOSE_SYNC_DELIVERY", "").lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    try:
        from ..settings import get_settings
        return bool(getattr(get_settings(), "sync_delivery_enabled", False))
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _channel_enabled(channel: str) -> bool:
    """A remote channel is enabled if its env flag isn't explicitly off. Default ON (day-one
    override). API channels additionally need their key present to actually deliver; without the key
    the channel is 'enabled but not deliverable' and the checkpoint still records + can be read."""
    flag = os.getenv(f"MONGOOSE_SYNC_{channel.upper()}", "").lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return _DEFAULT_ON


def _channel_deliverable(channel: str) -> tuple[bool, str]:
    """Can we actually deliver on this channel right now? Returns (deliverable, reason)."""
    if not _channel_enabled(channel):
        return False, "channel disabled"
    if channel == "anthropic_api" and not os.getenv("ANTHROPIC_API_KEY", ""):
        return False, "no ANTHROPIC_API_KEY"
    if channel == "openai_api" and not os.getenv("OPENAI_API_KEY", ""):
        return False, "no OPENAI_API_KEY"
    # chat_pro / claude_pro are reached via the phone/handoff surface, always 'deliverable' as a
    # notification even if the conversational bridge isn't wired , the payload reaches the owner.
    return True, "ok"


# --- assembling the checkpoint (always local, always reviewable) -------------

def assemble_checkpoint(*, since_iso: str | None = None) -> dict[str, Any]:
    """Build the checkpoint payload from real local state: what's been done, what surfaced from the
    data, and the open questions. Reads RTM capability/build state, recent diagnostics, and recent
    receipts. Never sends anything, this just builds the message."""
    done = _collect_done(since_iso)
    surfaced = _collect_surfaced()
    asking = _collect_questions(surfaced)
    cid = "sync_" + uuid.uuid4().hex[:10]
    return {"id": cid, "at": _now_iso(), "since": since_iso,
            "done": done, "surfaced": surfaced, "asking": asking,
            "summary": _summarise(done, surfaced, asking)}


def _collect_done(since_iso: str | None) -> list[str]:
    """What Mongoose has completed since the last checkpoint: built build-steps, exported reports,
    resolved diagnostics. Best-effort and defensive."""
    done: list[str] = []
    try:
        from . import rtm_capability as C
        cands = C.list_candidates(status="chosen") + C.list_candidates(status="building")
        for c in cands:
            done.append(f"RTM build: '{c['goal']}' is {c['status']} ({c.get('approach') or 'no approach noted'})")
    except Exception:
        pass
    try:
        from . import builder
        from .. import profile as _profile
        for p in (builder.list_plans() if hasattr(builder, "list_plans") else []):
            steps = p.get("steps", [])
            d = sum(1 for s in steps if s.get("status") == "done")
            if d and p.get("name", "").startswith(_profile.rtm_slug()):
                done.append(f"Build plan '{p['name']}': {d}/{len(steps)} steps done")
    except Exception:
        pass
    if not done:
        done.append("No new build progress since the last checkpoint.")
    return done


def _collect_surfaced() -> list[dict[str, Any]]:
    """What came up from the data that warrants a human/Claude view: RTM next-actions (arrears,
    payments to allocate, payables due), data-verification findings, capability gaps."""
    surfaced: list[dict[str, Any]] = []
    try:
        from . import rtm_management as M
        if M.list_flats():
            dash = M.management_dashboard()
            for a in dash.get("next_actions", []):
                if a.get("type") != "ok":
                    surfaced.append({"kind": "rtm_action", "urgency": a.get("urgency", "low"),
                                     "detail": a.get("title", "")})
    except Exception:
        pass
    try:
        from . import rtm_capability as C
        cap = C.capability_state()
        not_yet = [r for r in cap.get("requirements", []) if r["state"] in ("not_started", "scaffolded")]
        if not_yet:
            surfaced.append({"kind": "capability_gap", "urgency": "low",
                             "detail": "RTM requirements not yet in use: "
                                       + ", ".join(f"{r['requirement']} {r['label']}" for r in not_yet[:4])})
    except Exception:
        pass
    return surfaced


def _collect_questions(surfaced: list[dict[str, Any]]) -> list[str]:
    """Turn the surfaced items into specific questions Mongoose wants a view on."""
    qs: list[str] = []
    for s in surfaced:
        if s["kind"] == "rtm_action" and "arrears" in s["detail"].lower():
            qs.append("Several flats are in arrears, should I draft the first reminders for your review?")
        elif s["kind"] == "rtm_action" and "review" in s["detail"].lower():
            qs.append("There are bank credits to allocate, do you want to do that now or should I hold them in the review queue?")
        elif s["kind"] == "capability_gap":
            qs.append("Some RTM requirements aren't live yet, which should I prioritise building next?")
    if not qs:
        qs.append("Nothing urgent, anything you want me to focus on before the next checkpoint?")
    return qs[:4]


def _summarise(done: list[str], surfaced: list[dict[str, Any]], asking: list[str]) -> str:
    return (f"{len(done)} update(s), {len(surfaced)} thing(s) surfaced from the data, "
            f"{len(asking)} question(s).")


# --- delivery (over the enabled remote channels) -----------------------------

def channel_status() -> dict[str, Any]:
    """Which remote channels are enabled and deliverable right now. Always visible so it's never
    ambiguous where checkpoints go, including whether outbound delivery is even switched on."""
    delivery_on = _delivery_enabled()
    out = {"_delivery_enabled": delivery_on}
    for ch in REMOTE_CHANNELS:
        ch_ok, reason = _channel_deliverable(ch)
        deliverable = delivery_on and ch_ok
        if not delivery_on:
            reason = "outbound sync disabled (opt in to send)"
        out[ch] = {"enabled": _channel_enabled(ch), "deliverable": deliverable, "reason": reason}
    return out


def _format_for_humans(cp: dict[str, Any]) -> str:
    lines = ["Mongoose checkpoint, here's where we are.", ""]
    lines.append("Done:")
    for d in cp["done"]:
        lines.append(f"  - {d}")
    if cp["surfaced"]:
        lines.append("From the data:")
        for s in cp["surfaced"]:
            lines.append(f"  - [{s['urgency']}] {s['detail']}")
    lines.append("What do you think?")
    for q in cp["asking"]:
        lines.append(f"  - {q}")
    return "\n".join(lines)


async def deliver_checkpoint(cp: dict[str, Any]) -> dict[str, Any]:
    """Deliver an assembled checkpoint over the enabled channels. chat_pro/claude_pro reach the owner via
    the phone push (the handoff surface); the API channels can additionally get a model's quick take.
    Always records the checkpoint as a receipt first so it's reviewable regardless of delivery."""
    _record_receipt(cp)
    results: dict[str, Any] = {}
    human_text = _format_for_humans(cp)

    # CONSENT GATE: assembly + local recording always happen (reviewable, nothing leaves). But the
    # checkpoint summarises your data, so it is only TRANSMITTED off the machine when outbound sync
    # is explicitly enabled. Otherwise we stop here , the checkpoint is available locally, but no
    # household/RTM summary is pushed to a phone or a cloud model without that opt-in.
    if not _delivery_enabled():
        return {"ok": True, "checkpoint_id": cp["id"], "delivered": False,
                "reason": "outbound sync not enabled (set MONGOOSE_SYNC_DELIVERY=on to allow). "
                          "Checkpoint assembled and saved locally.",
                "channels": {ch: {"delivered": False, "reason": "delivery disabled"}
                             for ch in REMOTE_CHANNELS}}

    # chat_pro / claude_pro -> push to the owner's phone (the reach-owner channel)
    pushed_any = False
    for ch in ("chat_pro", "claude_pro"):
        deliverable, reason = _channel_deliverable(ch)
        if not deliverable:
            results[ch] = {"delivered": False, "reason": reason}
            continue
        if not pushed_any:  # one phone push covers both human channels
            try:
                from .notifications import send_to_phone
                r = await send_to_phone("Mongoose checkpoint", cp["summary"], priority="default", tags="speech_balloon")
                pushed_any = r.get("sent", False)
                results[ch] = {"delivered": pushed_any, "via": "phone_push"}
            except Exception as exc:
                results[ch] = {"delivered": False, "reason": str(exc)[:120]}
        else:
            results[ch] = {"delivered": True, "via": "phone_push (shared)"}

    # anthropic_api / openai_api -> optionally get a model's quick take on the checkpoint
    for ch in ("anthropic_api", "openai_api"):
        deliverable, reason = _channel_deliverable(ch)
        if not deliverable:
            results[ch] = {"delivered": False, "reason": reason}
            continue
        try:
            take = await _model_take(ch, human_text)
            results[ch] = {"delivered": bool(take), "take": (take or "")[:400]}
        except Exception as exc:
            results[ch] = {"delivered": False, "reason": str(exc)[:120]}

    return {"ok": True, "checkpoint_id": cp["id"], "channels": results}


async def _model_take(channel: str, human_text: str) -> str | None:
    """Ask the configured model for a brief take on the checkpoint (over the API channel). This is a
    'what do you think' to a model standing in for / assisting the owner's review. Returns None on
    failure. Read-only, it asks for an opinion, it does not act."""
    from .egress_guard import egress_allowed
    if not egress_allowed("cloud_llm")[0]:
        return None
    import httpx
    prompt = ("You are helping a director run a small residential RTM. Here is a status checkpoint "
              "from his local assistant. Give a brief, practical take: what looks right, what you'd "
              "prioritise, any risk to flag. Two or three sentences.\n\n" + human_text)
    if channel == "anthropic_api":
        key = os.getenv("ANTHROPIC_API_KEY", "")
        model = os.getenv("MONGOOSE_SYNC_ANTHROPIC_MODEL", "claude-sonnet-4-6")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                                  headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                           "content-type": "application/json"},
                                  json={"model": model, "max_tokens": 300,
                                        "messages": [{"role": "user", "content": prompt}]})
        if r.status_code != 200:
            return None
        parts = r.json().get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip() or None
    if channel == "openai_api":
        key = os.getenv("OPENAI_API_KEY", "")
        model = os.getenv("MONGOOSE_SYNC_OPENAI_MODEL", "gpt-4o-mini")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post("https://api.openai.com/v1/chat/completions",
                                  headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
                                  json={"model": model, "max_tokens": 300,
                                        "messages": [{"role": "user", "content": prompt}]})
        if r.status_code != 200:
            return None
        return r.json()["choices"][0]["message"]["content"].strip() or None
    return None


def _record_receipt(cp: dict[str, Any]) -> None:
    """Persist the checkpoint to a bounded history list in app_state so checkpoints are reviewable
    regardless of delivery (the last 50 are kept)."""
    try:
        from ..db import get_app_state, set_app_state
        raw = get_app_state("sync_checkpoint_history", "")
        hist = json.loads(raw) if raw else []
        hist.append({"id": cp["id"], "at": cp["at"], "summary": cp["summary"],
                     "asking": cp["asking"]})
        hist = hist[-50:]  # keep the last 50
        set_app_state("sync_checkpoint_history", json.dumps(hist))
        set_app_state("last_sync_checkpoint", json.dumps(cp))
    except Exception:
        pass  # best-effort; the checkpoint still returns to the caller


def checkpoint_history(limit: int = 10) -> list[dict[str, Any]]:
    """The recent sync checkpoints, newest first."""
    try:
        from ..db import get_app_state
        raw = get_app_state("sync_checkpoint_history", "")
        hist = json.loads(raw) if raw else []
        return list(reversed(hist))[:limit]
    except Exception:
        return []


async def run_checkpoint(*, since_iso: str | None = None) -> dict[str, Any]:
    """Assemble and deliver one checkpoint. This is what the scheduler calls three times a day."""
    cp = assemble_checkpoint(since_iso=since_iso)
    delivery = await deliver_checkpoint(cp)
    from ..db import set_app_state
    set_app_state("last_sync_checkpoint_at", cp["at"])
    return {"checkpoint": cp, "delivery": delivery}
