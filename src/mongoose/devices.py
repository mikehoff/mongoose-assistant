from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

# Device registry + pairing groundwork (Mac-side of "Android as a surface").
#
# The Mac mini stays the brain; phones/tablets become paired surfaces. This module is
# the trust foundation everything else (approvals-on-phone, notification ingest, voice
# relay) will hang off. It is deliberately the GROUNDWORK only: a device registry, a
# short-lived pairing-code flow, and revoke. The Android Companion APK is a separate
# codebase (a later version); here we make the Mac able to pair, list, and revoke
# devices safely.
#
# SECURITY MODEL (this is the sensitive part, pairing hands out a credential):
#   - A pairing code is minted ONLY by an already-authenticated user (you, on the Mac
#     cockpit, click "Add device"). An attacker on the LAN cannot mint codes.
#   - Codes are short-lived (default 5 min), single-use, and high-entropy.
#   - The device claims the code via /pair/claim (the only pre-auth pairing endpoint,
#     safe because it requires a valid unexpired code) and receives a device token.
#   - The code is consumed on first successful claim.
#   - Device tokens can be revoked; the request guard rejects revoked/unknown device
#     tokens. Revocation is immediate.
#
# We store only a HASH of the device token (like a password), never the token itself.

_CODE_TTL_SECONDS = 300  # pairing codes expire in 5 minutes
_DEVICE_TOKEN_BYTES = 32


def _ensure_tables(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS paired_devices (
            device_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            device_type TEXT NOT NULL,
            profile TEXT,
            token_hash TEXT NOT NULL,
            capabilities TEXT,
            paired_at TEXT NOT NULL,
            last_seen TEXT,
            transport TEXT,
            revoked INTEGER NOT NULL DEFAULT 0
        )""")
    con.execute(
        """CREATE TABLE IF NOT EXISTS pairing_codes (
            code_hash TEXT PRIMARY KEY,
            profile TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0
        )""")
    con.execute(
        """CREATE TABLE IF NOT EXISTS device_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            device_name TEXT,
            event TEXT NOT NULL,
            at TEXT NOT NULL,
            detail TEXT
        )""")
    con.execute(
        """CREATE TABLE IF NOT EXISTS device_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            privacy TEXT NOT NULL DEFAULT 'medium',
            payload_json TEXT,
            created_at TEXT NOT NULL
        )""")
    # Migrate older DBs that predate the chosen-name field. display_name is the name the
    # person asked Mongoose to call them by (e.g. "Sam"), distinct from the device label
    # ("Sam's iPhone"). It is captured at first run and used wherever a human is named.
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(paired_devices)")}
        if "display_name" not in cols:
            con.execute("ALTER TABLE paired_devices ADD COLUMN display_name TEXT")
    except Exception:
        pass


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write_receipt(con, *, device_id: str, device_name: str, event: str, detail: str = "") -> None:
    """Leave a legible trace of pairing-lifecycle events (paired, revoked). Receipts are
    one of Mongoose's core trust features."""
    con.execute(
        "INSERT INTO device_receipts(device_id, device_name, event, at, detail) VALUES(?,?,?,?,?)",
        (device_id, device_name, event, _now_iso(), detail))


def device_receipts(limit: int = 50) -> list[dict[str, Any]]:
    """Recent pairing/revocation receipts (newest first)."""
    from .db import connect
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT device_id, device_name, event, at, detail FROM device_receipts "
            "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    return [dict(r) for r in rows]


def server_fingerprint() -> str:
    """A stable, non-secret fingerprint a device can confirm on pairing (derived from
    the API token so it's unique per install, but hashed so it leaks nothing)."""
    from .settings import get_settings
    tok = getattr(get_settings(), "api_token", "") or ""
    return hashlib.sha256(("mongoose-fingerprint:" + tok).encode()).hexdigest()[:16]


# ---- pairing code lifecycle (minted by an authenticated user only) ----

def create_pairing_code(profile: str = "primary_phone") -> dict[str, Any]:
    """Mint a short-lived, single-use pairing code. CALLERS MUST be authenticated, the
    route that exposes this is nonce/session-gated; this function assumes that."""
    from .db import connect
    code = secrets.token_urlsafe(9)  # ~12 chars, high entropy, human-scannable
    now = time.time()
    with connect() as con:
        _ensure_tables(con)
        # opportunistically clear expired codes
        con.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
        con.execute(
            "INSERT INTO pairing_codes(code_hash, profile, created_at, expires_at, consumed) "
            "VALUES(?,?,?,?,0)",
            (_hash(code), profile, now, now + _CODE_TTL_SECONDS))
        con.commit()
    return {
        "code": code,
        "expires_in": _CODE_TTL_SECONDS,
        "fingerprint": server_fingerprint(),
        "profile": profile,
    }


