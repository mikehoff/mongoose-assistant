from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import audit, connect, safe_fts_query, utc_now_iso
from ..settings import get_settings

# A file-based message bus for coordinating Mongoose, Claude Code, HA Assist, and
# the human. Owned by Mongoose on the Mac mini. Design goals:
#   - human-readable + hand-editable (Markdown + YAML frontmatter), like the HA
#     append-only inbox pattern;
#   - safe for multiple concurrent writers WITHOUT locks: one immutable file per
#     message, written atomically (temp file + rename), never edited in place;
#   - indexed into SQLite/FTS so Mongoose can search and summarise.
#
# Layout (under <data_dir>/bus/):
#   inbox/    unread messages, one .md file each
#   archive/  messages that have been read/processed
#   files/    shared attachments (the "network share" surface)
#
# A message file name is "<id>.md". The id is time-ordered so a directory
# listing is naturally chronological.

VALID_TYPES = {"brief", "task", "alert", "reply", "note"}
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
# Message ids are generated internally in this exact shape; validate before using
# one as a filename component so a caller-supplied id can't traverse paths.
_MESSAGE_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[a-f0-9]{6}$")


def _safe_meta_value(v: Any) -> str:
    """Frontmatter values must be single-line: reject newlines so a field can't
    inject extra YAML keys or corrupt the parser."""
    s = str(v)
    if "\n" in s or "\r" in s:
        raise ValueError("frontmatter values must be single-line")
    return s


def _bus_root() -> Path:
    return get_settings().data_dir / "bus"


