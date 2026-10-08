from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

from ..db import connect, utc_now_iso, safe_fts_query, audit
from .whatsapp_accounts import (MONGOOSE_SEND, OWNER_READ, account_for_role, owner_number,
                                role_for_session as _role_for_session)

# WhatsApp bridge.
#
# Two paths, with a hard safety boundary between them:
#   INBOUND:  messages are indexed locally (searchable, brief-relevant), like email.
#   OUTBOUND: Mongoose may DRAFT and EDIT messages, but a draft is only ever sent
#             after EXPLICIT human approval. The model never has raw send authority;
#             there is no function that both composes and transmits in one step.
#
# The actual transport (an unofficial WhatsApp route) is deliberately isolated: this
# module owns the store and the approval state machine; a separate, swappable
# `send_fn` does the wire work, so a broken/unofficial route can't corrupt the
# safety model or the local index.

# ── Draft-scoped one-time approval tokens ─────────────────────────────────────────────────────────
# Instead of embedding the reusable master API token in ntfy action button headers (where it would
# be sent to the ntfy server), we mint short-lived, draft-scoped tokens. Each token can only
# approve OR cancel one specific draft, is consumed on first use, and expires after TTL_SECONDS.
# The master token never leaves this machine.

_DRAFT_TOKEN_TTL_SECONDS = 24 * 3600  # 24 hours: long enough for the user to tap; short enough to matter


