"""Self-service user accounts, optional username + password so any phone/tablet user can make their
own login and get a persistent session that works across all their devices.

Why this exists (and how it relates to the token):
  - The base auth model is a single shared API token: a device logs in once with the token and gets a
    signed session cookie (session_auth.py). That's fine for the owner's Mac, but it means everyone
    shares one secret and there's no per-person identity.
  - This module lets anyone who can already reach the cockpit (i.e. already has a session, or knows the
    token) CREATE their own username + password. Logging in with those credentials issues a normal
    session cookie, so the same person can log in from a phone, a tablet, and a laptop and each gets
    its own persistent session. "Remember me" makes that session last 30 days per device.

Security:
  - Passwords are NEVER stored in the clear. We hash with scrypt (memory-hard) + a per-user random salt,
    using only the standard library (no new dependency). The stored value is
    "scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>" so parameters travel with the hash.
  - Verification is constant-time (hmac.compare_digest on the derived hash).
  - Usernames are normalised (lowercased, trimmed) and uniqueness is enforced by the DB.
  - This module issues NO cookies and grants NO authority by itself; the route layer decides when to
    issue a session. Creating an account requires an already-authenticated caller (see app.py), so a
    random unauthenticated LAN client cannot create accounts.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# scrypt cost parameters. n must be a power of two; these are a reasonable interactive-login cost.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

_MIN_USERNAME = 3
_MAX_USERNAME = 32
_MIN_PASSWORD = 8
_MAX_PASSWORD = 256


def _db_path() -> Path:
    from ..settings import get_settings
    s = get_settings()
    return Path(getattr(s, "db_path", None) or (Path(getattr(s, "data_dir", "data")) / "mongoose.sqlite3"))


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path()))
    con.row_factory = sqlite3.Row
    return con


def ensure_table() -> None:
    """Create the accounts table if missing. Called lazily so it works even on an older DB that predates
    this feature (no missing-table crash on upgrade)."""
    con = _con()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_accounts (
                user_id       TEXT PRIMARY KEY,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                last_login_at TEXT,
                display_name  TEXT,
                allow_poke    INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # Migrate older tables that predate display_name / allow_poke
        cols = {r[1] for r in con.execute("PRAGMA table_info(user_accounts)").fetchall()}
        if "display_name" not in cols:
            con.execute("ALTER TABLE user_accounts ADD COLUMN display_name TEXT")
        if "allow_poke" not in cols:
            con.execute("ALTER TABLE user_accounts ADD COLUMN allow_poke INTEGER NOT NULL DEFAULT 1")
        if "owner_device_id" not in cols:
            # Links a poke identity to the owner's paired device so the owner identity is
            # idempotent: we can always find it again without the client remembering a password.
            con.execute("ALTER TABLE user_accounts ADD COLUMN owner_device_id TEXT")
        if "is_owner" not in cols:
            # Whether a credential login for this account restores OWNER (primary_phone) access
            # versus guest access. Set for the owner identity; never for a guest identity.
            con.execute("ALTER TABLE user_accounts ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0")
        if "device_id" not in cols:
            # The device this account currently lives on. Lets a push subscription be bound to the
            # right person even when there is no session cookie (the reason pokes had nowhere to
            # deliver): whoever is on this device IS this account.
            con.execute("ALTER TABLE user_accounts ADD COLUMN device_id TEXT")
        con.commit()
    finally:
        con.close()


def _normalise_username(username: str) -> str:
    return (username or "").strip().lower()


def _valid_username(username: str) -> Optional[str]:
    """Return an error string if invalid, else None. Allows letters, digits, dot, dash, underscore."""
    u = _normalise_username(username)
    if len(u) < _MIN_USERNAME:
        return f"username must be at least {_MIN_USERNAME} characters"
    if len(u) > _MAX_USERNAME:
        return f"username must be at most {_MAX_USERNAME} characters"
    if not all(c.isalnum() or c in "._-" for c in u):
        return "username may only contain letters, digits, and. _ -"
    return None


def _normalise_display_name(value: str | None, fallback: str = "") -> str:
    """Safe human display name for UI and notification headers.

    Display names are reused in push/ntfy HTTP headers and social messages, so
    they must be single-line and control-character free. Treat control chars as
    spaces, collapse whitespace, cap the length, and fall back to username.
    """
    raw = value or ""
    cleaned = "".join(ch if (ch == " " or ord(ch) >= 32) and ord(ch) != 127 else " " for ch in raw)
    cleaned = " ".join(cleaned.split())[:32].strip()
    return cleaned or (fallback or "").strip()[:32] or "user"


def _hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a stored 'scrypt$n$r$p$salt$hash' string."""
    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except Exception:
        return False
    try:
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    except Exception:
        return False
    return hmac.compare_digest(dk, expected)