def _dir(name: str) -> Path:
    p = _bus_root() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_message_id() -> str:
    """Time-ordered, collision-resistant id: ISO-ish prefix + short random."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file then os.replace (atomic on the same filesystem), so
    a concurrent reader never sees a half-written message."""
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _render(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            inner = ", ".join(_safe_meta_value(x) for x in v)
            lines.append(f"{k}: [{inner}]")
        else:
            lines.append(f"{k}: {_safe_meta_value(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n" + (body or "").rstrip() + "\n"


def _parse(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
        else:
            meta[k] = v
    return meta, body.strip()


def send_message(*, sender: str, to: str, subject: str, body: str = "",
                 type: str = "note", re_id: str | None = None,
                 attachments: list[str] | None = None) -> dict[str, Any]:
    """Drop a new message into the inbox. Returns the message metadata.

    Immutable: each call writes a brand-new file, never edits an existing one,
    so concurrent senders cannot collide.
    """
    if type not in VALID_TYPES:
        return {"ok": False, "error": f"type must be one of {sorted(VALID_TYPES)}"}
    # All frontmatter fields must be single-line; return a clean error rather than
    # letting _safe_meta_value raise during render.
    for field_name, value in (("sender", sender), ("to", to), ("subject", subject),
                              ("re_id", re_id or "")):
        if "\n" in str(value) or "\r" in str(value):
            return {"ok": False, "error": f"{field_name} must be single-line"}
    mid = new_message_id()
    meta = {
        "id": mid,
        "from": sender,
        "to": to,
        "type": type,
        "subject": subject,
        "created": utc_now_iso(),
    }
    if re_id:
        meta["re"] = re_id
    if attachments:
        meta["attachments"] = attachments
    _atomic_write(_dir("inbox") / f"{mid}.md", _render(meta, body))
    _index_message(meta, body, status="unread")
    audit("bus", "send", True, payload={"to": to, "type": type, "id": mid})
    return {"ok": True, **meta}


def list_messages(*, to: str | None = None, sender: str | None = None,
                  type: str | None = None, unread_only: bool = True,
                  limit: int = 50) -> list[dict[str, Any]]:
    """List messages from the index, newest first, with optional filters."""
    clauses, params = [], []
    if unread_only:
        clauses.append("status = 'unread'")
    if to:
        clauses.append("recipient = ?"); params.append(to)
    if sender:
        clauses.append("sender = ?"); params.append(sender)
    if type:
        clauses.append("type = ?"); params.append(type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT id, sender, recipient, type, subject, created, status "
            f"FROM bus_messages {where} ORDER BY created DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def read_message(message_id: str, *, archive: bool = True) -> dict[str, Any] | None:
    """Return a message's full content. By default marks it read (moves the file
    to archive/ and flips the index status)."""
    if not _MESSAGE_ID_RE.match(message_id or ""):
        return None
    inbox_path = _dir("inbox") / f"{message_id}.md"
    arch_path = _dir("archive") / f"{message_id}.md"
    path = inbox_path if inbox_path.exists() else (arch_path if arch_path.exists() else None)
    if path is None:
        return None
    meta, body = _parse(path.read_text(encoding="utf-8"))
    if archive and path == inbox_path:
        os.replace(inbox_path, arch_path)
        with connect() as con:
            con.execute("UPDATE bus_messages SET status = 'read' WHERE id = ?", (message_id,))
    return {**meta, "body": body, "status": "read" if archive else meta.get("status", "unread")}


def search_messages(query: str, limit: int = 20) -> list[dict[str, Any]]:
    match = safe_fts_query(query)
    if match is None:
        return []
    import sqlite3
    try:
        with connect() as con:
            rows = con.execute(
                "SELECT m.id, m.sender, m.recipient, m.type, m.subject, m.created, m.status "
                "FROM bus_fts f JOIN bus_messages m ON m.id = f.message_id "
                "WHERE bus_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def share_file(src_path: str, *, name: str | None = None) -> dict[str, Any]:
    """Copy a file into the shared files/ surface. Returns its bus-relative path.

    The source must live under an allowed root (the Mongoose data dir by default,
    plus any roots in MONGOOSE_SHARED_FILE_ROOTS). This stops an MCP client/model
    from copying arbitrary process-readable files into the shared folder."""
    src = Path(src_path).resolve()
    if not src.exists() or not src.is_file():
        return {"ok": False, "error": f"not a file: {src_path}"}
    # Containment: source must be under an allowed root.
    settings = get_settings()
    roots = [settings.data_dir.resolve()]
    extra = getattr(settings, "shared_file_roots", "") or ""
    roots += [Path(r).expanduser().resolve() for r in extra.split(",") if r.strip()]
    if not any(_is_within(src, root) for root in roots):
        return {"ok": False, "error": "source not under an allowed root "
                "(set MONGOOSE_SHARED_FILE_ROOTS to permit other locations)"}
    dest_name = name or src.name
    if "/" in dest_name or "\\" in dest_name or dest_name.startswith("."):
        return {"ok": False, "error": "invalid file name"}
    dest = _dir("files") / dest_name
    # Always byte-copy: a text round-trip (errors="replace") corrupts binary
    # files (images, pdfs) by substituting undecodable bytes.
    import shutil as _shutil
    _shutil.copyfile(src, dest)
    audit("bus", "share_file", True, payload={"name": dest_name})
    return {"ok": True, "name": dest_name, "path": str(dest)}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def reindex() -> int:
    """Rebuild the index from the files on disk (recovery / first run). Returns
    the number of messages indexed."""
    with connect() as con:
        con.execute("DELETE FROM bus_messages")
        con.execute("DELETE FROM bus_fts")
    n = 0
    for status, folder in (("unread", "inbox"), ("read", "archive")):
        for path in sorted(_dir(folder).glob("*.md")):
            meta, body = _parse(path.read_text(encoding="utf-8"))
            if meta.get("id"):
                _index_message(meta, body, status=status)
                n += 1
    return n


def _index_message(meta: dict[str, Any], body: str, *, status: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO bus_messages(id, sender, recipient, type, subject, created, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status",
            (meta.get("id"), meta.get("from"), meta.get("to"), meta.get("type"),
             meta.get("subject"), meta.get("created"), status),
        )
        # contentless FTS: only insert for genuinely new rows
        exists = con.execute("SELECT 1 FROM bus_fts WHERE message_id = ?", (meta.get("id"),)).fetchone()
        if not exists:
            con.execute(
                "INSERT INTO bus_fts(message_id, subject, body, sender, recipient) VALUES (?, ?, ?, ?, ?)",
                (meta.get("id"), meta.get("subject", ""), body, meta.get("from", ""), meta.get("to", "")),
            )