def pairing_qr_svg(pair_url: str) -> str | None:
    """Render a pairing URL as an inline SVG QR code, or None if the qrcode library
    isn't installed (the UI then falls back to showing the URL/code as text). Kept a
    SOFT dependency so a minimal install still pairs by typing the address."""
    try:
        import qrcode
        import qrcode.image.svg as svg
        img = qrcode.make(pair_url, image_factory=svg.SvgPathImage, box_size=10, border=2)
        import io
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return None


def _normalize_device_name(name: str, device_type: str = "") -> str:
    """Make a device name safe to use downstream, notably as the bus 'sender', which MUST
    be single-line (the bus correctly rejects multiline frontmatter). Strip CR/LF and other
    control characters, collapse whitespace, cap length, and fall back to a sensible default
    if nothing usable remains. Without this, a device paired as "bad\\nname" pairs fine but
    later breaks share-to-Mongoose. (QA-audit P1.)"""
    if not name:
        name = ""
    # remove control chars (incl. CR/LF/tab) entirely
    cleaned = "".join(ch for ch in name if ch == " " or (ord(ch) >= 32 and ord(ch) != 127))
    # collapse any run of whitespace to a single space, trim ends
    cleaned = " ".join(cleaned.split())
    # cap length (defensive; keeps receipts/UI tidy and the bus header bounded)
    cleaned = cleaned[:64].strip()
    if not cleaned:
        cleaned = (device_type or "device").strip() or "device"
    return cleaned


_PASSPHRASE_WORDS = (
    "otter", "cedar", "lamp", "river", "maple", "fox", "harbor", "willow", "ember", "pebble",
    "cocoa", "meadow", "raven", "lotus", "copper", "basil", "tiger", "olive", "comet", "dune",
    "marble", "hazel", "brook", "sparrow", "cactus", "violet", "thistle", "amber", "koala", "pine",
)


def _generate_passphrase() -> str:
    """A memorable but strong password: three short words plus a digit, e.g. 'otter-cedar-lamp-7'.
    Easy to read aloud, type, and remember; still hard to guess (30-word list, three picks + digit)."""
    words = [secrets.choice(_PASSPHRASE_WORDS) for _ in range(3)]
    return "-".join(words) + "-" + str(secrets.randbelow(10))


def _suggest_username(display_name: str) -> str:
    """A friendly username slug from the person's name (lowercase letters/digits only). Pads to a
    valid length and falls back to 'user'. The pairing screen lets the person edit it before the
    account is created, and register suffixes a number if it's already taken."""
    base = "".join(ch for ch in (display_name or "").lower() if ch.isalnum())
    if len(base) < 3:
        base = (base + "user")[:8] if base else "user"
    return base[:20]


def _normalize_person_name(name: str) -> str:
    """The human's chosen name, captured at pairing. Single line, colon-free, short (rendered in
    chat attribution and used to seed the account display name)."""
    if not name:
        return ""
    cleaned = "".join(ch for ch in str(name) if ch == " " or (ord(ch) >= 32 and ord(ch) != 127))
    return " ".join(cleaned.replace(":", " ").split())[:64].strip()


def _person_from_device_name(device_name: str) -> str:
    """Best-effort person name when only a device label was given ('Alex's phone' -> 'Alex')."""
    cleaned = _normalize_person_name(device_name)
    for suffix in ("'s phone", "’s phone", "'s device", "’s device", " phone", " device"):
        if cleaned.lower().endswith(suffix.lower()) and len(cleaned) > len(suffix):
            return cleaned[:-len(suffix)].strip()
    return cleaned


