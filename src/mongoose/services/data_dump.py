"""Data dump, on-demand exports of Mongoose's own data, addressable two ways.

WHAT THIS IS:
When the owner asks for "a data dump" (of their emails, tasks, notifications, diary, what Mongoose has
learned, a bug, or everything), this assembles a scoped export, zips it, and hands back BOTH:
  * a small download link  (best when you're at a screen), and
  * a dump id              (best when you're not, ask by voice now, pull it on the Mac later).
They address the SAME object: the dump id is the link's path component, so there is no duplication and no
second copy of the bytes.

THE DATA MODEL (in-batches vs out-batches, kept deliberately consistent):
  * IN-batches (data arriving) keep their existing ids: uploads -> asset_id (content SHA), bugs -> short
    id (b42), pokes -> poke_id. Content-addressed or random, because what matters is identity/dedupe.
  * OUT-batches (data leaving, i.e. dumps) get a dump_id that is TIME-ORDERED + short-random:
    'dump_YYYYMMDDTHHMMSS_<hex6>'. Time-ordered so a list of dumps sorts chronologically (you usually
    want the latest); short-random suffix so two dumps in the same second never collide; 'dump_' prefix
    so the id is self-describing wherever it appears (mirrors poke_/push_). The bytes themselves are
    stored once via the asset store (content-addressed), and the dump_registry row maps dump_id ->
    asset_id. So the human-facing handle is time-ordered and memorable-ish, while storage stays
    deduped and content-addressed underneath. Two ids, two purposes, one set of bytes.

PRIVACY BOUNDARY (this is a hard line, not a nicety):
A dump must not become a side channel around the system's scoping. So:
  * An OWNER dump exports owner-scoped data only. Guest-scoped private content (guest facts, guest chat,
    guest items) is NEVER included in an owner dump.
  * Encrypted guest journal content is never dumped in plaintext by anyone, see journal_privacy. A
    guest's own dump (if ever offered) would contain only what decrypts for that guest.
  * secrets.yaml, .env, TLS keys, tokens and the like are data Mongoose protects, not data it dumps;
    nothing here reads them.
"""
from __future__ import annotations

import io
import json
import hashlib
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import connect, utc_now_iso

# How long a dump's download link stays valid. Long enough to be useful across devices, short enough
# that a stale link in someone's history stops working. The dump itself persists; only the LINK expires.
_DUMP_LINK_TTL_HOURS = 24


# Dump kinds map to a scoped reader. "everything" is the union of the owner-safe kinds (never guest
# private content). Keep this list explicit so a new table is not silently swept into exports.
_OWNER_KINDS = ("emails", "tasks", "notifications", "diary", "facts", "bugs", "whatsapp", "events", "journal")


def _normalize_kind(kind: str | None) -> str:
    """Map the words a user actually says onto a canonical dump kind. 'support'/'all' both mean the full
    owner export here (we have no separate redaction tier because the kinds are already a non-secret
    allowlist); 'journal'/'wellness'/'mindfulness' -> the owner journal; others pass through if known."""
    k = (kind or "everything").strip().lower().replace(" ", "_")
    if k in {"journal", "journals", "wellness_journal", "mindfulness", "wellness", "checkins", "check_ins"}:
        return "journal"
    if k in {"bug", "bugs", "bug_report", "bug_reports"}:
        return "bugs"
    if k in {"everything", "full", "all", "complete", "support", "data"}:
        return "everything"
    return k