def create_account(username: str, password: str, display_name: str | None = None) -> dict[str, Any]:
    """Create a new account. Returns {ok, user_id, username, display_name} or {ok:False, error}.
    Never stores the plaintext password. Username uniqueness is enforced; a duplicate is a clean
    error, not a crash. display_name is the name the person wants to be known by; if omitted it
    defaults to the username so the brain always has a human name to use."""
    ensure_table()
    uerr = _valid_username(username)
    if uerr:
        return {"ok": False, "error": uerr}
    if not isinstance(password, str) or len(password) < _MIN_PASSWORD:
        return {"ok": False, "error": f"password must be at least {_MIN_PASSWORD} characters"}
    if len(password) > _MAX_PASSWORD:
        return {"ok": False, "error": "password is too long"}
    u = _normalise_username(username)
    dn = _normalise_display_name(display_name, u)
    user_id = "usr_" + secrets.token_hex(12)
    pw_hash = _hash_password(password)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    con = _con()
    try:
        con.execute(
            "INSERT INTO user_accounts (user_id, username, password_hash, created_at, display_name) "
            "VALUES (?,?,?,?,?)",
            (user_id, u, pw_hash, now, dn),
        )
        con.commit()
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "that username is already taken"}
    finally:
        con.close()
    return {"ok": True, "user_id": user_id, "username": u, "display_name": dn}


def _owner_username_from(display_name: str) -> str:
    """A safe, valid username slug from a display name, e.g. 'Alex' -> 'alex'. Falls back to 'owner'."""
    slug = "".join(ch for ch in (display_name or "").lower() if ch.isalnum())[:_MAX_USERNAME]
    if len(slug) < _MIN_USERNAME:
        slug = (slug + "owner")[:_MAX_USERNAME]
    return slug or "owner"


