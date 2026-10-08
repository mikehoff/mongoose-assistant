"""Journal privacy, two planes, so a guest's private words stay private AND Mongoose still grows familiar.

THE TENSION (named, because the obvious reading of the request is self-contradictory):
"Encrypt a guest's journal so only they can read it back" and "let the model keep learning about the
guest" cannot both be true of the SAME bytes: if the content is encrypted such that the model can't read
it, the model can't learn from it. So we split the data into two planes:

  PLANE 1, VERBATIM (private).   The full journal text the guest wrote. For a GUEST this is encrypted at
                                  rest with a key derived per-guest, so it is not sitting in plaintext in
                                  the SQLite file and one guest can never read another's entries. It is
                                  decrypted only to show that guest their own entry back.

  PLANE 2, LEARNED FACTS (durable, non-sensitive).  At write time, before the verbatim is sealed, we
                                  distil a few short, non-sensitive facts ("visiting from Canada",
                                  "gets anxious before interviews") into the guest's OWN memory scope
                                  (memory.scope_for_conversation -> 'guest:<device_id>'). The profile
                                  grows, so the model becomes more familiar with each participant over
                                  time, WITHOUT keeping a readable transcript of private reflections.

This mirrors how a thoughtful human confidant works: you remember the gist that matters about a friend,
you don't keep a literal recording of their private words.

HONEST LIMITATION (do not let anyone mistake this for more than it is):
This is at-rest encryption with a key the HOST can derive. It protects against casual inspection of the
DB, against one guest reading another's entries, and against the verbatim showing up in dumps/exports. It
is NOT end-to-end encryption against the host itself: a single-Mac local system has to hold the key to
show the guest their entry, so the running process can decrypt. True host-blind privacy would require the
guest's device to hold the key, which this architecture does not (yet) support. We call this "light
encryption" deliberately, and the owner's own journal is not encrypted at all (it is the owner's data on
the owner's machine).
"""
from __future__ import annotations

import base64
from typing import Any

from ..db import connect, utc_now_iso


# ── master secret + per-guest key derivation ──────────────────────────────────────────────────────
def _secret_path():
    """Legacy on-disk key location. Retained only so an existing key can be migrated into the DB once;
    new installs never create this file."""
    from pathlib import Path
    from ..settings import get_settings
    d = get_settings().data_dir / "keys"
    return Path(d) / "journal_master.key"


_APP_STATE_KEY = "journal_master_secret_b64"


def _master_secret() -> bytes:
    """A 32-byte master secret stored IN the database (app_state), so it is co-located with the
    ciphertext it protects: isolated together per test, backed up together, atomic together. This avoids
    the failure mode where a separate key FILE drifts from the DB (or a concurrent app startup clobbers
    it), which would make existing entries undecryptable. Per-guest keys are derived from this secret; it
    never leaves the machine and is never included in a dump. A one-time migration imports a pre-existing
    on-disk key so entries encrypted before this change keep decrypting."""
    import base64 as _b64
    from ..db import get_app_state, set_app_state

    raw = get_app_state(_APP_STATE_KEY, "")
    if raw:
        try:
            secret = _b64.b64decode(raw)
            if len(secret) >= 32:
                return secret[:32]
        except Exception:
            pass

    # one-time migration: if a legacy key file exists, adopt its bytes so old ciphertext still decrypts
    legacy = _secret_path()
    try:
        if legacy.exists():
            data = legacy.read_bytes().strip()
            if len(data) >= 32:
                secret = data[:32]
                set_app_state(_APP_STATE_KEY, _b64.b64encode(secret).decode("ascii"))
                return secret
    except Exception:
        pass

    # fresh secret, persisted in the DB
    import secrets as _s
    secret = _s.token_bytes(32)
    try:
        set_app_state(_APP_STATE_KEY, _b64.b64encode(secret).decode("ascii"))
    except Exception:
        pass
    return secret