def _ensure_draft_tokens_table() -> None:
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_draft_tokens (
                token       TEXT PRIMARY KEY,
                draft_id    INTEGER NOT NULL,
                action      TEXT NOT NULL,
                expires_at  REAL NOT NULL,
                used        INTEGER NOT NULL DEFAULT 0
            )
        """)


def _mint_draft_action_token(draft_id: int, *, action: str) -> str:
    """Create a single-use token tied to exactly one draft and one action (approve/cancel).
    Returns the hex token string."""
    _ensure_draft_tokens_table()
    token = secrets.token_hex(16)
    expires = time.time() + _DRAFT_TOKEN_TTL_SECONDS
    with connect() as con:
        con.execute(
            "INSERT INTO whatsapp_draft_tokens (token, draft_id, action, expires_at) VALUES (?,?,?,?)",
            (token, draft_id, action, expires))
    return token


def consume_draft_action_token(token: str, draft_id: int, action: str) -> bool:
    """Validate and consume a draft-scoped action token. Returns True if the token is valid
    (correct draft, correct action, not expired, not previously used). Single-use: marks used=1
    atomically on success so a replayed tap does nothing."""
    _ensure_draft_tokens_table()
    now = time.time()
    with connect() as con:
        row = con.execute(
            "SELECT token, draft_id, action, expires_at, used FROM whatsapp_draft_tokens "
            "WHERE token = ?", (token,)).fetchone()
        if not row:
            return False
        if row["used"] or row["draft_id"] != draft_id or row["action"] != action:
            return False
        if row["expires_at"] < now:
            return False
        # Consume atomically: only mark used if it's still 0 (prevents TOCTOU race)
        n = con.execute(
            "UPDATE whatsapp_draft_tokens SET used = 1 WHERE token = ? AND used = 0",
            (token,)).rowcount
        return n == 1

_PRIVATE = {"high", "secret"}


@dataclass
class WhatsAppMessage:
    chat_id: str
    contact: str
    direction: str            # 'in' | 'out'
    wa_message_id: str
    body: str = ""
    media_kind: str = "none"
    quoted_id: str | None = None
    sent_at: str | None = None
    received_at: str | None = None
    read_status: str | None = None
    privacy: str = "medium"
    provider_session: str = ""     # WAHA session this message arrived on / was sent from
    session_role: str = ""         # owner_read | mongoose_send | "" (legacy/unknown)


def index_message(msg: WhatsAppMessage) -> dict[str, Any]:
    """Index one inbound/outbound WhatsApp message locally. Idempotent on
    wa_message_id."""
    with connect() as con:
        existing = con.execute(
            "SELECT id FROM whatsapp_messages WHERE wa_message_id = ?", (msg.wa_message_id,)).fetchone()
        if existing:
            return {"ok": True, "skipped": "already_indexed", "id": existing["id"]}
        cur = con.execute(
            "INSERT INTO whatsapp_messages(chat_id, contact, direction, wa_message_id, body, "
            "media_kind, quoted_id, sent_at, received_at, read_status, privacy, "
            "provider_session, session_role, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (msg.chat_id, msg.contact, msg.direction, msg.wa_message_id, msg.body,
             msg.media_kind, msg.quoted_id, msg.sent_at, msg.received_at,
             msg.read_status, msg.privacy, msg.provider_session or "", msg.session_role or "",
             utc_now_iso()))
        rowid = cur.lastrowid
        # Local-first: index the body locally regardless of privacy, so your own
        # authenticated search can find it. Masking happens at OUTPUT time based on
        # the surface (see search_messages shared_surface). The plaintext is already
        # stored locally; omitting it from the local index only hurts you, not an
        # attacker.
        con.execute("INSERT INTO whatsapp_fts(rowid, contact, body) VALUES (?, ?, ?)",
                    (rowid, msg.contact or "", msg.body or ""))
    audit("whatsapp", "index", True, payload={"contact": msg.contact, "direction": msg.direction})
    # Learn the contact->chat_id mapping so 'draft to <name>' can later resolve to a
    # real chat id. Only learn from messages that carry both a name and a chat_id.
    if msg.contact and msg.chat_id and "@" in str(msg.chat_id):
        try:
            with connect() as con:
                con.execute(
                    "INSERT INTO whatsapp_contact_aliases(alias, chat_id, display_name, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(alias) DO UPDATE SET "
                    "chat_id=excluded.chat_id, updated_at=excluded.updated_at",
                    (msg.contact.lower(), msg.chat_id, msg.contact, utc_now_iso()))
        except Exception:
            pass
    return {"ok": True, "id": rowid}


def resolve_contact(name_or_chat: str) -> dict[str, Any]:
    """Resolve a human name to a WAHA chat_id. Returns {ok, chat_id} on a unique
    match, or {ok: False, reason, candidates} when unknown or ambiguous, so the
    approval step can refuse rather than send to a display name."""
    s = (name_or_chat or "").strip()
    if "@" in s:  # already a chat id
        return {"ok": True, "chat_id": s}
    with connect() as con:
        row = con.execute(
            "SELECT chat_id, display_name FROM whatsapp_contact_aliases WHERE alias = ?",
            (s.lower(),)).fetchone()
        if row:
            return {"ok": True, "chat_id": row["chat_id"], "display_name": row["display_name"]}
        rows = con.execute(
            "SELECT chat_id, display_name FROM whatsapp_contact_aliases WHERE alias LIKE ?",
            (f"%{s.lower()}%",)).fetchall()
    if len(rows) == 1:
        return {"ok": True, "chat_id": rows[0]["chat_id"], "display_name": rows[0]["display_name"]}
    if len(rows) > 1:
        return {"ok": False, "reason": "ambiguous",
                "candidates": [{"chat_id": r["chat_id"], "name": r["display_name"]} for r in rows]}
    return {"ok": False, "reason": "unknown_contact",
            "hint": "no chat id known for this name; message them first so Mongoose learns it, "
                    "or draft with an explicit chat_id"}


def ingest_normalised(nm) -> dict[str, Any]:
    """Index a provider-normalised message (NormalisedWhatsAppMessage). This is the
    webhook ingestion path: it maps the normalised model to the local store, deduped
    by provider message id, and (for inbound) raises a notification so the message
    feeds 'what changed', the brief, and friend check-ins. Media is metadata-only by
    default (never auto-downloaded)."""
    msg = WhatsAppMessage(
        chat_id=nm.chat_id,
        contact=nm.sender_name or nm.chat_id,
        direction="in" if nm.direction == "inbound" else "out",
        wa_message_id=f"{nm.provider}:{nm.session}:{nm.provider_message_id}",
        body=nm.text,
        media_kind=nm.media_kind or "none",
        quoted_id=nm.quoted_message_id or None,
        received_at=nm.timestamp or None,
        privacy=nm.privacy or "high",
        provider_session=getattr(nm, "session", "") or "",
        session_role=_role_for_session(getattr(nm, "session", "")),
    )
    res = index_message(msg)
    # Inbound messages feed the rest of Mongoose via a notification event. We write
    # the notification row directly (the async ingest path isn't awaitable from this
    # sync webhook handler), reusing the same routing/dedupe.
    if nm.direction == "inbound" and not res.get("skipped"):
        try:
            from .notifications import NotificationIn, store_notification
            store_notification(NotificationIn(
                source="whatsapp", app="whatsapp",
                title=f"WhatsApp from {msg.contact}",
                body="" if str(nm.privacy).lower() in _PRIVATE else (nm.text or "")[:200],
                privacy=nm.privacy or "high", urgency="normal", device="whatsapp"))
        except Exception:
            pass
    return res


def process_webhook(payload: dict, provider_name: str | None = None) -> dict[str, Any]:
    """Normalise a provider webhook and ingest every message it carries. Returns a
    count. The route is responsible for HMAC/token verification BEFORE calling this."""
    from .whatsapp_providers.base import get_provider
    provider = get_provider(provider_name)
    if provider is None:
        return {"ok": False, "error": "no_provider"}
    ingested = 0
    for nm in provider.normalise_webhook(payload):
        r = ingest_normalised(nm)
        if r.get("ok") and not r.get("skipped"):
            ingested += 1
    return {"ok": True, "ingested": ingested}


def search_messages(q: str, limit: int = 10, *, shared_surface: bool = False,
                    include_all_accounts: bool = False) -> list[dict[str, Any]]:
    """Search indexed WhatsApp messages. By default (local authenticated use) results
    are unmasked: the body is already on your machine, so your own search should find
    it. Pass shared_surface=True for output going somewhere shared/spoken (HA, Alexa,
    a shared screen), where private/secret content is masked.

    Dual-account: by default this is the owner's inbox. Inbound replies that landed on Mongoose's OWN
    sender number are hidden so they don't pollute 'the owner's WhatsApp'. Outbound Mongoose notifications
    stay searchable. Pass include_all_accounts=True to see everything."""
    match = safe_fts_query(q)
    if match is None:
        return []
    hide_mongoose_inbound = not include_all_accounts
    with connect() as con:
        sql = ("SELECT m.id, m.contact, m.direction, substr(m.body,1,300) AS snippet, "
               "m.sent_at, m.received_at, m.privacy, m.session_role "
               "FROM whatsapp_fts f JOIN whatsapp_messages m ON m.id = f.rowid "
               "WHERE whatsapp_fts MATCH ? ")
        params = [match]
        if hide_mongoose_inbound:
            sql += "AND NOT (m.session_role = ? AND m.direction = 'in') "
            params.append(MONGOOSE_SEND)
        sql += "ORDER BY m.indexed_at DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(sql, tuple(params)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if shared_surface and str(d.get("privacy", "")).lower() in _PRIVATE:
                d["snippet"] = "[private message]"
            out.append(d)
        return out


def recent_from(contact: str, limit: int = 10) -> list[dict[str, Any]]:
    """Recent messages with one contact (most recent first), private bodies masked."""
    with connect() as con:
        rows = con.execute(
            "SELECT contact, direction, body, sent_at, received_at, privacy "
            "FROM whatsapp_messages WHERE contact = ? ORDER BY indexed_at DESC LIMIT ?",
            (contact, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if str(d.get("privacy", "")).lower() in _PRIVATE:
                d["body"] = "[private message]"
            out.append(d)
        return out


def unanswered_inbound(within_hours: int = 72, *, include_mongoose: bool = False) -> list[dict[str, Any]]:
    """Contacts whose latest owner-inbox message is INBOUND (i.e. you haven't replied).

    Uses a window query because SQLite does not guarantee that a non-aggregated `direction` selected
    beside MAX(indexed_at) belongs to the row with that max timestamp. Dual-account: replies that landed
    on Mongoose's OWN sender number are excluded from the owner's personal unanswered list unless
    include_mongoose=True."""
    conditions: list[str] = []
    params: tuple = ()
    if not include_mongoose:
        conditions.append("(session_role IS NULL OR session_role IN (?, ''))")
        params = params + (OWNER_READ,)
    if within_hours and within_hours > 0:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
        conditions.append("indexed_at >= ?")
        params = params + (cutoff,)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with connect() as con:
        rows = con.execute(
            f"""
            SELECT contact, direction, indexed_at AS latest FROM (
                SELECT contact, direction, indexed_at,
                       ROW_NUMBER() OVER (PARTITION BY contact ORDER BY indexed_at DESC, id DESC) AS rn
                FROM whatsapp_messages
                {where}
            ) WHERE rn = 1 ORDER BY latest DESC
            """, params).fetchall()
        return [{"contact": r["contact"], "latest": r["latest"]}
                for r in rows if r["direction"] == "in" and r["contact"]]


# --- outbound: draft -> approve -> send (the safety boundary) ----------------
def draft_message(contact: str, body: str, chat_id: str = "", *,
                  send_role: str = MONGOOSE_SEND, send_session: str | None = None) -> dict[str, Any]:
    """Create a PENDING outbound draft. This does NOT send. A human must approve it first.

    Dual-account: approved sends default to Mongoose's OWN WhatsApp number (mongoose_send), so
    Mongoose messages come from Mongoose, not from the owner's personal account. Pass send_role=OWNER_READ
    to send from the owner's own number instead."""
    session = send_session or account_for_role(send_role).session
    with connect() as con:
        if _has_send_role_cols(con):
            cur = con.execute(
                "INSERT INTO whatsapp_drafts(chat_id, contact, body, status, created_at, send_role, send_session) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (chat_id, contact, body, utc_now_iso(), send_role, session))
        else:
            cur = con.execute(
                "INSERT INTO whatsapp_drafts(chat_id, contact, body, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (chat_id, contact, body, utc_now_iso()))
        draft_id = cur.lastrowid
    audit("whatsapp", "draft", True, payload={"contact": contact, "send_role": send_role})
    return {"id": draft_id, "contact": contact, "body": body, "status": "pending", "send_role": send_role}


def _has_send_role_cols(con) -> bool:
    cols = [r[1] for r in con.execute("PRAGMA table_info(whatsapp_drafts)")]
    return "send_role" in cols


async def send_draft_approval(draft_id: int) -> dict[str, Any]:
    """Push an actionable approval notification to the phone with Send / Cancel buttons,
    so approving a draft is one tap, not a CLI command.

    Security: previously this sent the master MONGOOSE_API_TOKEN in the ntfy action headers,
    which would expose the reusable master token to any non-local ntfy server. This version
    uses short-lived, draft-scoped one-time approval tokens instead. Each token is:
      - Tied to ONE specific draft, can only approve/cancel that draft.
      - Single-use (consumed on first use).
      - Short-lived (default 24h, configurable).
      - Random (128-bit hex, not guessable).
    The master API token never leaves this machine."""
    import os
    from .notifications import send_to_phone
    with connect() as con:
        row = con.execute("SELECT contact, body FROM whatsapp_drafts WHERE id = ?",
                          (draft_id,)).fetchone()
    if not row:
        return {"sent": False, "reason": "draft_not_found"}
    try:
        from ..access_urls import user_facing_base
        base = (os.getenv("MONGOOSE_PUBLIC_URL") or user_facing_base(prefer_public=True, prefer_lan=True)).rstrip("/")
    except Exception:
        base = (os.getenv("MONGOOSE_PUBLIC_URL") or "https://127.0.0.1:8765").rstrip("/")
    # Mint two draft-scoped tokens (one to approve, one to cancel). Each can only act
    # on this one draft, and only once. The master token is never sent to ntfy.
    approve_token = _mint_draft_action_token(draft_id, action="approve")
    cancel_token = _mint_draft_action_token(draft_id, action="cancel")
    actions = [
        {"action": "http", "label": "Send", "method": "POST",
         "url": f"{base}/whatsapp/outbox/{draft_id}/approve",
         "headers": {"X-Draft-Action-Token": approve_token}, "clear": True},
        {"action": "http", "label": "Cancel", "method": "POST",
         "url": f"{base}/whatsapp/outbox/{draft_id}/cancel",
         "headers": {"X-Draft-Action-Token": cancel_token}, "clear": True},
    ]
    return await send_to_phone(
        f"Send to {row['contact']}?", row["body"], priority="default",
        tags=["speech_balloon"], actions=actions)


def list_drafts(status: str = "pending") -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT id, chat_id, contact, body, status, created_at, decided_at "
            "FROM whatsapp_drafts WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
        return [dict(r) for r in rows]


def edit_draft(draft_id: int, body: str) -> dict[str, Any]:
    with connect() as con:
        con.execute("UPDATE whatsapp_drafts SET body = ? WHERE id = ? AND status = 'pending'",
                    (body, draft_id))
    return {"id": draft_id, "body": body, "status": "pending"}


def cancel_draft(draft_id: int) -> dict[str, Any]:
    with connect() as con:
        con.execute("UPDATE whatsapp_drafts SET status = 'cancelled', decided_at = ? WHERE id = ?",
                    (utc_now_iso(), draft_id))
    return {"id": draft_id, "status": "cancelled"}


def approve_and_send(draft_id: int, send_fn=None) -> dict[str, Any]:
    """Send a draft AFTER explicit approval. This is the ONLY path that sends and it
    requires a draft_id the human chose, so the model can't send autonomously.

    Transport resolution: an explicit send_fn(contact, body) wins (used in tests);
    otherwise the configured WhatsApp provider's send_text is used (real sending via
    WAHA). If neither is available, the draft is marked approved but not transmitted
    (dry mode), so the safety boundary degrades safely rather than failing open."""
    with connect() as con:
        _cols = [r[1] for r in con.execute("PRAGMA table_info(whatsapp_drafts)")]
        _sel = "id, chat_id, contact, body, status" + (", send_role" if "send_role" in _cols else "")
        row = con.execute(
            f"SELECT {_sel} FROM whatsapp_drafts WHERE id = ?",
            (draft_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "draft_not_found"}
        if row["status"] not in ("pending", "approved"):
            return {"ok": False, "error": f"draft_status_{row['status']}"}
        contact, body, chat_id = row["contact"], row["body"], row["chat_id"]
        send_role = (row["send_role"] if "send_role" in row.keys() else None) or MONGOOSE_SEND

    # Resolve the transport. send_fn is the test/override seam; otherwise the
    # configured provider. The provider is only ever invoked here, post-approval.
    if send_fn is None:
        from .whatsapp_providers.base import get_provider
        try:
            provider = get_provider(role=send_role)
        except TypeError:
            # A test or older caller may have replaced get_provider with a signature that predates the
            # role kwarg. Fall back so dual-account routing never breaks existing seams.
            provider = get_provider()
        if provider is not None:
            # Resolve a real chat id before sending. WAHA needs e.g. 1234@c.us, not a
            # display name, so refuse rather than send to "Jordan".
            target = chat_id if (chat_id and "@" in str(chat_id)) else None
            if target is None:
                resolved = resolve_contact(contact)
                if not resolved.get("ok"):
                    return {"ok": False, "error": "contact_unresolved",
                            "reason": resolved.get("reason"),
                            "candidates": resolved.get("candidates"),
                            "hint": resolved.get("hint")}
                target = resolved["chat_id"]

            def send_fn(_contact, _body):
                return provider.send_text(target, _body)

    sent = False
    detail = "approved (no transport configured; not transmitted)"
    if send_fn is not None:
        try:
            send_fn(contact, body)
            sent = True
            detail = "sent"
        except Exception as exc:
            audit("whatsapp", "send", False, payload={"contact": contact, "error": str(exc)})
            with connect() as con:
                con.execute("UPDATE whatsapp_drafts SET error = ? WHERE id = ?",
                            (str(exc)[:300], draft_id)) if _has_error_col(con) else None
            # FIX (external audit, additional findings #8): get_provider() ALWAYS constructs a
            # WahaProvider for the default name (never None), even with no WAHA_URL set, so it
            # genuinely attempts a network call and raises httpx.ConnectError -- the exact "OS
            # error" the audit's walkthrough hit. That specific, identifiable exception class
            # gets the same clean setup instruction connect()/login_qr() already give. Scoped
            # narrowly to THIS exception type only (not a blanket rewrite of every transport
            # failure) so a genuinely-configured provider's own real errors -- including a
            # deliberately raised one in a test -- still report honestly as transport_failed.
            try:
                import httpx
                if isinstance(exc, httpx.ConnectError):
                    from .whatsapp_journey import _waha_configured
                    if not _waha_configured():
                        return {"ok": False, "error": "WhatsApp isn't started yet. Use the "
                                "Start WhatsApp button, no terminal needed.",
                                "can_start_container": True}
            except ImportError:
                pass
            return {"ok": False, "error": f"transport_failed: {exc}"}

    with connect() as con:
        con.execute("UPDATE whatsapp_drafts SET status = ?, decided_at = ? WHERE id = ?",
                    ("sent" if sent else "approved", utc_now_iso(), draft_id))
    # Record the sent message through index_message so it goes into whatsapp_fts and
    # is searchable like any other message (not a raw insert that bypasses FTS).
    if sent:
        index_message(WhatsAppMessage(
            chat_id=chat_id or "", contact=contact, direction="out",
            wa_message_id=f"draft-{draft_id}-{utc_now_iso()}", body=body,
            media_kind="none", sent_at=utc_now_iso(), privacy="medium"))
    audit("whatsapp", "send", True, payload={"contact": contact, "transmitted": sent})
    out = {"ok": True, "draft_id": draft_id, "sent": sent, "detail": detail}
    # Clarity only (do NOT flip ok: existing callers key on sent/detail, and flipping ok would break them).
    # When there is no transport the draft is approved-but-not-transmitted; name that unambiguously so a
    # caller/log can tell "approved, nothing sent" apart from a real send.
    if not sent:
        out["reason"] = "no WhatsApp transport configured"
    return out


def _has_error_col(con) -> bool:
    cols = [r[1] for r in con.execute("PRAGMA table_info(whatsapp_drafts)")]
    return "error" in cols


def send_mongoose_notification_to_owner(text: str, *, to: str = "") -> dict[str, Any]:
    """Send an owner/system notification FROM Mongoose's own WhatsApp number TO the owner's number.

    Deliberately narrow: the Mongoose sender role only, the owner number only, never an arbitrary
    contact, and never person-to-person auto-send. It still flows through the approved-outbox transport
    (so it is audited), but auto-approves because Mongoose is notifying its own owner from its own
    number, not sending a message on the owner's behalf to a third party. This is a notification channel,
    not a general messaging capability."""
    number = (to or owner_number()).strip()
    if not number:
        return {"ok": False, "error": "no_owner_number",
                "detail": "Set WHATSAPP_OWNER_NUMBER to use WhatsApp as an owner notification channel."}
    chat_id = number if "@" in number else f"{number}@c.us"
    d = draft_message(contact=number, body=text, chat_id=chat_id, send_role=MONGOOSE_SEND)
    draft_id = d.get("id")
    if not draft_id:
        return {"ok": False, "error": "draft_failed"}
    return approve_and_send(draft_id)