def _ensure_registry() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS dump_registry (
                dump_id     TEXT PRIMARY KEY,
                asset_id    TEXT NOT NULL,
                kind        TEXT NOT NULL,
                scope       TEXT NOT NULL DEFAULT 'owner',
                filename    TEXT NOT NULL,
                size_bytes  INTEGER NOT NULL DEFAULT 0,
                row_counts  TEXT NOT NULL DEFAULT '{}',
                created_at  TEXT NOT NULL,
                created_by  TEXT NOT NULL DEFAULT 'owner',
                token_hash  TEXT,
                expires_at  TEXT
            )
            """
        )
        # older DBs: add the link-token columns if missing (safe no-op when present)
        for col in ("token_hash TEXT", "expires_at TEXT"):
            try:
                con.execute(f"ALTER TABLE dump_registry ADD COLUMN {col}")
            except Exception:
                pass


def _new_dump_id() -> str:
    return ("dump_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "_" + secrets.token_hex(3))


def _new_download_token() -> str:
    """A high-entropy, URL-safe token. Returned to the user once (in the link); only its hash is stored,
    so the raw token can never be read back out of the DB or a dump."""
    return secrets.token_urlsafe(32)


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def refresh_download_token(dump_id: str) -> dict[str, Any] | None:
    """Mint a FRESH download token for an existing dump and return a usable link.

    The raw token exists only in the returned URL (the DB stores its hash), so the only way to hand
    out a working link after creation is to re-mint. The previous link stops working, which is the
    desired behaviour for a shareable link: each redeem produces one fresh, expiring URL."""
    rec = get_dump(dump_id)
    if not rec:
        return None
    token = _new_download_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=_DUMP_LINK_TTL_HOURS)).isoformat()
    with connect() as con:
        con.execute("UPDATE dump_registry SET token_hash=?, expires_at=? WHERE dump_id=?",
                    (_token_hash(token), expires_at, rec["dump_id"]))
        con.commit()
    return {"ok": True, "dump_id": rec["dump_id"], "filename": rec["filename"],
            "download_url": f"/dump/{rec['dump_id']}/download?token={token}",
            "expires_at": expires_at}


def validate_download_token(dump_id: str, token: str) -> bool:
    """Constant-time check that a supplied token matches the dump's stored hash AND has not expired.
    Used by the public download route so the bytes are served only to a holder of the real token."""
    if not dump_id or not token:
        return False
    _ensure_registry()
    with connect() as con:
        row = con.execute(
            "SELECT token_hash, expires_at FROM dump_registry WHERE dump_id=?", (dump_id,)).fetchone()
    if not row:
        return False
    stored = (dict(row).get("token_hash") or "")
    expires = (dict(row).get("expires_at") or "")
    if not stored or not expires:
        return False
    # expiry check (the dump persists; only the link expires)
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(expires):
            return False
    except Exception:
        return False
    return secrets.compare_digest(stored, _token_hash(token))


# ── scoped readers (each returns a list of plain dict rows, owner-scoped) ──────────────────────────
def _q(con, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _read_kind(con, kind: str) -> list[dict[str, Any]]:
    """Owner-scoped reader for one kind. Returns [] for anything unknown or empty. Guest-private content
    is structurally excluded: memory_facts is filtered to the owner scope, and guest tables are not read."""
    if kind == "emails":
        return _q(con, "SELECT id, message_id, sender, subject, date_sent, snippet FROM email_messages ORDER BY date_sent DESC")
    if kind == "tasks":
        return _q(con, "SELECT * FROM tasks ORDER BY rowid DESC")
    if kind == "notifications":
        return _q(con, "SELECT * FROM notifications ORDER BY rowid DESC")
    if kind == "diary":
        return _q(con, "SELECT at, kind, summary FROM diary_entries ORDER BY at DESC")
    if kind == "facts":
        # OWNER facts only , never guest-scoped facts (those stay in their guest scope).
        return _q(con, "SELECT fact, source, scope, created_at FROM memory_facts WHERE scope='owner' ORDER BY created_at DESC")
    if kind == "bugs":
        return _q(con, "SELECT * FROM bug_reports ORDER BY created_at DESC")
    if kind == "whatsapp":
        return _q(con, "SELECT id, chat, sender, ts, body FROM whatsapp_messages ORDER BY ts DESC")
    if kind == "events":
        return _q(con, "SELECT * FROM events ORDER BY rowid DESC")
    if kind == "journal":
        # The OWNER's own wellness/self-journal entries (bs_journal). Guest journals are encrypted and
        # are never included here , they live in guest_journal under per-guest keys.
        return _q(con, "SELECT id, at, mode, body_signal, body_texture, emotion_words, need, "
                       "next_step, text, source FROM bs_journal ORDER BY at DESC")
    return []


def available_kinds() -> list[str]:
    return list(_OWNER_KINDS) + ["everything"]


# Secret-key parts: any column whose name contains one of these is redacted as a safety net, even though
# the curated kinds above already avoid secret-bearing tables. Belt and suspenders (adapted from the
# parallel data-dump implementation): never let a token/password slip into an export.
_SECRET_KEY_PARTS = ("token", "secret", "password", "api_key", "apikey", "authorization",
                     "cookie", "credential", "private_key", "passwd", "auth", "refresh_token",
                     "client_secret", "access_token", ".pem", "vapid")


# Private journal free-text columns. In a BROAD dump (everything) these are redacted so reflective
# content does not leak through a general export; only an explicit `journal` dump includes the wording.
# This treats the journal as more sensitive than a task list, which it is.
_PRIVATE_JOURNAL_TEXT_COLS = ("body_signal", "body_texture", "text", "summary", "response_summary",
                              "entry_text", "need", "next_step", "emotion_words", "note")


def _redact_value(key: str, value: Any) -> Any:
    low = str(key or "").lower()
    if any(part in low for part in _SECRET_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…[truncated]"
    return value


def _redact_private_journal_text(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep journal metadata (timestamps, mode) but redact the private free-text columns, so a broad
    export shows that journaling happened without exposing what was written."""
    out = []
    for row in rows:
        r = dict(row)
        for col in _PRIVATE_JOURNAL_TEXT_COLS:
            if col in r and r[col] not in (None, "", [], 0):
                r[col] = "[private journal text redacted; ask for a journal dump to include it]"
        out.append(r)
    return out


