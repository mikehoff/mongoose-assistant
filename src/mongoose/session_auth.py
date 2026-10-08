from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

# Session-cookie auth for LAN access from phones/tablets.
#
# The route guard accepts the API token in an X-Mongoose-Token HEADER. A browser
# opening a URL can't set that header, so the cockpit was Mac-local. This module lets
# a device log in ONCE with the token and receive a signed session cookie; the guard
# then accepts that cookie as equivalent to the header.
#
# Security:
#   - The cookie value is a signed, expiring token (HMAC over an expiry), so it can't
#     be forged without the server secret and it can't outlive its expiry.
#   - It is set http-only + SameSite=Lax by the caller, so script can't read it and
#     cross-site top-level POSTs don't carry it.
#   - Cookies are auto-attached by the browser, which reintroduces CSRF risk that the
#     header approach didn't have. So the cockpit's existing per-action NONCE stays
#     REQUIRED on mutating routes (install/approve/cancel): a cookie alone is not
#     sufficient to mutate. Cookie = "this device may read/use"; nonce = "this exact
#     same-origin page issued this action".
#
# The signing secret is derived from the API token (so it rotates if the token does)
# plus a PERSISTED salt (stored under the data dir, bound to the token). So sessions
# survive a server restart (remember-device truly lasts its TTL), but a token change
# regenerates the salt and invalidates all existing sessions. Remember-device extends
# the expiry. (The _PROCESS_SALT below is only a fallback if the salt file can't be
# written; see _persistent_salt.)

_PROCESS_SALT = secrets.token_bytes(16)

SESSION_COOKIE = "mongoose_session"
_DEFAULT_TTL = 12 * 3600           # 12h: a normal session
_REMEMBER_TTL = 30 * 24 * 3600     # 30d: "remember this device"


def _persistent_salt() -> bytes:
    """A salt persisted on disk so sessions survive a server restart (so "remember
    this device for 30 days" actually means 30 days). Stored under the data dir, tied
    to the current API token: if the token changes, the stored salt is regenerated, so
    changing the token still invalidates old sessions. Falls back to a per-process
    salt if the file can't be written."""
    import hashlib as _h
    from .settings import get_settings
    s = get_settings()
    token = (getattr(s, "api_token", "") or "").encode()
    token_fp = _h.sha256(token).hexdigest()[:16]
    try:
        d = s.data_dir
        d.mkdir(parents=True, exist_ok=True)
        f = d / "session_salt"
        if f.exists():
            stored = f.read_text(encoding="utf-8").strip()
            fp, _, salt_hex = stored.partition(":")
            if fp == token_fp and salt_hex:
                return bytes.fromhex(salt_hex)
        # missing or token changed: mint a fresh salt bound to this token
        salt = secrets.token_bytes(16)
        f.write_text(f"{token_fp}:{salt.hex()}", encoding="utf-8")
        try:
            f.chmod(0o600)
        except Exception:
            pass
        return salt
    except Exception:
        return _PROCESS_SALT


def _secret() -> bytes:
    from .settings import get_settings
    token = (getattr(get_settings(), "api_token", "") or "").encode()
    return hashlib.sha256(b"mongoose-session/" + token + _persistent_salt()).digest()


def issue_session(*, remember: bool = False, user_id: str = "") -> str:
    """Mint a signed session value. Two forms, both signed with the same server secret:
      - bearer (token login):     '<expiry>.<hexmac>'              (no identity)
      - account (username login): '<expiry>.u=<user_id>.<hexmac>'  (identifies the user across devices)
    The MAC covers the expiry AND any bound user_id, so neither can be altered without the secret. The
    account form is what lets one person log in from a phone, a tablet and a laptop and have each device
    hold its own persistent session tied to the same identity."""
    ttl = _REMEMBER_TTL if remember else _DEFAULT_TTL
    expiry = int(time.time()) + ttl
    if user_id:
        payload = f"{expiry}.u={user_id}"
        mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{mac}"
    mac = hmac.new(_secret(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{mac}"


def _parse_session(value: Optional[str]) -> Optional[tuple[int, str, str]]:
    """Return (expiry, user_id, mac) for a well-formed token, else None. user_id is '' for bearer."""
    if not value or "." not in value:
        return None
    parts = value.split(".")
    # account form: <expiry>.u=<user_id>.<mac>
    if len(parts) == 3 and parts[1].startswith("u="):
        try:
            return int(parts[0]), parts[1][2:], parts[2]
        except ValueError:
            return None
    # bearer form: <expiry>.<mac>
    if len(parts) == 2:
        try:
            return int(parts[0]), "", parts[1]
        except ValueError:
            return None
    return None


def verify_session(value: Optional[str]) -> bool:
    """True if `value` is a valid, unexpired session token (either form)."""
    return session_user(value) is not False


def session_user(value: Optional[str]):
    """Verify a session and return the bound user_id (str, possibly '' for a bearer/token session) if
    valid, or False if invalid/expired. Lets callers know WHO is logged in, not just whether."""
    parsed = _parse_session(value)
    if parsed is None:
        return False
    expiry, user_id, mac = parsed
    if expiry < time.time():
        return False
    payload = f"{expiry}.u={user_id}" if user_id else str(expiry)
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return False
    return user_id


def cookie_max_age(remember: bool) -> int:
    return _REMEMBER_TTL if remember else _DEFAULT_TTL


def token_matches(supplied: str) -> bool:
    """Constant-time check of a supplied token against the configured API token."""
    from .settings import get_settings
    token = getattr(get_settings(), "api_token", "") or ""
    return bool(token) and hmac.compare_digest(supplied or "", token)
