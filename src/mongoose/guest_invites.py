from __future__ import annotations

"""Guest URL invites.

This is deliberately separate from the short QR pairing-code flow. A guest invite is a
capability URL the owner creates from Cockpit and sends to a friend. It is longer-lived
(default 5 days), requires a DDNS + HTTPS public URL by default, creates a scoped
trusted_guest device token when claimed, and records the guest's consent choices for
what may be shown to other guests later.
"""

import hashlib
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from . import __version__

_DEFAULT_VALID_DAYS = 5
_MAX_VALID_DAYS = 30
_DEFAULT_MIN_VERSION = "1.1"
_TOKEN_BYTES = 32
_MAX_OWNER_LABEL = 80
_MAX_GUEST_NAME = 80
_MAX_PUBLIC_NAME = 80
_MAX_FUN_FACT = 280


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalise_text(value: Any, *, fallback: str = "", limit: int = 120) -> str:
    raw = str(value or fallback or "")
    # Single-line, no controls, collapsed whitespace. Keeps names safe for receipts/UI.
    cleaned = "".join(ch for ch in raw if ch == " " or (ord(ch) >= 32 and ord(ch) != 127))
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:limit]


def _normalise_fun_fact(value: Any) -> str:
    raw = str(value or "")
    cleaned = "".join(ch for ch in raw if ch in "\n\r\t" or (ord(ch) >= 32 and ord(ch) != 127))
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:_MAX_FUN_FACT]


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _valid_days(value: Any) -> int:
    try:
        days = int(value)
    except Exception:
        days = _DEFAULT_VALID_DAYS
    return max(1, min(_MAX_VALID_DAYS, days))


def _normalise_version(value: Any) -> str:
    raw = str(value or _DEFAULT_MIN_VERSION).strip().lower().lstrip("v")
    parts = []
    for p in raw.replace("-", ".").split("."):
        if p.isdigit():
            parts.append(str(int(p)))
        else:
            break
    return ".".join(parts[:3]) or _DEFAULT_MIN_VERSION


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = []
    for p in _normalise_version(value).split("."):
        try:
            parts.append(int(p))
        except Exception:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def version_gte(actual: str, minimum: str) -> bool:
    return _version_tuple(actual) >= _version_tuple(minimum)