def get_or_create_owner_account(device_id: str, display_name: str) -> dict[str, Any]:
    """Idempotently return the poke identity bound to the owner's device, creating it on first call.

    The owner is already authenticated by their paired device, so they must never face a poke
    sign-in wall. This binds one poke account to the device (by device_id, not by a client-held
    password), so every call returns the same identity and the owner can always be poked and can
    always poke. Returns {ok, user_id, username, display_name}."""
    ensure_table()
    if not device_id:
        return {"ok": False, "error": "no device"}
    dn = _normalise_display_name(display_name, "owner")
    con = _con()
    try:
        row = con.execute("SELECT user_id, username, display_name FROM user_accounts "
                          "WHERE owner_device_id =?", (device_id,)).fetchone()
        if row is not None:
            con.execute("UPDATE user_accounts SET is_owner =1, device_id =? WHERE user_id =?",
                        (device_id, row["user_id"]))
            con.commit()
            return {"ok": True, "user_id": row["user_id"], "username": row["username"],
                    "display_name": row["display_name"] or dn}
    finally:
        con.close()
    # No account bound to THIS device. Before minting a new one, ADOPT an existing owner identity if
    # there is one: a re-pair gives a fresh device_id, and without this the owner account would
    # multiply (alex, alex1, alex2...) each time the owner re-pairs. One household owner = one identity.
    existing = owner_account()
    if existing:
        con = _con()
        try:
            con.execute("UPDATE user_accounts SET owner_device_id =?, device_id =?, is_owner =1 WHERE user_id =?",
                        (device_id, device_id, existing["user_id"]))
            con.commit()
        finally:
            con.close()
        return {"ok": True, "user_id": existing["user_id"], "username": existing["username"],
                "display_name": existing.get("display_name") or dn}
    # No account yet for this device: mint one with a unique username slug and a random password
    # (the device token is the real authority; the password is never surfaced or needed).
    base = _owner_username_from(dn)
    for attempt in range(0, 50):
        cand = base if attempt == 0 else f"{base}{attempt}"[:_MAX_USERNAME]
        res = create_account(cand, secrets.token_urlsafe(16), display_name=dn)
        if res.get("ok"):
            uid = res["user_id"]
            con = _con()
            try:
                con.execute("UPDATE user_accounts SET owner_device_id =?, device_id =?, allow_poke =1, is_owner =1 WHERE user_id =?",
                            (device_id, device_id, uid))
                con.commit()
            finally:
                con.close()
            return {"ok": True, "user_id": uid, "username": res["username"], "display_name": dn}
    return {"ok": False, "error": "could not allocate an owner username"}


def verify_credentials(username: str, password: str) -> dict[str, Any]:
    """Check a username + password. Returns {ok, user_id, username} on success, else {ok:False, error}.
    The error is deliberately generic (no 'user not found' vs 'wrong password' distinction) to avoid
    confirming which usernames exist. Updates last_login_at on success."""
    ensure_table()
    u = _normalise_username(username)
    con = _con()
    try:
        row = con.execute("SELECT * FROM user_accounts WHERE username =?", (u,)).fetchone()
        if row is None or not _verify_password(password or "", row["password_hash"]):
            return {"ok": False, "error": "invalid username or password"}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        con.execute("UPDATE user_accounts SET last_login_at =? WHERE user_id =?", (now, row["user_id"]))
        con.commit()
        return {"ok": True, "user_id": row["user_id"], "username": row["username"]}
    finally:
        con.close()


def change_password(username: str, old_password: str, new_password: str) -> dict[str, Any]:
    """Change a password after verifying the current one."""
    chk = verify_credentials(username, old_password)
    if not chk.get("ok"):
        return {"ok": False, "error": "current password is incorrect"}
    if not isinstance(new_password, str) or len(new_password) < _MIN_PASSWORD:
        return {"ok": False, "error": f"new password must be at least {_MIN_PASSWORD} characters"}
    if len(new_password) > _MAX_PASSWORD:
        return {"ok": False, "error": "new password is too long"}
    ensure_table()
    con = _con()
    try:
        con.execute("UPDATE user_accounts SET password_hash =? WHERE user_id =?",
                    (_hash_password(new_password), chk["user_id"]))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "user_id": chk["user_id"], "username": chk["username"]}