def claim_pairing_code(code: str, device_name: str, device_type: str, display_name: str = "") -> dict[str, Any]:
    """A device redeems a pairing code and is registered, returning a device token.
    This is the ONLY pre-auth pairing step; it is safe because a valid unexpired code
    can only have been minted by an authenticated user. Never raises.

    display_name is the human's name captured at pairing. It is stored on the device, and we return
    a SUGGESTED username + memorable passphrase so the pairing screen can hand the person a real,
    portable login to save (and edit) before they ever use Mongoose. The account is created by the
    pairing screen on confirm, so the person can change either first."""
    from .db import connect
    now = time.time()
    if not code:
        return {"ok": False, "error": "pairing code required"}
    display_name = _normalize_person_name(display_name)
    if not device_name:
        device_name = (display_name + "'s device") if display_name else "device"
    # Normalize BEFORE storing so the name is safe everywhere it's later used (bus sender,
    # receipts, UI). A multiline/control-char name would otherwise break share-to-Mongoose.
    device_name = _normalize_device_name(device_name, device_type)
    if not display_name:
        display_name = _person_from_device_name(device_name)
    ch = _hash(code)
    with connect() as con:
        _ensure_tables(con)
        row = con.execute(
            "SELECT profile, expires_at, consumed FROM pairing_codes WHERE code_hash=?",
            (ch,)).fetchone()
        if row is None:
            return {"ok": False, "error": "invalid pairing code"}
        if row["consumed"]:
            return {"ok": False, "error": "pairing code already used"}
        if row["expires_at"] < now:
            return {"ok": False, "error": "pairing code expired"}
        profile = row["profile"] or "primary_phone"
        # consume the code (single use)
        con.execute("UPDATE pairing_codes SET consumed=1 WHERE code_hash=?", (ch,))
        # register the device
        device_id = secrets.token_hex(8)
        device_token = secrets.token_urlsafe(_DEVICE_TOKEN_BYTES)
        paired_at = _now_iso()
        caps = ",".join(_default_caps(profile))
        con.execute(
            "INSERT INTO paired_devices(device_id, name, device_type, profile, token_hash, "
            "capabilities, paired_at, last_seen, transport, display_name, revoked) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0)",
            (device_id, device_name[:80], (device_type or "android")[:32], profile,
             _hash(device_token), caps, paired_at, paired_at, "lan", display_name[:80]))
        _write_receipt(con, device_id=device_id, device_name=device_name[:80],
                       event="paired", detail=f"profile={profile}")
        con.commit()
    return {
        "ok": True,
        "device_id": device_id,
        "device_token": device_token,  # returned ONCE; only the hash is stored
        "profile": profile,
        "capabilities": _default_caps(profile),
        "display_name": display_name,
        "suggested_username": _suggest_username(display_name),
        "suggested_password": _generate_passphrase(),
        "server_url_hint": "use the host you scanned",
    }


def pairing_code_status(code: str) -> dict[str, Any]:
    """Has this pairing code been claimed yet? Lets the surface that shows the QR stop
    showing it the instant a device consumes it (single-use codes must not keep offering a
    scannable QR after they've been used). Reveals only the code's lifecycle state, never a
    secret. A code that's unknown or already cleared (expiry sweep) reports gone."""
    from .db import connect
    ch = _hash((code or "").strip())
    now = time.time()
    with connect() as con:
        _ensure_tables(con)
        row = con.execute(
            "SELECT consumed, expires_at FROM pairing_codes WHERE code_hash=?", (ch,)
        ).fetchone()
    if not row:
        return {"found": False, "consumed": False, "expired": True}
    return {"found": True, "consumed": bool(row["consumed"]), "expired": (row["expires_at"] < now)}


# What Android is FOR in Mongoose, encoded as capabilities, not a flat list. Each role
# has capabilities that are either expected (the device should do this), optional, or
# off-by-design (deliberately not for this role, e.g. a shared tablet does not capture
# personal notifications). The registry's job is to answer "what can THIS device do for
# Mongoose, what isn't enabled yet, and what's the next useful step?", that is what makes
# the device layer serve the Needs Me / Approvals / Voice / What Changed loop rather than
# being neutral plumbing.

# Human-facing description of each capability and why Mongoose wants it.
_CAPABILITY_INFO: dict[str, dict[str, str]] = {
    "approvals":        {"label": "Approvals", "why": "approve WhatsApp sends, installs, syncs from this device"},
    "voice":            {"label": "Voice", "why": "use this device's mic to talk to Mongoose"},
    "notifications":    {"label": "Notification capture", "why": "tell Mongoose what changed from app notifications"},
    "share_sheet":      {"label": "Share to Mongoose", "why": "send links/files/text from any app to Mongoose"},
    "selected_folders": {"label": "Selected folders", "why": "index chosen folders (not your whole phone)"},
    "cockpit":          {"label": "Cockpit", "why": "view status, brief, search on a larger screen"},
    "logs":             {"label": "Logs/status", "why": "see what Mongoose did and any failures"},
    "search":           {"label": "Search", "why": "search your synced corpus from this device"},
    "push":             {"label": "Push notifications", "why": "Mongoose can alert this device when something needs you"},
    "ambient_presence": {"label": "Ambient presence", "why": "tell Mongoose this device is alive and contributing local state"},
    "offline_queue":    {"label": "Offline queue", "why": "queue device events until the Mac is reachable"},
    "future_companion": {"label": "Native companion", "why": "future Android companion app support"},
}