def _ensure_tables(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS guest_invites (
            invite_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            owner_label TEXT NOT NULL,
            owner_label_salt TEXT NOT NULL,
            owner_label_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            expires_ts REAL NOT NULL,
            valid_days INTEGER NOT NULL,
            min_version TEXT NOT NULL,
            claimed_at TEXT,
            claimed_device_id TEXT,
            revoked INTEGER NOT NULL DEFAULT 0,
            suggested_name TEXT NOT NULL DEFAULT ''
        )"""
    )
    # Migration for installs whose guest_invites table predates suggested_name.
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(guest_invites)").fetchall()]
        if "suggested_name" not in cols:
            con.execute("ALTER TABLE guest_invites ADD COLUMN suggested_name TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    con.execute(
        """CREATE TABLE IF NOT EXISTS guest_profiles (
            device_id TEXT PRIMARY KEY,
            invite_id TEXT,
            owner_label TEXT,
            mongoose_name TEXT NOT NULL,
            public_name TEXT NOT NULL,
            fun_fact TEXT,
            announce_join INTEGER NOT NULL DEFAULT 0,
            share_fun_fact INTEGER NOT NULL DEFAULT 0,
            min_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def _effective_env_for_readiness() -> dict[str, str]:
    try:
        from .setup_wizard import effective_env
        return dict(effective_env())
    except Exception:
        import os
        return dict(os.environ)


def _saved_env_for_readiness() -> dict[str, str]:
    try:
        from .setup_wizard import _read_env
        return dict(_read_env())
    except Exception:
        return {}


def guest_invite_prerequisites(env: dict | None = None) -> dict[str, Any]:
    """Return the public base URL state for guest invites.

    Guest invites need a public HTTPS base URL and a server actually reachable from the
    network. They do NOT need the owner's allow_remote flag: guest/device routes are
    authorized by their own scoped device token, independent of allow_remote (see
    _guard_sensitive_routes' "does NOT depend on allow_remote" -- requiring it on top of a
    device token dead-ends a freshly paired phone for no security gain, and gating invite
    CREATION on it blocked invites even when DDNS was genuinely configured). Reads the saved
    .env alongside the live process, so a setting saved by the wizard is not reported as
    missing purely because this process has not restarted since; a genuinely unconfigured or
    not-yet-restarted server is still correctly blocked, just with an honest reason.
    """
    explicit_env = env is not None
    env = dict(env or _effective_env_for_readiness())
    saved_env = {} if explicit_env else _saved_env_for_readiness()
    for key, value in saved_env.items():
        if value and key not in env:
            env[key] = value
    problems: list[dict[str, str]] = []
    base = None
    try:
        from .services.remote_access import public_base_url
        base = public_base_url(env)
    except Exception:
        base = None
    if not base:
        try:
            from .ddns import public_url
            base = public_url(env)
        except Exception:
            base = None
    # Tunnel mode changes what "reachable" means: cloudflared connects OUTBOUND to the
    # local origin, so a loopback-only Mongoose is fully correct for guests, and demanding
    # a LAN bind here would send the owner to fix something that is not broken (external
    # review caught exactly this: the old checks collapsed every route into the
    # DDNS/router-forward model).
    tunnel_mode = False
    try:
        from .cloudflare_tunnel import tunnel_state
        tunnel_mode = bool(tunnel_state(env).get("configured"))
    except Exception:
        tunnel_mode = False
    if tunnel_mode:
        # Config presence is not reachability. Audit of 1.50.4 proved the exact failure:
        # a configured-but-dead tunnel still produced a normal-looking invite link, so the
        # guest's first contact with Mongoose was a browser error page, precisely the
        # experience this whole route exists to eliminate. Invite creation in tunnel mode
        # therefore requires the live end-to-end proof (public /health answering with THIS
        # Mongoose), the same standard setup_state's ready_via_tunnel already applies.
        try:
            from .cloudflare_tunnel import tunnel_state as _tst, tunnel_end_to_end_proof
            _st = _tst(env)
            _proof = tunnel_end_to_end_proof(_st.get("hostname", ""))
            if not _proof.get("ok"):
                problems.append({
                    "code": "tunnel_not_proven",
                    "message": "Cloudflare Tunnel is configured, but the public guest route is not live yet.",
                    "action": "Open Guest access (Cloudflare Tunnel) and repair until the public check passes.",
                })
        except Exception:
            problems.append({
                "code": "tunnel_not_proven",
                "message": "Could not verify the Cloudflare Tunnel guest route is live.",
                "action": "Open Guest access (Cloudflare Tunnel) and run the check before inviting guests.",
            })

    if not base:
        problems.append({
            "code": "no_dynamic_dns",
            "message": "Set up a public HTTPS address first so the guest link works away from home.",
            "action": "Open Setup → Guest access (Cloudflare Tunnel), the guest-friendly route; or Dynamic DNS.",
        })
    elif not str(base).startswith("https://"):
        problems.append({
            "code": "no_https",
            "message": "Guest invites require HTTPS, not HTTP, so phones trust the microphone and the link is private.",
            "action": "Use Cloudflare Tunnel (trusted HTTPS automatically), or finish TLS for your DDNS name.",
        })

    try:
        from .settings import get_settings
        s = get_settings()
        live_host = str(getattr(s, "host", "localhost") or "localhost")
        saved_host = str(saved_env.get("MONGOOSE_HOST") or env.get("MONGOOSE_HOST") or live_host)
        live_bound = live_host in ("0.0.0.0", "::")
        saved_bound = saved_host in ("0.0.0.0", "::")
        if tunnel_mode:
            # cloudflared reaches Mongoose on loopback; no LAN bind is needed for guests,
            # and requiring one would reintroduce exposure the tunnel exists to avoid.
            pass
        elif saved_bound and not live_bound:
            problems.append({
                "code": "remote_restart_needed",
                "message": "Public ingress is configured and LAN binding is saved, but the running Mongoose process is still local-only.",
                "action": "Use Repair / restart LAN access so the saved settings take effect.",
            })
        elif not (live_bound or saved_bound):
            problems.append({
                "code": "server_not_network_bound",
                "message": "Public ingress is configured, but this Mongoose server is still bound to this Mac only.",
                "action": "Use Cloudflare Tunnel (works with this-Mac-only binding), Enable LAN access, or Repair / restart.",
            })
    except Exception:
        problems.append({
            "code": "remote_unknown",
            "message": "Mongoose could not confirm whether the running server is reachable from the network.",
            "action": "Run the setup repair check before sending guest invites.",
        })

    return {"ok": not problems, "base_url": base or "", "problems": problems}


def create_guest_invite(*, owner_label: str, valid_days: int | str | None = None,
                        min_version: str | None = None, base_url: str | None = None,
                        suggested_name: str | None = None) -> dict[str, Any]:
    """Create a long-lived, single-use guest invite URL.

    owner_label is the private owner-side name (e.g. "Sam" or "Mum"). It is stored
    internally, never placed in the URL, and bound to a salted hash so the invite has a
    stable non-plaintext fingerprint for future dedupe/audit work.
    """
    from .db import connect

    label = _normalise_text(owner_label, limit=_MAX_OWNER_LABEL)
    # The name the GUEST will see prefilled (and can change). Defaults to the owner label,
    # which is usually exactly the friend's name anyway; stored plainly because it is shown
    # to the invite holder by design.
    shown_name = _normalise_text(suggested_name if suggested_name is not None else owner_label, limit=80)
    if not label:
        return {"ok": False, "error": "owner_label_required",
                "message": "Say what you call this guest internally first."}

    if base_url is None:
        pre = guest_invite_prerequisites()
        if not pre.get("ok"):
            return {"ok": False, "error": "guest_remote_access_not_ready",
                    "message": "Guest invite links need HTTPS + Dynamic DNS before they are safe to send.",
                    "problems": pre.get("problems", []), "base_url": pre.get("base_url", "")}
        base_url = str(pre.get("base_url") or "").rstrip("/")
    else:
        base_url = str(base_url).rstrip("/")
        if not base_url.startswith("https://"):
            return {"ok": False, "error": "https_required", "message": "Guest invite base_url must be HTTPS."}

    days = _valid_days(valid_days)
    minimum = _normalise_version(min_version or _DEFAULT_MIN_VERSION)
    invite_id = secrets.token_hex(8)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    salt = secrets.token_urlsafe(16)
    token_hash = _hash(token)
    label_hash = _hash(f"{salt}:{label}")
    now = time.time()
    expires_ts = now + days * 24 * 3600
    created_at = _iso_from_ts(now)
    expires_at = _iso_from_ts(expires_ts)

    with connect() as con:
        _ensure_tables(con)
        con.execute(
            "INSERT INTO guest_invites(invite_id, token_hash, owner_label, owner_label_salt, "
            "owner_label_hash, created_at, expires_at, expires_ts, valid_days, min_version, revoked, "
            "suggested_name) VALUES(?,?,?,?,?,?,?,?,?,?,0,?)",
            (invite_id, token_hash, label, salt, label_hash, created_at, expires_at,
             expires_ts, days, minimum, shown_name),
        )
        con.commit()

    try:
        from .font_assets import kick_rajdhani_provisioning
        display_font = kick_rajdhani_provisioning(timeout=12.0)
    except Exception as exc:
        display_font = {"ok": False, "family": "Rajdhani", "source": "error", "message": str(exc)}

    invite_url = f"{base_url}/guest/invite/{token}"
    return {"ok": True, "invite_id": invite_id, "invite_url": invite_url,
            "expires_at": expires_at, "expires_in_days": days, "min_version": minimum,
            "owner_label": label, "suggested_name": shown_name, "https": True, "dynamic_dns": True,
            "display_font": display_font}


def _invite_row(con, token: str):
    _ensure_tables(con)
    return con.execute(
        "SELECT * FROM guest_invites WHERE token_hash=?",
        (_hash(str(token or "")),),
    ).fetchone()


def invite_status(token: str) -> dict[str, Any]:
    from .db import connect
    with connect() as con:
        row = _invite_row(con, token)
    if row is None:
        return {"ok": False, "error": "invalid_invite", "message": "This invite link does not look right. Open the latest link your host sent you, or ask them for a new one."}
    now = time.time()
    if row["revoked"]:
        return {"ok": False, "error": "revoked_invite", "message": "This invite was cancelled. Ask your host for a new invite if you still need access."}
    if row["claimed_at"]:
        return {"ok": False, "error": "already_claimed", "message": "This invite has already been used. If that was you and you are now signed out (for example after adding Mongoose to your home screen), ask your host to reset your password, then sign in with your username. Otherwise ask your host for a new invite."}
    if float(row["expires_ts"]) < now:
        return {"ok": False, "error": "expired_invite", "message": "This invite has expired. Ask your host for a fresh invite link.",
                "expires_at": row["expires_at"]}
    try:
        shown = row["suggested_name"]
    except (IndexError, KeyError):
        shown = ""
    return {"ok": True, "expires_at": row["expires_at"], "min_version": row["min_version"],
            "suggested_name": shown or "", "codename": "Sillygoose", "server_version": __version__}


def claim_guest_invite(token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Claim a guest invite and return a device token once.

    This creates a trusted_guest device and a guest profile. Guests never get owner tools,
    Cockpit access, owner memory, or shared conversation history; existing /device/chat
    isolation handles that once the token is stored on their browser.
    """
    from .db import connect
    from .devices import (_DEVICE_TOKEN_BYTES, _default_caps, _ensure_tables as _ensure_device_tables,
                          _hash as _device_hash, _normalize_device_name, _write_receipt,
                          _suggest_username, _generate_passphrase)

    payload = payload if isinstance(payload, dict) else {}
    mongoose_name = _normalise_text(payload.get("mongoose_name"), limit=_MAX_GUEST_NAME)
    public_name = _normalise_text(payload.get("public_name") or mongoose_name, limit=_MAX_PUBLIC_NAME)
    fun_fact = _normalise_fun_fact(payload.get("fun_fact"))
    # Joining the room IS the consent for room-level social behaviour: the lounge name and
    # the fun fact were typed into the LOUNGE onboarding form. These flags exist only as an
    # Consent defaults match the UI, which presents both as UNCHECKED opt-ins: an API
    # caller that omits the fields must not share more than the visible form would.
    # (Audit note: absent-means-yes made the backend more permissive than the UI.)
    announce_join = _boolish(payload.get("announce_join", False))
    share_fun_fact = _boolish(payload.get("share_fun_fact", False))
    client_version = _normalise_version(payload.get("client_version") or __version__)

    if not mongoose_name:
        return {"ok": False, "error": "mongoose_name_required",
                "message": "Please say how you want Mongoose to refer to you."}
    if not public_name:
        return {"ok": False, "error": "public_name_required",
                "message": "Please say how human guests should refer to you."}
    if not fun_fact:
        return {"ok": False, "error": "fun_fact_required",
                "message": "Add one tiny fun fact before you come in."}

    with connect() as con:
        _ensure_tables(con)
        _ensure_device_tables(con)
        try:
            con.execute("BEGIN IMMEDIATE")
        except Exception:
            pass
        row = _invite_row(con, token)
        if row is None:
            return {"ok": False, "error": "invalid_invite", "message": "This invite link does not look right. Open the latest link your host sent you, or ask them for a new one."}
        if row["revoked"]:
            return {"ok": False, "error": "revoked_invite", "message": "This invite was cancelled. Ask your host for a new invite if you still need access."}
        if row["claimed_at"]:
            return {"ok": False, "error": "already_claimed", "message": "This invite has already been used. If that was you and you are now signed out (for example after adding Mongoose to your home screen), ask your host to reset your password, then sign in with your username. Otherwise ask your host for a new invite."}
        if float(row["expires_ts"]) < time.time():
            return {"ok": False, "error": "expired_invite", "message": "This invite has expired. Ask your host for a fresh invite link."}
        minimum = row["min_version"] or _DEFAULT_MIN_VERSION
        # Gate on the TRUSTED running server version, not a value the browser hands us. In this web
        # invite flow the "client" is the server's own page, so a self-reported client_version is not
        # a real compatibility gate (a direct POST could lie). client_version is kept for diagnostics.
        if not version_gte(__version__, minimum):
            return {"ok": False, "error": "server_version_too_old",
                    "message": f"This invite needs Mongoose {minimum} or newer; this server is {__version__}.",
                    "min_version": minimum, "server_version": __version__, "client_version": client_version}

        profile = "trusted_guest"
        device_id = secrets.token_hex(8)
        device_token = secrets.token_urlsafe(_DEVICE_TOKEN_BYTES)
        now_iso = _now_iso()
        device_name = _normalize_device_name(mongoose_name, "guest_web")
        caps = ",".join(_default_caps(profile))
        # Claim ATOMICALLY: this UPDATE only matches while the invite is still unclaimed, unrevoked
        # and unexpired, so exactly one of two browsers racing the same link can match (claimed_at IS
        # NULL). The loser gets rowcount 0 and falls through to a re-read + a precise error, instead
        # of both creating a guest and the second silently overwriting claimed_device_id.
        cur = con.execute(
            "UPDATE guest_invites SET claimed_at=?, claimed_device_id=? "
            "WHERE invite_id=? AND claimed_at IS NULL AND revoked=0 AND expires_ts>=?",
            (now_iso, device_id, row["invite_id"], time.time()),
        )
        if cur.rowcount != 1:
            fresh = _invite_row(con, token)
            if fresh is None:
                return {"ok": False, "error": "invalid_invite", "message": "This invite link does not look right. Open the latest link your host sent you, or ask them for a new one."}
            if fresh["revoked"]:
                return {"ok": False, "error": "revoked_invite", "message": "This invite was cancelled. Ask your host for a new invite if you still need access."}
            if float(fresh["expires_ts"]) < time.time():
                return {"ok": False, "error": "expired_invite", "message": "This invite has expired. Ask your host for a fresh invite link."}
            return {"ok": False, "error": "already_claimed", "message": "This invite has already been used. If that was you and you are now signed out (for example after adding Mongoose to your home screen), ask your host to reset your password, then sign in with your username. Otherwise ask your host for a new invite."}
        con.execute(
            "INSERT INTO paired_devices(device_id, name, display_name, device_type, profile, token_hash, "
            "capabilities, paired_at, last_seen, transport, revoked) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
            (device_id, device_name[:80], mongoose_name[:40], "guest_web", profile, _device_hash(device_token),
             caps, now_iso, now_iso, "guest_invite"),
        )
        con.execute(
            "INSERT INTO guest_profiles(device_id, invite_id, owner_label, mongoose_name, public_name, "
            "fun_fact, announce_join, share_fun_fact, min_version, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, row["invite_id"], row["owner_label"], mongoose_name, public_name,
             fun_fact, int(announce_join), int(share_fun_fact), minimum, now_iso, now_iso),
        )
        detail = (f"invite={row['invite_id']}; owner_label_hash={row['owner_label_hash'][:10]}; "
                  f"public_name={public_name}; announce_join={int(announce_join)}; "
                  f"share_fun_fact={int(share_fun_fact)}")
        _write_receipt(con, device_id=device_id, device_name=device_name,
                       event="guest_invite_claimed", detail=detail)
        con.commit()

    # If the guest consented to being announced, add a small public join beat to the
    # shared lounge. This is deliberately best-effort: the invitation claim must not fail
    # just because the optional group narrative nicety could not be posted.
    try:
        from .services.lounge_surpriser import note_guest_joined
        note_guest_joined(device_id=device_id, public_name=public_name, mongoose_name=mongoose_name,
                          fun_fact=fun_fact, announce_join=announce_join,
                          share_fun_fact=share_fun_fact)
    except Exception:
        pass

    # Offer a personal identity the same way owner pairing does: a friendly suggested username
    # and a memorable passphrase, shown to the guest to save before first use (copiable and
    # changeable). The account is only created when they save it (register handles collisions);
    # the device token already works for chat, so saving a login is optional, not a gate.
    return {"ok": True, "device_id": device_id, "device_token": device_token,
            "profile": profile, "capabilities": _default_caps(profile),
            "mongoose_name": mongoose_name, "public_name": public_name,
            "announce_join": announce_join, "share_fun_fact": share_fun_fact,
            "suggested_username": _suggest_username(public_name),
            "suggested_password": _generate_passphrase()}


def delete_invite_by_token(token: str) -> dict[str, Any]:
    """Invalidate an invite from its own landing page ("Delete this invitation").

    The bearer of the link is the only party who could call this, and a bearer who wants
    the invitation gone should get exactly that: the token is single-use anyway, so
    honouring the request costs nothing and respects the guest's choice. Idempotent."""
    from .db import connect
    token_hash = _hash(str(token or ""))
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT invite_id, revoked FROM guest_invites WHERE token_hash=?",
                          (token_hash,)).fetchone()
        if row is None:
            return {"ok": False, "error": "invalid_invite"}
        con.execute("UPDATE guest_invites SET revoked=1 WHERE token_hash=?", (token_hash,))
        con.commit()
    return {"ok": True, "deleted": True}


def list_guest_invites(limit: int = 50) -> list[dict[str, Any]]:
    from .db import connect
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT invite_id, owner_label, owner_label_hash, created_at, expires_at, valid_days, "
            "min_version, claimed_at, claimed_device_id, revoked FROM guest_invites "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit or 50), 200)),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["revoked"] = bool(d.get("revoked"))
        d["claimed"] = bool(d.get("claimed_at"))
        out.append(d)
    return out


