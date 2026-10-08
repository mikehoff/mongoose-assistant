"""Owner pairing secret: proves the first device to pair is actually Alex, not a guest or a random
device on the network.

The problem it closes: the first-run install QR minted a `primary_phone` (owner) pairing code with no
identity check, so whoever scanned first claimed the owner slot. On a shared network that's a real
hole: a guest's phone (or anyone who opened the /install page) could become the owner.

The fix: at first run Mongoose mints a short secret that is shown ONLY where someone with physical
access to the Mac can see it: the install terminal and the loopback /install page. Claiming the OWNER
slot requires presenting this secret. A remote/guest device that scans the QR can still pair, but only
as a guest; it cannot take the owner slot without the secret from the Mac's screen.

The secret is single-purpose and short-lived: it exists only until the owner device is paired, then
it's consumed (the owner slot is full, so it's irrelevant). Stored in the data dir, chmod 600.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from .settings import get_settings

_SECRET_FILE = "owner_pairing_secret"
# Human-typable: 6 uppercase letters/digits, unambiguous (no 0/O/1/I).
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _secret_path() -> Path:
    return get_settings().data_dir / _SECRET_FILE


def _mint() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(6))


def get_or_create_owner_secret() -> str:
    """The current owner-pairing secret, minting+persisting one if absent. Shown only on the Mac."""
    p = _secret_path()
    try:
        if p.exists():
            existing = p.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    secret = _mint()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secret, encoding="utf-8")
        p.chmod(0o600)
    except OSError:
        # If we can't persist it, still return the value for this process so the flow works;
        # a restart will mint a new one (acceptable for a first-run, pre-owner secret).
        pass
    return secret


def verify_owner_secret(candidate: str) -> bool:
    """Constant-time check of a presented secret against the stored one. Empty/avbsent -> False."""
    candidate = (candidate or "").strip().upper()
    if not candidate:
        return False
    try:
        stored = _secret_path().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not stored:
        return False
    return secrets.compare_digest(candidate, stored)


def consume_owner_secret() -> None:
    """Remove the secret once the owner device is paired: it has served its purpose and shouldn't
    linger on disk."""
    try:
        _secret_path().unlink()
    except OSError:
        pass


def owner_secret_active() -> bool:
    """True if an owner secret currently exists on disk (i.e. the owner slot isn't claimed yet)."""
    try:
        return _secret_path().exists() and bool(_secret_path().read_text(encoding="utf-8").strip())
    except OSError:
        return False
