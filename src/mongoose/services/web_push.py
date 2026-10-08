"""Web Push notifications. This is what makes Mongoose feel like a real app: an installed PWA on a
phone receives push notifications even when closed, with NO third-party app (unlike ntfy). It uses
the standard Web Push protocol with VAPID, implemented natively on the `cryptography` library so we
don't need pywebpush.

How it works:
  1. We generate a VAPID keypair once (EC P-256), stored in app_state. The public key is handed to
     the browser; the private key signs each push so the push service trusts us.
  2. The browser's service worker subscribes via PushManager and sends us its subscription (an
     endpoint URL + two keys: p256dh and auth). We store it in push_subscriptions.
  3. To notify, we encrypt the payload (aes128gcm per RFC 8291), sign a VAPID JWT, and POST to the
     subscription's endpoint. The push service wakes the SW, which shows the notification.

Web Push REQUIRES https on the page that subscribes (secure context), so this depends on TLS being
set up first. On http it simply won't have any subscriptions, and we fall back to ntfy.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import time
from typing import Any, Optional

from ..db import connect

# app_state keys for the VAPID keypair
_VAPID_PRIV = "vapid_private_key_pem"
_VAPID_PUB_RAW = "vapid_public_key_raw_b64"  # uncompressed P-256 point, urlsafe-b64, for the browser
_MAX_CONSECUTIVE_FAILURES = 5


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ── tables ───────────────────────────────────────────────────────────────────────────────────

def ensure_table() -> None:
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                sub_id       TEXT PRIMARY KEY,
                endpoint     TEXT NOT NULL UNIQUE,
                p256dh       TEXT NOT NULL,
                auth         TEXT NOT NULL,
                user_id      TEXT,
                device_id    TEXT,
                device_profile TEXT,
                device_label TEXT,
                created_at   TEXT NOT NULL,
                last_ok_at   TEXT,
                fail_count   INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Additive migration for users updating from an earlier PWA build. CREATE TABLE IF NOT
        # EXISTS does not add columns to an existing table, so the account/profile targeting patch
        # would otherwise work in tests on a fresh DB but fail the day after a real update with
        # "no such column: user_id/device_profile". Keep this local to the owning service because
        # this table is optional and created lazily when push is first used.
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(push_subscriptions)")}
            for column, definition in (
                ("user_id", "TEXT"),
                ("device_id", "TEXT"),
                ("device_profile", "TEXT"),
                ("device_label", "TEXT"),
                ("last_ok_at", "TEXT"),
                ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in cols:
                    con.execute(f"ALTER TABLE push_subscriptions ADD COLUMN {column} {definition}")
        except Exception:
            # A broken push-subscription schema should not stop the whole app booting; callers will
            # surface a clear subscription/send failure. Fresh DBs and normal migrations take this path.
            pass


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── VAPID keypair ──────────────────────────────────────────────────────────────────────────────

def _get_app_state(key: str) -> Optional[str]:
    from ..db import get_app_state
    try:
        return get_app_state(key)
    except Exception:
        return None


def _set_app_state(key: str, value: str) -> None:
    from ..db import set_app_state
    set_app_state(key, value)


def ensure_vapid_keys() -> dict[str, Any]:
    """Generate the VAPID keypair once and cache it. Returns {public_key} (the browser-facing key)."""
    priv_pem = _get_app_state(_VAPID_PRIV)
    pub_raw = _get_app_state(_VAPID_PUB_RAW)
    if priv_pem and pub_raw:
        return {"ok": True, "public_key": pub_raw}
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
    except Exception as exc:
        return {"ok": False, "error": f"cryptography unavailable: {exc}"}

    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    # Uncompressed public point (0x04 || X || Y), 65 bytes, urlsafe-b64 for the browser.
    pub_point = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_raw = _b64url_encode(pub_point)
    _set_app_state(_VAPID_PRIV, priv_pem)
    _set_app_state(_VAPID_PUB_RAW, pub_raw)
    return {"ok": True, "public_key": pub_raw}


def public_key() -> Optional[str]:
    r = ensure_vapid_keys()
    return r.get("public_key") if r.get("ok") else None


def _load_private_key():
    from cryptography.hazmat.primitives import serialization
    priv_pem = _get_app_state(_VAPID_PRIV)
    if not priv_pem:
        return None
    return serialization.load_pem_private_key(priv_pem.encode("ascii"), password=None)


# ── subscriptions ──────────────────────────────────────────────────────────────────────────────

def save_subscription(
    subscription: dict,
    *,
    user_id: str = "",
    device_label: str = "",
    device_id: str = "",
    device_profile: str = "",
) -> dict[str, Any]:
    """Store a PushSubscription from the browser. subscription = {endpoint, keys:{p256dh, auth}}.

    Account and device metadata are deliberately stored with the subscription. After a day or two
    of multi-device use, "broadcast to every phone" stops being delightful: owner reminders should
    not pop on a guest PWA, and personal social messages should not leak to shared devices.
    """
    ensure_table()
    endpoint = subscription.get("endpoint", "")
    keys = subscription.get("keys", {}) or {}
    p256dh = keys.get("p256dh", "")
    auth = keys.get("auth", "")
    if not (endpoint and p256dh and auth):
        return {"ok": False, "error": "incomplete subscription"}
    import hashlib
    sub_id = "push_" + hashlib.sha256(endpoint.encode()).hexdigest()[:16]
    with connect() as con:
        con.execute(
            "INSERT INTO push_subscriptions "
            "(sub_id, endpoint, p256dh, auth, user_id, device_id, device_profile, device_label, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, "
            "  user_id=COALESCE(excluded.user_id, push_subscriptions.user_id), "
            "  device_id=COALESCE(excluded.device_id, push_subscriptions.device_id), "
            "  device_profile=COALESCE(excluded.device_profile, push_subscriptions.device_profile), "
            "  device_label=COALESCE(excluded.device_label, push_subscriptions.device_label), "
            "  fail_count=0",
            (sub_id, endpoint, p256dh, auth, user_id or None, device_id or None,
             device_profile or None, device_label or None, _now_iso()))
    return {"ok": True, "sub_id": sub_id}


def bind_device_subscriptions_to_user(device_id: str, user_id: str) -> int:
    """Link a device's Web Push subscriptions to a user account, so account-targeted
    notifications (pokes) actually reach that phone. A subscription created before the person
    logged in has user_id NULL and is unreachable by user_id, which is exactly why a poke landed
    with no notification. Called on login. Returns the number of subscriptions bound."""
    if not device_id or not user_id:
        return 0
    ensure_table()
    with connect() as con:
        cur = con.execute("UPDATE push_subscriptions SET user_id=? WHERE device_id=?",
                          (str(user_id), str(device_id)))
        con.commit()
        return cur.rowcount


def remove_subscription(endpoint: str) -> dict[str, Any]:
    ensure_table()
    with connect() as con:
        con.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    return {"ok": True}


def owners_for_endpoint(endpoint: str) -> list[dict[str, str]]:
    """Every stored subscription for this endpoint, with its owner identity, REGARDLESS of scope. The
    /push/unsubscribe authorization check MUST use this, not list_subscriptions: list_subscriptions only
    returns unscoped/owner-profile subs, so it cannot see a guest's or another device's subscription,
    which let the ownership check be bypassed and any caller delete anyone else's subscription by
    endpoint (external audit finding #7, regressed)."""
    ensure_table()
    with connect() as con:
        rows = con.execute(
            "SELECT user_id, device_id FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchall()
    return [{"user_id": (r[0] or ""), "device_id": (r[1] or "")} for r in rows]


def _mark_subscription_failure(endpoint: str, *, terminal: bool = False) -> bool:
    """Record a failed push delivery and prune endpoints that are permanently dead or repeatedly failing.

    Browser/PWA push endpoints churn in real life: users reinstall the PWA, browsers rotate data, phones
    leave the network. Without pruning, a 2-3 week install accumulates dead subscriptions and every
    reminder becomes slow/noisy. 404/410 is terminal; other failures are pruned after several consecutive
    failures and reset to zero on a success.
    """
    ensure_table()
    if terminal:
        remove_subscription(endpoint)
        return True
    with connect() as con:
        con.execute("UPDATE push_subscriptions SET fail_count=fail_count+1 WHERE endpoint=?", (endpoint,))
        row = con.execute("SELECT fail_count FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
        if row and int(row["fail_count"] or 0) >= _MAX_CONSECUTIVE_FAILURES:
            con.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
            return True
    return False


def prune_dead_subscriptions(*, max_fail_count: int = _MAX_CONSECUTIVE_FAILURES) -> dict[str, int]:
    """Remove subscriptions already over the consecutive-failure threshold. Safe maintenance helper."""
    ensure_table()
    with connect() as con:
        before = int(con.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0])
        con.execute("DELETE FROM push_subscriptions WHERE fail_count >= ?", (max(1, int(max_fail_count)),))
        after = int(con.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0])
    return {"pruned": before - after, "remaining": after}


# ── test / status tri-state ────────────────────────────────────────────────────────────────────
# These three signals are deliberately separate so the UI never conflates them (the green-test /
# still-says-not-set confusion): whether the SERVER can send push at all, how many devices are
# actually subscribed, and whether a test push has genuinely been DELIVERED. "Is THIS browser
# subscribed" is answered client-side, because only the browser knows its own PushManager state.
_LAST_TEST_OK_KEY = "push_last_test_ok_at"
_LAST_TEST_AT_KEY = "push_last_test_at"


def record_test_result(sent: int) -> None:
    """Remember when a push test last ran, and last actually reached a device."""
    now = _now_iso()
    _set_app_state(_LAST_TEST_AT_KEY, now)
    if int(sent or 0) > 0:
        _set_app_state(_LAST_TEST_OK_KEY, now)


def push_state_summary() -> dict[str, Any]:
    """Distinct push signals for the UI: settings_saved (VAPID keys exist, so the server can send at
    all), subscriptions (the currently-subscribed fleet), and whether/when a test push last succeeded."""
    return {
        "settings_saved": bool(public_key()),
        "subscriptions": subscription_count(),
        "last_test_ok_at": _get_app_state(_LAST_TEST_OK_KEY),
        "last_test_at": _get_app_state(_LAST_TEST_AT_KEY),
    }


def _profile_clause(device_profiles: tuple[str, ...] | list[str] | set[str] | None, *,
                    include_unscoped: bool) -> tuple[str, list[str]]:
    profiles = [str(p).strip() for p in (device_profiles or []) if str(p).strip()]
    if not profiles:
        return "", []
    placeholders = ",".join("?" for _ in profiles)
    clause = f"device_profile IN ({placeholders})"
    params = profiles
    if include_unscoped:
        # Legacy subscriptions created before device_profile existed are treated as owner/system
        # subscriptions for compatibility. New trusted_guest subscriptions are explicitly scoped and
        # will not match this fallback.
        clause = f"({clause} OR device_profile IS NULL OR device_profile='')"
    return clause, params


def list_subscriptions(
    *,
    user_id: str | None = None,
    device_profiles: tuple[str, ...] | list[str] | set[str] | None = None,
    device_ids: tuple[str, ...] | list[str] | set[str] | None = None,
    include_unscoped: bool = True,
) -> list[dict[str, Any]]:
    """Stored push subscriptions, optionally limited to one account user or device profile.

    `user_id` is account-scoped for truly personal social notifications. `device_profiles` is
    device-scoped for owner/system notifications such as alarms, so a trusted_guest PWA does not
    receive the owner's reminders merely because it was installed on the same Mongoose instance.
    """
    ensure_table()
    with connect() as con:
        clauses: list[str] = []
        params: list[str] = []
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        if device_ids:
            ids = [d for d in device_ids if d]
            if ids:
                clauses.append("device_id IN (%s)" % ",".join("?" * len(ids)))
                params.extend(ids)
            else:
                clauses.append("1=0")
        elif device_ids is not None:
            # an explicit empty target must match nobody, never fall through to broadcast
            clauses.append("1=0")
        prof_clause, prof_params = _profile_clause(device_profiles, include_unscoped=include_unscoped)
        if prof_clause and not device_ids and device_ids is None:
            clauses.append(prof_clause)
            params.extend(prof_params)
        # A PURE broadcast (no user, no device target, no profile) is an owner/system notification (an
        # alarm, a benchmark note, an ops alert). It must reach ONLY the owner: sending it to a guest is
        # a privacy leak (the guest sees the owner's internal notifications) AND it is exactly the
        # notification spam that got a guest's browser to block us. Filter by OWNER user_id rather than
        # device_profile, so a mis-scoped guest subscription (NULL profile, e.g. subscribed without a
        # device token) is still excluded. Guests get a push ONLY when their own user_id is targeted.
        if not user_id and device_ids is None and device_profiles is None:
            _owner_uid = ""
            try:
                from .auth_accounts import owner_account
                _owner_uid = (owner_account() or {}).get("user_id") or ""
            except Exception:
                _owner_uid = ""
            if _owner_uid:
                clauses.append("(user_id IS NULL OR user_id = ?)")  # owner + legacy unscoped only
                params.append(_owner_uid)
            else:
                clauses.append("(device_profile IS NULL OR device_profile != 'trusted_guest')")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = con.execute(
            "SELECT * FROM push_subscriptions" + where + " ORDER BY created_at DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def subscription_count(
    *,
    user_id: str | None = None,
    device_profiles: tuple[str, ...] | list[str] | set[str] | None = None,
    device_ids: tuple[str, ...] | list[str] | set[str] | None = None,
    include_unscoped: bool = True,
) -> int:
    ensure_table()
    with connect() as con:
        clauses: list[str] = []
        params: list[str] = []
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        if device_ids:
            ids = [d for d in device_ids if d]
            if ids:
                clauses.append("device_id IN (%s)" % ",".join("?" * len(ids)))
                params.extend(ids)
            else:
                clauses.append("1=0")
        elif device_ids is not None:
            clauses.append("1=0")
        prof_clause, prof_params = _profile_clause(device_profiles, include_unscoped=include_unscoped)
        if prof_clause and device_ids is None:
            clauses.append(prof_clause)
            params.extend(prof_params)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return int(con.execute("SELECT COUNT(*) FROM push_subscriptions" + where, params).fetchone()[0])


# ── RFC 8291 payload encryption (aes128gcm) ────────────────────────────────────────────────────

def _encrypt_payload(payload: bytes, p256dh_b64: str, auth_b64: str) -> bytes:
    """Encrypt `payload` for a subscription per RFC 8291 (Content-Encoding: aes128gcm)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    ua_public_bytes = _b64url_decode(p256dh_b64)   # client public key (uncompressed point)
    auth_secret = _b64url_decode(auth_b64)         # 16-byte auth secret

    # Our ephemeral keypair for this message
    server_priv = ec.generate_private_key(ec.SECP256R1())
    server_pub_bytes = server_priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_bytes)
    shared = server_priv.exchange(ec.ECDH(), ua_public)

    # PRK_key = HKDF(auth_secret, shared, "WebPush: info" || 0x00 || ua_public || server_public)
    key_info = b"WebPush: info\x00" + ua_public_bytes + server_pub_bytes
    prk_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
                   info=key_info).derive(shared)

    salt = os.urandom(16)
    # CEK and nonce derived from prk_key + salt
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(prk_key)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(prk_key)

    # aes128gcm content: pad with 0x02 delimiter then encrypt
    aesgcm = AESGCM(cek)
    ciphertext = aesgcm.encrypt(nonce, payload + b"\x02", None)

    # Header: salt(16) || record_size(4, uint32 BE) || idlen(1) || keyid(server_pub_bytes)
    record_size = 4096
    header = salt + struct.pack("!I", record_size) + struct.pack("!B", len(server_pub_bytes)) + server_pub_bytes
    return header + ciphertext


def _default_vapid_subject() -> str:
    """A VAPID `sub` that push services actually accept. Apple's push service REJECTS a mailto at a
    reserved/non-routable domain such as *.local with 403 BadJwtToken (Google FCM is lenient, which is
    why Android pushes worked while every iPhone push silently failed). So we NEVER default to .local:
    prefer a mailto at the configured public domain, and fall back to a valid public-TLD placeholder.
    Override with the VAPID_SUBJECT env var (a mailto: or https: URL)."""
    try:
        from urllib.parse import urlparse
        from .remote_access import public_base_url
        base = (public_base_url() or "").strip()
        host = urlparse(base).netloc if base.startswith("https://") else ""
        if host and not host.endswith(".local"):
            return f"mailto:admin@{host}"
    except Exception:
        pass
    return "mailto:admin@mongoose.app"


def _vapid_auth_header(endpoint: str) -> dict[str, str]:
    """Build the VAPID Authorization header (a signed JWT) for the push endpoint's origin."""
    from urllib.parse import urlparse
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils

    priv = _load_private_key()
    if priv is None:
        return {}

    parsed = urlparse(endpoint)
    aud = f"{parsed.scheme}://{parsed.netloc}"
    sub = os.getenv("VAPID_SUBJECT") or _default_vapid_subject()

    header = {"typ": "JWT", "alg": "ES256"}
    claims = {"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": sub}
    signing_input = (_b64url_encode(json.dumps(header, separators=(",", ":")).encode()) + "." +
                     _b64url_encode(json.dumps(claims, separators=(",", ":")).encode()))

    der_sig = priv.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    # Convert DER signature to raw r||s (64 bytes) as JWT/JWS requires
    r, s = asym_utils.decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    jwt = signing_input + "." + _b64url_encode(raw_sig)

    pub_raw = _get_app_state(_VAPID_PUB_RAW) or ""
    return {"Authorization": f"vapid t={jwt}, k={pub_raw}"}


# ── send ───────────────────────────────────────────────────────────────────────────────────────

async def send_web_push(title: str, body: str = "", *, url: str = "/device/launchpad",
                        tag: str = "", icon: str = "/static/img/mongoose-192.png",
                        user_id: str | None = None,
                        device_profiles: tuple[str, ...] | list[str] | set[str] | None = None,
                        device_ids: tuple[str, ...] | list[str] | set[str] | None = None,
                        include_unscoped: bool = True) -> dict[str, Any]:
    """Send a Web Push notification.

    By default this broadcasts to every stored subscription (legacy system alert behaviour). When
    `user_id` is supplied it sends only to that account user; when `device_profiles` is supplied it
    sends only to those device profiles, with legacy unscoped owner subscriptions included by
    default for upgrade compatibility.
    """
    ensure_table()
    subs = list_subscriptions(user_id=user_id, device_profiles=device_profiles,
                              device_ids=device_ids, include_unscoped=include_unscoped)
    if not subs:
        targeted = user_id or device_profiles or device_ids
        return {"sent": 0, "reason": "no_target_subscriptions" if targeted else "no_subscriptions"}
    if _load_private_key() is None:
        return {"sent": 0, "reason": "no_vapid_keys"}

    payload = json.dumps({"title": title or "Mongoose", "body": body or "",
                          "url": url, "tag": tag or "", "icon": icon}).encode("utf-8")

    from .egress_guard import egress_allowed
    if not egress_allowed("web_push")[0]:
        return {"sent": 0, "reason": "egress_blocked_non_live"}
    import httpx
    sent = 0
    pruned = 0
    failed = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for sub in subs:
            try:
                encrypted = _encrypt_payload(payload, sub["p256dh"], sub["auth"])
                headers = {
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": "86400",
                    "Urgency": "normal",
                }
                headers.update(_vapid_auth_header(sub["endpoint"]))
                resp = await client.post(sub["endpoint"], content=encrypted, headers=headers)
                if resp.status_code in (404, 410):
                    if _mark_subscription_failure(sub["endpoint"], terminal=True):
                        pruned += 1
                elif resp.status_code < 300:
                    sent += 1
                    with connect() as con:
                        con.execute("UPDATE push_subscriptions SET last_ok_at=?, fail_count=0 WHERE endpoint=?",
                                    (_now_iso(), sub["endpoint"]))
                else:
                    failed += 1
                    if _mark_subscription_failure(sub["endpoint"]):
                        pruned += 1
            except Exception:
                failed += 1
                if _mark_subscription_failure(sub["endpoint"]):
                    pruned += 1
    return {"sent": sent, "pruned": pruned, "failed": failed, "total": len(subs)}
