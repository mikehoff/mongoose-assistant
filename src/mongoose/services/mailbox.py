"""Direct messages between Alex (the owner) and trusted guests, carried by Mongoose.

The simplest thing that actually works: a single messages table with sender/recipient
participant ids, plus send/list/unread/mark-read. Mongoose itself is the courier: a guest
says "tell Alex the boiler is leaking", Mongoose stores a message addressed to the owner and
fires a notification; Alex sees it in his next chat or as a push, replies, and the guest sees
it on their side. No new transport, no accounts: it rides on the device pairing and the
existing notification push.

Participant ids:
  - "owner"            : Alex (every owner surface: cockpit, phone, CLI: is the same person)
  - "guest:<device_id>": a specific trusted-guest device

Isolation: a guest can only ever message the owner and read their own thread. A guest can NOT
address another guest. The owner can message any specific guest or broadcast to all guests.
"""
from __future__ import annotations

from typing import Any

from ..db import audit, connect, utc_now_iso

OWNER = "owner"


def _is_guest(pid: str) -> bool:
    return pid.startswith("guest:")


def ensure_tables() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mailbox_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_mailbox_recipient ON mailbox_messages(recipient, read_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_mailbox_sender ON mailbox_messages(sender)")


def _valid_pair(sender: str, recipient: str) -> tuple[bool, str]:
    """Enforce who may message whom. Returns (ok, reason)."""
    if sender == recipient:
        return False, "cannot message yourself"
    if _is_guest(sender):
        # A guest may only message the owner.
        if recipient != OWNER:
            return False, "guests can only message the owner"
        return True, ""
    if sender == OWNER:
        # The owner may message a specific guest, or broadcast to all guests.
        if recipient == "guest:*" or _is_guest(recipient):
            return True, ""
        return False, "owner can only message a guest or guest:* (broadcast)"
    return False, f"unknown sender '{sender}'"


def send_message(*, sender: str, recipient: str, body: str, notify: bool = True) -> dict[str, Any]:
    """Store a message and (optionally) fire a notification to the recipient.

    For a broadcast (recipient == 'guest:*') one row is stored per currently-paired guest device.
    """
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "empty message"}
    if len(body) > 4000:
        body = body[:4000]
    ok, reason = _valid_pair(sender, recipient)
    if not ok:
        return {"ok": False, "error": reason}
    ensure_tables()

    recipients: list[str]
    if recipient == "guest:*":
        recipients = _all_guest_ids()
        if not recipients:
            return {"ok": False, "error": "no paired guests to broadcast to"}
    else:
        recipients = [recipient]

    now = utc_now_iso()
    ids: list[int] = []
    with connect() as con:
        for rcpt in recipients:
            cur = con.execute(
                "INSERT INTO mailbox_messages(sender, recipient, body, created_at) VALUES (?, ?, ?, ?)",
                (sender, rcpt, body, now),
            )
            ids.append(int(cur.lastrowid))
    audit("mailbox", "send", True, payload={"sender": sender, "recipient": recipient, "count": len(ids)})

    if notify:
        for rcpt in recipients:
            _notify_recipient(sender=sender, recipient=rcpt, body=body)

    return {"ok": True, "ids": ids, "delivered_to": recipients, "created_at": now}