def _guest_fernet(device_id: str):
    """Derive a Fernet key for one guest from the master secret + their device id (HKDF). Two guests get
    independent keys, so one guest's key cannot decrypt another's entries. Returns a Fernet instance, or
    None if the crypto library is unavailable (callers then fall back to NOT storing verbatim)."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.fernet import Fernet
    except Exception:
        return None
    salt = (device_id or "unknown").encode("utf-8")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"mongoose-guest-journal-v1")
    key = hkdf.derive(_master_secret())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_for_guest(device_id: str, plaintext: str) -> str | None:
    """Encrypt verbatim journal text for a guest. Returns an opaque token string, or None if encryption
    is unavailable (the caller must then NOT persist the plaintext, privacy beats persistence)."""
    f = _guest_fernet(device_id)
    if f is None:
        return None
    try:
        return f.encrypt((plaintext or "").encode("utf-8")).decode("ascii")
    except Exception:
        return None


def decrypt_for_guest(device_id: str, token: str) -> str | None:
    """Decrypt a guest's own entry back to them. Returns the plaintext, or None if the token is not
    theirs / unreadable (a different guest's device_id derives a different key and simply fails here)."""
    f = _guest_fernet(device_id)
    if f is None or not token:
        return None
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        return None


# ── storage: a guest journal table whose verbatim column is ciphertext ─────────────────────────────
def _ensure_table() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_journal (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scope        TEXT NOT NULL,            -- 'guest:<device_id>'
                device_id    TEXT NOT NULL,
                at           TEXT NOT NULL,            -- timestamp (this is the "timestamped" requirement)
                mode         TEXT NOT NULL DEFAULT 'journal',
                ciphertext   TEXT,                    -- Fernet token of the verbatim text (NULL if crypto absent)
                has_text     INTEGER NOT NULL DEFAULT 0,
                fact_count   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_guest_journal_scope ON guest_journal(scope, at)")


def _split_facts(text: str) -> list[str]:
    """Extract a few SHORT, non-sensitive facts worth remembering about the participant from a journal
    entry, deterministically (no model needed). Conservative on purpose: we would rather learn a little
    reliably than over-extract sensitive detail. The model-assisted path can enrich this elsewhere; here
    we keep it to safe, surface signals (interests/activities phrased generically)."""
    out: list[str] = []
    low = (text or "").lower()
    # very light, safe signals , presence of a topic, not the private content around it
    cues = {
        "music": "enjoys music",
        "guitar": "plays or likes guitar",
        "running": "runs / into running",
        "swim": "swims / into swimming",
        "coffee": "is a coffee person",
        "tea": "is a tea person",
        "cooking": "enjoys cooking",
        "garden": "into gardening",
        "reading": "enjoys reading",
        "travel": "into travel",
        "yoga": "does yoga",
        "cycling": "into cycling",
    }
    for cue, fact in cues.items():
        if cue in low and fact not in out:
            out.append(fact)
    return out[:4]


def record_guest_journal(device_id: str, text: str, *, mode: str = "journal",
                         learn: bool = True) -> dict[str, Any]:
    """Persist a GUEST journal entry: timestamped, with the verbatim ENCRYPTED at rest, and (if learn)
    a few non-sensitive facts distilled into the guest's own memory scope so the model grows familiar.

    Returns {ok, id, at, encrypted, facts_learned}. If encryption is unavailable, the verbatim is NOT
    stored in plaintext (has_text=0, ciphertext NULL); the facts may still be learned so familiarity is
    preserved even when at-rest sealing cannot be."""
    _ensure_table()
    scope = f"guest:{device_id}" if not str(device_id).startswith("guest:") else str(device_id)
    dev = scope.split("guest:", 1)[-1]
    now = utc_now_iso()
    token = encrypt_for_guest(dev, text)
    has_text = 1 if token else 0

    facts_learned = 0
    if learn:
        facts = _split_facts(text)
        if facts:
            try:
                from .memory import remember_fact
                for fct in facts:
                    remember_fact(fct, source="guest_journal", scope=scope)
                facts_learned = len(facts)
            except Exception:
                facts_learned = 0

    with connect() as con:
        cur = con.execute(
            "INSERT INTO guest_journal(scope, device_id, at, mode, ciphertext, has_text, fact_count, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (scope, dev, now, mode, token, has_text, facts_learned, now),
        )
        jid = cur.lastrowid
    return {"ok": True, "id": jid, "at": now, "encrypted": bool(token),
            "facts_learned": facts_learned,
            "note": ("verbatim sealed at rest; only readable back to this guest" if token
                     else "verbatim not stored (encryption unavailable); learned facts kept")}


def read_guest_journal(device_id: str, *, limit: int = 30) -> dict[str, Any]:
    """Return a guest's OWN entries, decrypted back to them. Only entries under this device's scope are
    returned, and they decrypt only with this device's derived key, so a guest sees their own and nobody
    else's. Owner code does not call this to read content; it is the guest reading themselves back."""
    _ensure_table()
    dev = str(device_id).split("guest:", 1)[-1]
    scope = f"guest:{dev}"
    with connect() as con:
        rows = con.execute(
            "SELECT id, at, mode, ciphertext, has_text, fact_count FROM guest_journal "
            "WHERE scope=? ORDER BY at DESC LIMIT ?", (scope, max(1, min(limit, 200)))).fetchall()
    entries = []
    for r in rows:
        d = dict(r)
        text = decrypt_for_guest(dev, d.pop("ciphertext")) if d.get("has_text") else None
        d["text"] = text if text is not None else ("(could not decrypt)" if d.get("has_text") else "")
        entries.append(d)
    return {"ok": True, "scope": scope, "entries": entries}


def guest_journal_summary(device_id: str) -> dict[str, Any]:
    """Privacy-safe counts for a guest, WITHOUT returning any verbatim content. Suitable for an
    owner-facing 'is the guest space working' view: how many entries, how many facts learned, never the
    words themselves."""
    _ensure_table()
    dev = str(device_id).split("guest:", 1)[-1]
    scope = f"guest:{dev}"
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(fact_count),0) AS facts, MAX(at) AS last_at "
            "FROM guest_journal WHERE scope=?", (scope,)).fetchone()
    return {"ok": True, "scope": scope, "entry_count": int(row["n"] or 0),
            "facts_learned": int(row["facts"] or 0), "last_entry_at": row["last_at"],
            "note": "counts only; journal content is private to the guest and not returned here"}