# Per-role capability plan. "expected" = should be on for this role; "optional" = can be
# turned on; "off_by_design" = intentionally not for this role (shown so the user knows
# it's a choice, not a gap).
_ROLE_PLAN: dict[str, dict[str, str]] = {
    "primary_phone": {
        "approvals": "expected", "voice": "expected", "push": "expected",
        "notifications": "optional", "share_sheet": "optional", "selected_folders": "optional",
        "cockpit": "optional", "search": "optional", "ambient_presence": "expected",
        "offline_queue": "optional", "future_companion": "not_supported",
    },
    "home_tablet": {
        "cockpit": "expected", "voice": "expected", "approvals": "expected",
        "search": "optional", "logs": "optional", "push": "optional",
        "ambient_presence": "expected", "offline_queue": "optional",
        "notifications": "off_by_design",     # a shared home tablet should not capture personal notifications
        "selected_folders": "off_by_design", "future_companion": "not_supported",
    },
    # trusted_guest: a friend or family member invited from the cockpit.
    # Gets their own isolated conversation (no shared history) and their own memory
    # namespace (facts they share are scoped to their conversation only). Owner tools
    # (email, calendar, home control, files, the real scheduler) are all off. They DO
    # get a set of LIGHT, scope-isolated capabilities (guest_tasks.py): a low-stakes
    # coach / counsellor / research assistant / day helper / ADHD nudge, plus their own
    # reminders/notes , none of which can touch the owner's data, tools, or scheduler.
    # Accessible over DDNS so they can use it from home.
    "trusted_guest": {
        "voice": "optional",       # push-to-talk works if they choose it
        "cockpit": "off_by_design",  # never shows the management cockpit
        "approvals": "off_by_design",
        "push": "off_by_design",
        "notifications": "off_by_design",
        "selected_folders": "off_by_design",
        "ambient_presence": "off_by_design",
        "tools": "off_by_design",    # all OWNER tools explicitly disabled
        "light_tasks": "expected",   # guest-safe, scope-isolated coach/research/day-helper/reminders
        "share_sheet": "off_by_design",
    },
    "unknown": {
        "cockpit": "optional", "approvals": "optional",
    },
}


def _expected_caps(profile: str) -> list[str]:
    plan = _ROLE_PLAN.get(profile, _ROLE_PLAN["unknown"])
    return [c for c, state in plan.items() if state == "expected"]


def _default_caps(profile: str) -> list[str]:
    """Capabilities ENABLED at pairing time: the role's expected set (the device can turn
    on optional ones later)."""
    return _expected_caps(profile)


def capability_status(profile: str, enabled: list[str]) -> list[dict[str, Any]]:
    """For a device, compute each capability's state: enabled / available (planned but
    not yet on) / off_by_design. This is what lets Mongoose say 'notifications are not
    enabled yet' vs 'this tablet has no personal notifications by design'."""
    plan = _ROLE_PLAN.get(profile, _ROLE_PLAN["unknown"])
    enabled_set = set(enabled or [])
    out = []
    # union of planned caps and anything actually enabled
    for cap in list(plan.keys()) + [c for c in enabled_set if c not in plan]:
        if any(o["capability"] == cap for o in out):
            continue
        plan_state = plan.get(cap, "optional")
        if cap in enabled_set:
            state = "enabled"
        elif plan_state == "off_by_design":
            state = "off_by_design"
        elif plan_state == "not_supported":
            state = "not_supported_yet"
        elif plan_state == "expected":
            state = "missing_permission"
        else:
            state = "available"   # optional, but not yet enabled
        info = _CAPABILITY_INFO.get(cap, {"label": cap, "why": ""})
        out.append({"capability": cap, "label": info["label"], "why": info["why"],
                    "state": state, "planned": plan_state})
    return out