def guest_public_directory() -> list[dict[str, str]]:
    """Guest-visible directory honoring consent choices. Excludes revoked guests by joining to the
    device table, a removed guest must not keep appearing to other guests."""
    from .db import connect
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT gp.public_name, gp.fun_fact, gp.announce_join, gp.share_fun_fact "
            "FROM guest_profiles gp JOIN paired_devices pd ON pd.device_id = gp.device_id "
            "WHERE gp.announce_join=1 AND pd.revoked=0 ORDER BY gp.created_at DESC LIMIT 50"
        ).fetchall()
    directory = []
    for r in rows:
        item = {"public_name": r["public_name"]}
        if r["share_fun_fact"]:
            item["fun_fact"] = r["fun_fact"] or ""
        directory.append(item)
    return directory


def list_invites(limit: int = 100) -> list[dict[str, Any]]:
    """Owner-facing list of guest invites (newest first) for the cockpit, so a misdirected or
    unwanted invite can be found and revoked. Never includes the raw token (only its id/label/state)."""
    from .db import connect
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT invite_id, owner_label, created_at, expires_at, expires_ts, claimed_at, "
            "claimed_device_id, revoked FROM guest_invites ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    now = time.time()
    out = []
    for r in rows:
        expired = bool(r["expires_ts"]) and float(r["expires_ts"]) < now
        state = ("revoked" if r["revoked"] else "claimed" if r["claimed_at"]
                 else "expired" if expired else "pending")
        out.append({
            "invite_id": r["invite_id"], "owner_label": r["owner_label"],
            "created_at": r["created_at"], "expires_at": r["expires_at"],
            "claimed": bool(r["claimed_at"]), "claimed_device_id": r["claimed_device_id"],
            "revoked": bool(r["revoked"]), "expired": expired, "state": state,
        })
    return out


def revoke_invite(invite_id: str) -> dict[str, Any]:
    """Revoke a guest invite by id. Revoking an UNCLAIMED invite stops it being used; revoking a
    CLAIMED invite also revokes the guest device it created, so 'cancel this invite' removes access
    whether or not the guest already came in. Idempotent."""
    iid = (invite_id or "").strip()
    if not iid:
        return {"ok": False, "error": "missing_invite_id"}
    from .db import connect
    from .devices import _ensure_tables as _ensure_device_tables
    with connect() as con:
        _ensure_tables(con)
        _ensure_device_tables(con)
        row = con.execute(
            "SELECT claimed_device_id FROM guest_invites WHERE invite_id=?", (iid,)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "unknown_invite"}
        con.execute("UPDATE guest_invites SET revoked=1 WHERE invite_id=?", (iid,))
        device_revoked = False
        claimed_device = row["claimed_device_id"]
        if claimed_device:
            con.execute("UPDATE paired_devices SET revoked=1 WHERE device_id=?", (claimed_device,))
            device_revoked = True
        con.commit()
    return {"ok": True, "invite_id": iid, "revoked": True, "device_revoked": device_revoked,
            "device_id": claimed_device or None}