def _redact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _redact_value(k, v) for k, v in row.items()} for row in rows]


def _collect(kinds: list[str], *, journal_specific: bool = False) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    with connect() as con:
        for k in kinds:
            rows = _redact_rows(_read_kind(con, k))
            # In a broad dump, redact the private journal wording (metadata stays). An explicit journal
            # dump (journal_specific) keeps the full text the user asked for.
            if k == "journal" and not journal_specific:
                rows = _redact_private_journal_text(rows)
            out[k] = rows
    return out


def create_dump(kind: str = "everything", *, created_by: str = "owner") -> dict[str, Any]:
    """Build a scoped export, store it once via the asset store, and register a retrievable dump_id.
    Returns {ok, dump_id, asset_id, filename, download_url, row_counts, redeem_hint}. Owner-scoped:
    contains no guest-private content."""
    kind = _normalize_kind(kind)
    if kind == "everything":
        kinds = list(_OWNER_KINDS)
    elif kind in _OWNER_KINDS:
        kinds = [kind]
    else:
        return {"ok": False, "error": f"unknown dump kind '{kind}'. options: {', '.join(available_kinds())}"}

    data = _collect(kinds, journal_specific=(kind == "journal"))
    row_counts = {k: len(v) for k, v in data.items()}

    manifest = {
        "mongoose_dump": True,
        "kind": kind,
        "scope": "owner",
        "created_at": utc_now_iso(),
        "row_counts": row_counts,
        "note": "Owner-scoped export. Contains no guest-private content. Generated by Mongoose.",
    }

    # zip it: one manifest.json + one <kind>.json per kind
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False, default=str))
        for k, rows in data.items():
            zf.writestr(f"{k}.json", json.dumps(rows, indent=2, ensure_ascii=False, default=str))
    payload = buf.getvalue()

    dump_id = _new_dump_id()
    filename = f"{dump_id}.zip"

    # store the bytes via the asset store (content-addressed, deduped, already has a download path)
    from . import assets as _assets
    rec = _assets.store_asset(payload, filename, uploaded_by=created_by,
                              source_surface="data_dump", mime_type="application/zip",
                              note=f"Mongoose data dump ({kind})")
    if not rec.get("ok"):
        return {"ok": False, "error": rec.get("error", "could not store dump")}
    asset_id = rec["asset_id"]

    token = _new_download_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=_DUMP_LINK_TTL_HOURS)).isoformat()

    _ensure_registry()
    with connect() as con:
        con.execute(
            "INSERT INTO dump_registry(dump_id, asset_id, kind, scope, filename, size_bytes, row_counts, created_at, created_by, token_hash, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (dump_id, asset_id, kind, "owner", filename, len(payload),
             json.dumps(row_counts), utc_now_iso(), created_by, _token_hash(token), expires_at),
        )

    return {
        "ok": True,
        "dump_id": dump_id,
        "asset_id": asset_id,
        "filename": filename,
        "size_bytes": len(payload),
        "row_counts": row_counts,
        # A usable link: carries the one-time token so it works from a browser or another device,
        # and expires on its own. The nonce-gated cockpit path still works too.
        "download_url": f"/dump/{dump_id}/download?token={token}",
        "expires_at": expires_at,
        "redeem_hint": f"Open the link to download (valid {_DUMP_LINK_TTL_HOURS}h), or paste the dump id ({dump_id}) into the Build/Staging area to fetch it there.",
    }


def get_dump(dump_id: str) -> dict[str, Any] | None:
    """Look up a dump by id. Returns the registry row (with asset_id + stored path), or None."""
    _ensure_registry()
    with connect() as con:
        row = con.execute("SELECT * FROM dump_registry WHERE dump_id=?", ((dump_id or "").strip(),)).fetchone()
    if not row:
        return None
    rec = dict(row)
    try:
        rec["row_counts"] = json.loads(rec.get("row_counts") or "{}")
    except Exception:
        rec["row_counts"] = {}
    return rec


def dump_bytes(dump_id: str) -> tuple[bytes, str] | None:
    """Return (bytes, filename) for a dump id by resolving through the asset store, or None if missing."""
    rec = get_dump(dump_id)
    if not rec:
        return None
    from . import assets as _assets
    p = _assets.asset_path(rec["asset_id"])
    if p is None or not p.exists():
        return None
    return p.read_bytes(), rec["filename"]


def list_dumps(limit: int = 30) -> dict[str, Any]:
    _ensure_registry()
    with connect() as con:
        rows = con.execute(
            "SELECT dump_id, kind, filename, size_bytes, created_at FROM dump_registry "
            "ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    return {"ok": True, "dumps": [dict(r) for r in rows]}