def _next_step(profile: str, enabled: list[str], last_seen: str | None, revoked: bool) -> str:
    """A clear next useful setup step for this device (the reviewer's 'next useful step
    per device'). Empty string when nothing is needed."""
    if revoked:
        return "This device is revoked. Pair it again to restore access."
    # expected capabilities not yet enabled are the priority
    missing_expected = [c for c in _expected_caps(profile) if c not in set(enabled or [])]
    if missing_expected:
        info = _CAPABILITY_INFO.get(missing_expected[0], {"label": missing_expected[0]})
        return f"Enable {info['label']} on this device (expected for its role)."
    # then offer the most valuable optional one
    plan = _ROLE_PLAN.get(profile, _ROLE_PLAN["unknown"])
    optional_off = [c for c, s in plan.items() if s == "optional" and c not in set(enabled or [])]
    if profile == "primary_phone" and "notifications" in optional_off:
        return "Optional: enable Notification capture so Mongoose can tell you what changed."
    if optional_off:
        info = _CAPABILITY_INFO.get(optional_off[0], {"label": optional_off[0]})
        return f"Optional: enable {info['label']}."
    return ""


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---- device registry queries / management ----

def list_devices() -> list[dict[str, Any]]:
    """All paired devices with what each can do for Mongoose, freshness, and the next
    useful step. No secrets. This is the heart of 'what can THIS device do?' rather than
    just 'what's paired?'."""
    from .db import connect
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT device_id, name, display_name, device_type, profile, capabilities, paired_at, "
            "last_seen, transport, revoked FROM paired_devices ORDER BY paired_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        enabled = (d.get("capabilities") or "").split(",") if d.get("capabilities") else []
        d["capabilities"] = enabled
        d["revoked"] = bool(d["revoked"])
        profile = d.get("profile") or "unknown"
        # per-capability state: enabled / available / off_by_design
        d["capability_status"] = capability_status(profile, enabled)
        # freshness: healthy / stale / never, plus a human age
        d["freshness"] = _freshness(d.get("last_seen"))
        # the clear next useful step for this device
        d["next_step"] = _next_step(profile, enabled, d.get("last_seen"), d["revoked"])
        out.append(d)
    return out