def list_thread(*, participant: str, other: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Messages visible to `participant`. For a guest, that is only their thread with the owner.
    For the owner, optionally filter to the thread with one guest via `other`."""
    ensure_tables()
    with connect() as con:
        if _is_guest(participant):
            rows = con.execute(
                "SELECT id, sender, recipient, body, created_at, read_at FROM mailbox_messages "
                "WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?) "
                "ORDER BY id DESC LIMIT ?",
                (participant, OWNER, OWNER, participant, limit),
            ).fetchall()
        elif participant == OWNER and other:
            rows = con.execute(
                "SELECT id, sender, recipient, body, created_at, read_at FROM mailbox_messages "
                "WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?) "
                "ORDER BY id DESC LIMIT ?",
                (OWNER, other, other, OWNER, limit),
            ).fetchall()
        else:  # owner, all threads
            rows = con.execute(
                "SELECT id, sender, recipient, body, created_at, read_at FROM mailbox_messages "
                "WHERE sender=? OR recipient=? ORDER BY id DESC LIMIT ?",
                (OWNER, OWNER, limit),
            ).fetchall()
    return [dict(r) for r in rows][::-1]  # chronological for display


def unread_for(participant: str, limit: int = 50) -> list[dict[str, Any]]:
    ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT id, sender, recipient, body, created_at FROM mailbox_messages "
            "WHERE recipient=? AND read_at IS NULL ORDER BY id ASC LIMIT ?",
            (participant, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def unread_count(participant: str) -> int:
    ensure_tables()
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM mailbox_messages WHERE recipient=? AND read_at IS NULL",
            (participant,),
        ).fetchone()
    return int(row["n"]) if row else 0


def mark_read(*, participant: str, up_to_id: int | None = None) -> dict[str, Any]:
    """Mark this participant's inbound messages as read (all, or up to a given id)."""
    ensure_tables()
    now = utc_now_iso()
    with connect() as con:
        if up_to_id is not None:
            cur = con.execute(
                "UPDATE mailbox_messages SET read_at=? WHERE recipient=? AND read_at IS NULL AND id<=?",
                (now, participant, int(up_to_id)),
            )
        else:
            cur = con.execute(
                "UPDATE mailbox_messages SET read_at=? WHERE recipient=? AND read_at IS NULL",
                (now, participant),
            )
        marked = cur.rowcount
    return {"ok": True, "marked": marked}


def _all_guest_ids() -> list[str]:
    """Currently-paired trusted-guest devices, as participant ids."""
    try:
        from ..devices import list_devices
        out = []
        for d in list_devices():
            if d.get("profile") == "trusted_guest" and not d.get("revoked"):
                out.append(f"guest:{d['device_id']}")
        return out
    except Exception:
        return []


def _owner_name() -> str:
    """The owner's chosen name. Defaults to the site profile's owner.name but can be set at first run."""
    try:
        from ..db import get_app_state
        v = (get_app_state("owner_display_name") or "").strip()
        if v:
            return v[:40]
    except Exception:
        pass
    from .. import profile as _profile
    return _profile.owner_name()


def _display_name(pid: str) -> str:
    if pid == OWNER:
        return _owner_name()
    if _is_guest(pid):
        did = pid.split(":", 1)[1]
        try:
            from ..devices import device_whoami
            who = device_whoami(did)
            if who:
                # Prefer the name the person asked to be called by; fall back to the device label.
                return (who.get("display_name") or who.get("name") or "a guest")
        except Exception:
            pass
        return "a guest"
    return pid


def name_for(pid: str) -> str:
    """Public accessor for a participant's display name."""
    return _display_name(pid)


def recipient_is_active(recipient: str, within_seconds: int = 300) -> bool:
    """True if a message to this recipient would reach an app that's currently open/recent,
    so the sender can be told honestly whether it's 'sent' now or 'saved' for later. The owner
    has many surfaces, so we treat the owner as reachable; a guest depends on their device."""
    if recipient == OWNER:
        return True
    if _is_guest(recipient):
        try:
            from ..devices import device_recently_active
            return device_recently_active(recipient.split(":", 1)[1], within_seconds)
        except Exception:
            return False
    return False


def _notify_recipient(*, sender: str, recipient: str, body: str) -> None:
    """Fire a notification so the recipient learns of the message without polling.
    Best-effort: never raises into the send path."""
    try:
        from .notifications import store_notification, NotificationIn
        sender_name = _display_name(sender)
        store_notification(NotificationIn(
            source="mongoose-mailbox",
            app="messages",
            title=f"Message from {sender_name}",
            body=body[:280],
            privacy="private",
            urgency="normal",
            device=recipient,
        ))
    except Exception:
        pass  # delivery is best-effort; the message is already stored and will show on next list


def resolve_recipient_phrase(phrase: str) -> str | None:
    """Map a natural phrase to a participant id for the owner addressing a guest.
    'jo' / 'jo phone' -> guest:<device_id> if a guest device name matches; 'everyone'/'all' -> guest:*."""
    p = (phrase or "").strip().lower()
    if not p:
        return None
    if p in ("everyone", "all", "all guests", "the guests", "guests"):
        return "guest:*"
    from .. import profile as _profile
    if p in (_profile.owner_name().lower(), "owner", "myself"):
        return OWNER
    try:
        from ..devices import list_devices
        for d in list_devices():
            if d.get("profile") != "trusted_guest" or d.get("revoked"):
                continue
            name = (d.get("name") or "").lower()
            if name and (p == name or p in name or name in p):
                return f"guest:{d['device_id']}"
    except Exception:
        pass
    return None