def get_account(user_id: str) -> Optional[dict[str, Any]]:
    """Look up an account by id (no secrets returned)."""
    ensure_table()
    con = _con()
    try:
        row = con.execute("SELECT user_id, username, created_at, last_login_at, display_name, "
                          "allow_poke, is_owner FROM user_accounts WHERE user_id =?",
                          (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def is_owner_account(user_id: str) -> bool:
    """Whether a credential login for this account should restore OWNER (primary_phone) access."""
    acct = get_account(user_id)
    return bool(acct and acct.get("is_owner"))


def owner_account() -> Optional[dict[str, Any]]:
    """The account that restores owner access on a credential login, if one exists."""
    ensure_table()
    con = _con()
    try:
        row = con.execute("SELECT user_id, username, display_name FROM user_accounts "
                          "WHERE is_owner =1 ORDER BY created_at LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def set_password(user_id: str, new_password: str) -> dict[str, Any]:
    """Set (or reset) an account's password WITHOUT the old one. The caller must have already proven
    authority another way (e.g. the owner's paired device token), so this is how the owner, whose
    identity was auto-created with a random password, chooses a sign-in password they actually know."""
    if not isinstance(new_password, str) or len(new_password) < _MIN_PASSWORD:
        return {"ok": False, "error": f"password must be at least {_MIN_PASSWORD} characters"}
    if len(new_password) > _MAX_PASSWORD:
        return {"ok": False, "error": "password is too long"}
    ensure_table()
    con = _con()
    try:
        cur = con.execute("UPDATE user_accounts SET password_hash =? WHERE user_id =?",
                          (_hash_password(new_password), user_id))
        con.commit()
        if not cur.rowcount:
            return {"ok": False, "error": "no such account"}
    finally:
        con.close()
    return {"ok": True, "user_id": user_id}


_RESET_WORDS = ("otter", "willow", "cobalt", "maple", "harbor", "ember", "tunic", "pebble", "cedar",
                "mango", "violet", "raven", "olive", "copper", "meadow", "quartz", "hazel", "briar")


def generate_passphrase() -> str:
    """A short, human-typeable passphrase (three words + two digits), e.g. 'otter-cedar-mango-42'.
    Readable enough to relay by voice/text and type on a phone, random enough for a reset credential."""
    import secrets
    return "-".join(secrets.choice(_RESET_WORDS) for _ in range(3)) + "-" + str(secrets.randbelow(90) + 10)


def reset_password(user_id: str, *, new_password: str | None = None) -> dict[str, Any]:
    """Owner-authorised password reset: set a fresh, typeable passphrase for an account and return it
    ONCE in plaintext so the host can relay it. The CALLER must have proven owner authority (this backs
    the cockpit-nonce-gated /cockpit/guests/reset-password route); it is not self-serve. Returns
    {ok, user_id, username, display_name, password}."""
    acct = get_account(user_id)
    if not acct:
        return {"ok": False, "error": "no such account"}
    pw = new_password or generate_passphrase()
    res = set_password(user_id, pw)
    if not res.get("ok"):
        return res
    return {"ok": True, "user_id": user_id, "username": acct.get("username"),
            "display_name": acct.get("display_name"), "password": pw}


def account_for_device(device_id: str) -> str:
    """The account user_id currently living on this device, or '' . Used to bind a push
    subscription to the right person when no session cookie is present."""
    if not device_id:
        return ""
    ensure_table()
    con = _con()
    try:
        row = con.execute("SELECT user_id FROM user_accounts WHERE device_id =? "
                          "ORDER BY last_login_at DESC, created_at DESC LIMIT 1",
                          (str(device_id),)).fetchone()
        return row["user_id"] if row else ""
    finally:
        con.close()


def set_account_device(user_id: str, device_id: str) -> None:
    """Record which device an account is currently on (so pushes can bind without a cookie)."""
    if not user_id or not device_id:
        return
    ensure_table()
    con = _con()
    try:
        con.execute("UPDATE user_accounts SET device_id =? WHERE user_id =?", (str(device_id), user_id))
        con.commit()
    finally:
        con.close()


def rebind_owner_device(user_id: str, device_id: str) -> None:
    """Repoint an OWNER account's device link to the device it is currently on (after a re-login on
    a fresh device). No-op for non-owner accounts, so a guest login never claims an owner binding."""
    ensure_table()
    con = _con()
    try:
        con.execute("UPDATE user_accounts SET owner_device_id =? WHERE user_id =? AND is_owner =1",
                    (device_id, user_id))
        con.commit()
    finally:
        con.close()


def account_count() -> int:
    ensure_table()
    con = _con()
    try:
        return int(con.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0])
    finally:
        con.close()


def update_profile(user_id: str, *, display_name: Optional[str] = None,
                   allow_poke: Optional[bool] = None) -> dict[str, Any]:
    """Update display name and/or poke preference for a user."""
    ensure_table()
    con = _con()
    try:
        row = con.execute("SELECT * FROM user_accounts WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "user not found"}
        updates, params = [], []
        if display_name is not None:
            dn = _normalise_display_name(display_name, row["username"])
            updates.append("display_name = ?"); params.append(dn or None)
        if allow_poke is not None:
            updates.append("allow_poke = ?"); params.append(1 if allow_poke else 0)
        if updates:
            params.append(user_id)
            con.execute(f"UPDATE user_accounts SET {', '.join(updates)} WHERE user_id = ?", params)
            con.commit()
        row2 = con.execute("SELECT * FROM user_accounts WHERE user_id = ?", (user_id,)).fetchone()
        return {"ok": True, "user_id": user_id, "username": row2["username"],
                "display_name": row2["display_name"], "allow_poke": bool(row2["allow_poke"])}
    finally:
        con.close()


def list_users(exclude_user_id: str = "") -> list[dict[str, Any]]:
    """Return all users (display name, username, allow_poke): for user discovery."""
    ensure_table()
    con = _con()
    try:
        rows = con.execute(
            "SELECT user_id, username, display_name, allow_poke FROM user_accounts "
            "WHERE user_id != ? ORDER BY display_name, username",
            (exclude_user_id,)).fetchall()
        return [{"user_id": r["user_id"], "username": r["username"],
                 "display_name": r["display_name"] or r["username"],
                 "allow_poke": bool(r["allow_poke"])} for r in rows]
    finally:
        con.close()


_CONFUSABLE = {
    # Cyrillic look-alikes -> Latin
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "х": "x", "у": "y", "к": "k", "м": "m", "т": "t",
    "н": "h", "в": "b", "і": "i", "ј": "j", "ѕ": "s",
    # Greek look-alikes -> Latin
    "α": "a", "ο": "o", "ρ": "p", "ν": "v", "τ": "t",
    "κ": "k", "ι": "i", "ε": "e",
}


def _collision_key(name: str) -> str:
    """Impersonation-resistant key for a display name. Folds case + Unicode compatibility forms,
    maps common Cyrillic/Greek look-alikes to Latin, then drops everything that is not a letter or
    digit (spaces, zero-width, punctuation). So 'Alex', ' Alex', 'Mik e *', and Cyrillic 'Mіke'
    all reduce to the SAME key, and a second person cannot slip past the display-name check with
    spacing, punctuation, or homoglyph tricks."""
    import unicodedata
    s = unicodedata.normalize("NFKC", name or "").casefold()
    s = "".join(_CONFUSABLE.get(ch, ch) for ch in s)
    return "".join(ch for ch in s if ch.isalnum())


def display_name_taken(display_name: str, exclude_user_id: str = "") -> bool:
    """True if another account already uses a CONFUSABLY-EQUAL display name (see _collision_key).
    Display name is the human-visible label (poke, attribution, the lounge); a name that merely
    LOOKS the same as an existing person is the impersonation accident to catch, not just a
    byte-identical one. Deliberately NOT a DB unique constraint, so this is a soft, overridable
    check that callers opt into (pairing does, to stop a second visible 'Alex' at the door)."""
    key = _collision_key(display_name)
    if not key:
        return False
    for u in list_users(exclude_user_id):
        if _collision_key(u.get("display_name") or "") == key:
            return True
    return False


def find_user(name: str) -> Optional[dict[str, Any]]:
    """Find a user by username or display_name (case-insensitive)."""
    ensure_table()
    n = (name or "").strip().lower()
    con = _con()
    try:
        row = con.execute(
            "SELECT * FROM user_accounts WHERE lower(username)=? OR lower(display_name)=?",
            (n, n)).fetchone()
        if not row:
            return None
        return {"user_id": row["user_id"], "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "allow_poke": bool(row["allow_poke"])}
    finally:
        con.close()