def _freshness(last_seen: str | None) -> dict[str, Any]:
    """Has this device checked in recently? Lets Mongoose say 'not seen for 3 days'."""
    if not last_seen:
        return {"state": "never", "detail": "never checked in"}
    try:
        from datetime import datetime, timezone
        seen = datetime.fromisoformat(last_seen)
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - seen).total_seconds()
    except Exception:
        return {"state": "unknown", "detail": ""}
    if age < 3600:
        return {"state": "healthy", "detail": "seen recently"}
    if age < 86400:
        return {"state": "healthy", "detail": f"seen {int(age // 3600)}h ago"}
    days = int(age // 86400)
    return {"state": "stale", "detail": f"not seen for {days} day{'s' if days != 1 else ''}"}


def revoke_device(device_id: str) -> dict[str, Any]:
    """Revoke a device immediately. Its token stops working on the next request. Also
    quarantines the device's ATTRIBUTED messages out of the shared main conversation:
    revoking access must also stop the revoked device's words sitting invisibly inside the
    owner's brain thread (a mis-paired device once left a week of a stranger's private
    chat in the owner's single-brain history, discovered only on the next pairing).
    Unattributed historical rows cannot be selected; conversation rollover is that remedy."""
    from .db import connect
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT name FROM paired_devices WHERE device_id=?", (device_id,)).fetchone()
        cur = con.execute("UPDATE paired_devices SET revoked=1 WHERE device_id=?", (device_id,))
        if cur.rowcount == 0:
            con.commit()
            return {"ok": False, "error": "unknown device"}
        _write_receipt(con, device_id=device_id, device_name=(row["name"] if row else ""),
                       event="revoked")
        con.commit()
    try:
        from .services.memory import quarantine_device_messages
        q = quarantine_device_messages(device_id)
    except Exception as exc:
        q = {"ok": False, "error": str(exc)[:120]}
    return {"ok": True, "device_id": device_id, "revoked": True,
            "messages_quarantined": q.get("moved", 0),
            "quarantine": q.get("quarantine_conversation")}


def merge_devices(source_id: str, target_id: str, *,
                  confirm_source_name: str = "", confirm_target_name: str = "") -> dict[str, Any]:
    """Fold a duplicate device (SOURCE) into a surviving one (TARGET) when the owner
    confirms they are the same person. Consolidates that person's attributed messages, per
    device events and receipts, push subscriptions, and capabilities onto TARGET, then
    revokes SOURCE.

    Unlike revoke, a merge does NOT quarantine the source's messages: they belong to the
    same person, so they are re-attributed to the surviving device and stay in the thread.

    Accident-hardened on purpose, because a stray click must never merge the wrong people:
      - both devices must exist and be different;
      - both must share the same profile (never fold a guest into the owner, or the reverse);
      - TARGET must be live (not revoked);
      - the caller must echo BOTH device names exactly (typed confirmation).
    Nothing is deleted. SOURCE is revoked, not removed, and every move leaves a receipt, so a
    mistaken merge is auditable and the raw rows still exist.
    """
    from .db import connect
    if not source_id or not target_id:
        return {"ok": False, "error": "source and target device ids are required"}
    if source_id == target_id:
        return {"ok": False, "error": "cannot merge a device into itself"}

    def _norm(value: str) -> str:
        return " ".join((value or "").split()).casefold()

    with connect() as con:
        _ensure_tables(con)
        src = con.execute(
            "SELECT device_id, name, profile, capabilities, display_name, revoked "
            "FROM paired_devices WHERE device_id=?", (source_id,)).fetchone()
        tgt = con.execute(
            "SELECT device_id, name, profile, capabilities, display_name, revoked "
            "FROM paired_devices WHERE device_id=?", (target_id,)).fetchone()
        if src is None:
            return {"ok": False, "error": "unknown source device"}
        if tgt is None:
            return {"ok": False, "error": "unknown target device"}
        if (src["profile"] or "") != (tgt["profile"] or ""):
            return {"ok": False, "error": "cannot merge across profiles "
                    f"(source is {src['profile'] or 'unknown'}, target is "
                    f"{tgt['profile'] or 'unknown'})"}
        if tgt["revoked"]:
            return {"ok": False, "error": "target device is revoked; merge into a live device"}
        if _norm(confirm_source_name) != _norm(src["name"]) or \
                _norm(confirm_target_name) != _norm(tgt["name"]):
            return {"ok": False, "error": "name confirmation did not match; merge refused",
                    "expected_source": src["name"], "expected_target": tgt["name"]}

        # 1. re-point this person's state onto the surviving device
        moved: dict[str, int] = {}
        moved["messages"] = con.execute(
            "UPDATE messages SET sender_device_id=?, sender_label=? WHERE sender_device_id=?",
            (target_id, tgt["name"], source_id)).rowcount
        moved["device_events"] = con.execute(
            "UPDATE device_events SET device_id=? WHERE device_id=?",
            (target_id, source_id)).rowcount
        moved["device_receipts"] = con.execute(
            "UPDATE device_receipts SET device_id=? WHERE device_id=?",
            (target_id, source_id)).rowcount
        # push_subscriptions is created lazily by web_push only once notifications are set
        # up; a brain that never enabled push has no such table, so skip it gracefully.
        has_push = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='push_subscriptions'"
        ).fetchone() is not None
        moved["push_subscriptions"] = con.execute(
            "UPDATE push_subscriptions SET device_id=?, device_label=? WHERE device_id=?",
            (target_id, tgt["name"], source_id)).rowcount if has_push else 0

        # 2. union capabilities, keeping only valid ones for the shared profile
        profile = tgt["profile"] or "unknown"
        plan = _ROLE_PLAN.get(profile, _ROLE_PLAN["unknown"])
        s_caps = (src["capabilities"] or "").split(",") if src["capabilities"] else []
        t_caps = (tgt["capabilities"] or "").split(",") if tgt["capabilities"] else []
        merged_caps = sorted({c for c in (s_caps + t_caps)
                              if c in _CAPABILITY_INFO and plan.get(c) != "off_by_design"})
        # 3. keep the target's chosen name; adopt the source's only if target had none
        display_name = tgt["display_name"] or src["display_name"] or ""
        con.execute(
            "UPDATE paired_devices SET capabilities=?, display_name=?, last_seen=? WHERE device_id=?",
            (",".join(merged_caps), display_name, _now_iso(), target_id))
        # 4. revoke the source (kept for audit, not deleted)
        con.execute("UPDATE paired_devices SET revoked=1 WHERE device_id=?", (source_id,))
        _write_receipt(con, device_id=target_id, device_name=tgt["name"], event="merged_in",
                       detail=f"absorbed {src['name']} ({source_id})")
        _write_receipt(con, device_id=source_id, device_name=src["name"], event="merged_into",
                       detail=f"folded into {tgt['name']} ({target_id})")
        con.commit()
    return {"ok": True, "source_id": source_id, "target_id": target_id,
            "source_name": src["name"], "target_name": tgt["name"],
            "moved": moved, "capabilities": merged_caps, "display_name": display_name,
            "source_revoked": True}


def set_device_capabilities(device_id: str, enabled: list[str]) -> dict[str, Any]:
    """A device reports which capabilities it has actually enabled (e.g. after the user
    granted notification access). Only known capabilities are accepted; off_by_design
    caps for the role are ignored (a tablet can't enable personal notification capture).
    Leaves a receipt."""
    from .db import connect
    if not isinstance(enabled, list):
        return {"ok": False, "error": "enabled must be a list"}
    with connect() as con:
        _ensure_tables(con)
        row = con.execute(
            "SELECT name, profile, revoked FROM paired_devices WHERE device_id=?",
            (device_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "unknown device"}
        if row["revoked"]:
            return {"ok": False, "error": "device is revoked"}
        profile = row["profile"] or "unknown"
        plan = _ROLE_PLAN.get(profile, _ROLE_PLAN["unknown"])
        accepted = [c for c in enabled
                    if c in _CAPABILITY_INFO and plan.get(c) != "off_by_design"]
        con.execute("UPDATE paired_devices SET capabilities=?, last_seen=? WHERE device_id=?",
                    (",".join(sorted(set(accepted))), _now_iso(), device_id))
        _write_receipt(con, device_id=device_id, device_name=row["name"],
                       event="capabilities_updated", detail=",".join(sorted(set(accepted))))
        con.commit()
    return {"ok": True, "device_id": device_id, "enabled": sorted(set(accepted)),
            "capability_status": capability_status(profile, accepted)}


def register_login_device(*, display_name: str, profile: str, device_type: str = "phone") -> dict[str, Any]:
    """Mint a fresh device + token for a CREDENTIAL login (no pairing code), so someone who cleared
    or lost their device token can get back in with their username + password. The profile is
    decided by the caller from the account (owner vs guest), never chosen by the client."""
    from .db import connect
    display_name = _normalize_person_name(display_name) or "user"
    device_name = _normalize_device_name(display_name + "'s device", device_type)
    profile = profile if profile in ("primary_phone", "trusted_guest") else "trusted_guest"
    device_id = secrets.token_hex(8)
    device_token = secrets.token_urlsafe(_DEVICE_TOKEN_BYTES)
    now = _now_iso()
    caps = ",".join(_default_caps(profile))
    with connect() as con:
        _ensure_tables(con)
        con.execute(
            "INSERT INTO paired_devices(device_id, name, device_type, profile, token_hash, "
            "capabilities, paired_at, last_seen, transport, display_name, revoked) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0)",
            (device_id, device_name[:80], (device_type or "phone")[:32], profile,
             _hash(device_token), caps, now, now, "login", display_name[:80]))
        con.commit()
    return {"ok": True, "device_id": device_id, "device_token": device_token,
            "profile": profile, "display_name": display_name}


def device_for_token(supplied_token: str) -> dict[str, Any] | None:
    """Resolve a device token to its (non-revoked) device record, or None. Used by the
    request guard to authenticate paired devices and reject revoked ones."""
    if not supplied_token:
        return None
    from .db import connect
    th = _hash(supplied_token)
    with connect() as con:
        _ensure_tables(con)
        row = con.execute(
            "SELECT device_id, name, display_name, profile, capabilities, revoked FROM paired_devices "
            "WHERE token_hash=?", (th,)).fetchone()
    if row is None or row["revoked"]:
        return None
    # touch last_seen
    with connect() as con:
        con.execute("UPDATE paired_devices SET last_seen=? WHERE device_id=?",
                    (_now_iso(), row["device_id"]))
        con.commit()
    return {"device_id": row["device_id"], "name": row["name"],
            "display_name": row["display_name"],
            "profile": row["profile"],
            "capabilities": (row["capabilities"] or "").split(",") if row["capabilities"] else []}


_ALLOWED_DEVICE_EVENTS = {
    "device_checkin",
    "pwa_installed",
    "capability_update",
    "voice_available",
    "notifications_permission_missing",
    "folders_not_configured",
    "share_intake_created",
    "ambient_presence_update",
    "offline_queue_flushed",
}
_MAX_EVENT_PAYLOAD_CHARS = 8000
_MAX_SHARE_CHARS = 12000


def device_whoami(device_id: str) -> dict[str, Any] | None:
    """Safe metadata for the paired device itself. No secrets."""
    for d in list_devices():
        if d["device_id"] == device_id:
            return d
    return None


def set_device_display_name(device_id: str, display_name: str) -> dict[str, Any]:
    """Set the name the person wants Mongoose to call them by, for this device. Captured
    at first run. Distinct from the device label. Trimmed to 40 chars; blank clears it."""
    from .db import connect
    dn = (display_name or "").strip()[:40]
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT revoked FROM paired_devices WHERE device_id=?", (device_id,)).fetchone()
        if row is None or row["revoked"]:
            return {"ok": False, "error": "unknown or revoked device"}
        con.execute("UPDATE paired_devices SET display_name=? WHERE device_id=?", (dn or None, device_id))
        con.commit()
    return {"ok": True, "device_id": device_id, "display_name": dn or None}


def device_recently_active(device_id: str, within_seconds: int = 300) -> bool:
    """True if this device has been seen within the window. Used to tell a sender honestly
    whether a message will land now ('sent') or wait for the recipient to open the app ('saved')."""
    who = device_whoami(device_id)
    if not who:
        return False
    last = who.get("last_seen")
    if not last:
        return False
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() <= within_seconds
    except Exception:
        return False


def heartbeat(device_id: str) -> dict[str, Any]:
    """Touch last_seen and return safe device state. No noisy receipt on every beat."""
    from .db import connect
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT revoked FROM paired_devices WHERE device_id=?", (device_id,)).fetchone()
        if row is None or row["revoked"]:
            return {"ok": False, "error": "unknown or revoked device"}
        con.execute("UPDATE paired_devices SET last_seen=? WHERE device_id=?", (_now_iso(), device_id))
        con.commit()
    return {"ok": True, "device": device_whoami(device_id)}


def record_device_event(device_id: str, event_type: str, payload: dict[str, Any] | None = None,
                        privacy: str = "medium") -> dict[str, Any]:
    """Safe envelope for future Android/PWA events. This is deliberately small and
    allowlisted: it lets the phone/tablet contribute state without becoming a general
    API tunnel."""
    import json
    from .db import connect
    event_type = str(event_type or "")
    if event_type not in _ALLOWED_DEVICE_EVENTS:
        return {"ok": False, "error": "unsupported device event"}
    payload = payload if isinstance(payload, dict) else {}
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) > _MAX_EVENT_PAYLOAD_CHARS:
        return {"ok": False, "error": "device event payload too large"}
    privacy = privacy if privacy in ("low", "medium", "high", "secret") else "medium"
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT name, revoked FROM paired_devices WHERE device_id=?", (device_id,)).fetchone()
        if row is None or row["revoked"]:
            return {"ok": False, "error": "unknown or revoked device"}
        now = _now_iso()
        con.execute("UPDATE paired_devices SET last_seen=? WHERE device_id=?", (now, device_id))
        con.execute("INSERT INTO device_events(device_id,event_type,privacy,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (device_id, event_type, privacy, raw, now))
        if event_type in {"pwa_installed", "capability_update", "voice_available",
                          "notifications_permission_missing", "folders_not_configured",
                          "offline_queue_flushed", "share_intake_created"}:
            _write_receipt(con, device_id=device_id, device_name=row["name"],
                           event=event_type, detail="")
        con.commit()
    return {"ok": True, "device_id": device_id, "event_type": event_type}


def share_to_mongoose(device_id: str, *, title: str = "", note: str = "", url: str = "",
                      project: str = "") -> dict[str, Any]:
    """PWA precursor to Android Share Sheet: a paired device can drop a bounded note/link
    into the local bus. No files, no cloud, no acting."""
    from .db import connect
    from .services.bus import send_message
    title = str(title or "").strip()[:120] or "Shared from device"
    note = str(note or "").strip()
    url = str(url or "").strip()
    project = str(project or "").strip()[:80]
    body_parts = []
    if url:
        body_parts.append(f"URL: {url}")
    if project:
        body_parts.append(f"Project/thread: {project}")
    if note:
        body_parts.append(note)
    body = "\n\n".join(body_parts)[:_MAX_SHARE_CHARS]
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT name, revoked FROM paired_devices WHERE device_id=?", (device_id,)).fetchone()
        if row is None or row["revoked"]:
            return {"ok": False, "error": "unknown or revoked device"}
        device_name = row["name"]
    res = send_message(sender=f"device:{device_name}", to="mongoose", subject=title, body=body, type="note")
    record_device_event(device_id, "share_intake_created", {"title": title, "has_url": bool(url)}, "medium")
    return {"ok": bool(res.get("ok")), "message": res, "title": title}


def recent_device_events(limit: int = 50) -> list[dict[str, Any]]:
    from .db import connect
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT device_id,event_type,privacy,payload_json,created_at FROM device_events "
            "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    return [dict(r) for r in rows]
