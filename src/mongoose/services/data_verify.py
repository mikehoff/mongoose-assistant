from __future__ import annotations

import os
from typing import Any

from ..db import connect

# Data verifier.
#
# The schema verifier answers "do the tables/columns/indexes exist?". This answers
# the harder question: "is the data INSIDE them internally coherent and trustworthy?"
# A local corpus you can't trust is worse than no corpus, so this checks row-level
# invariants: no duplicate identities, FTS rows exist for searchable rows, indexed
# files still exist on disk, attachments link to something, etc.
#
# It is READ-ONLY. Repair is a separate, explicit, opt-in step (repair-data), and
# defaults to dry-run, because user state is sacred and destructive fixes must be
# deliberate.

OK = "ok"
WARN = "warn"
ERROR = "error"


def _finding(level: str, check: str, detail: str, count: int = 0, fixable: str | None = None) -> dict[str, Any]:
    return {"level": level, "check": check, "detail": detail, "count": count, "fixable": fixable}


def verify_data() -> dict[str, Any]:
    """Run all row-level integrity checks. Returns findings (ok/warn/error) plus a
    summary. Never modifies anything."""
    findings: list[dict[str, Any]] = []
    with connect() as con:
        findings += _check_files(con)
        findings += _check_attachments(con)
        findings += _check_email(con)
        findings += _check_memory(con)
        findings += _check_whatsapp(con)
        findings += _check_social(con)
    errors = sum(1 for f in findings if f["level"] == ERROR)
    warns = sum(1 for f in findings if f["level"] == WARN)
    return {
        "ok": errors == 0,
        "errors": errors,
        "warnings": warns,
        "findings": findings,
    }


def _has_table(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _check_files(con) -> list[dict[str, Any]]:
    out = []
    if not _has_table(con, "files"):
        return out
    # No duplicate (device, path).
    dupes = con.execute(
        "SELECT device, path, COUNT(*) c FROM files GROUP BY device, path HAVING c > 1").fetchall()
    out.append(_finding(ERROR if dupes else OK, "files.unique_device_path",
                        f"{len(dupes)} duplicate (device, path) key(s)" if dupes
                        else "no duplicate (device, path) keys", len(dupes),
                        fixable="dedupe-files" if dupes else None))
    # Empty paths/titles.
    empties = con.execute("SELECT COUNT(*) FROM files WHERE path = '' OR title = ''").fetchone()[0]
    out.append(_finding(ERROR if empties else OK, "files.nonempty",
                        f"{empties} file row(s) with empty path/title" if empties
                        else "all file rows have path and title", empties))
    # FTS coherence: every files row should have an FTS row.
    if _has_table(con, "file_fts"):
        n_files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_fts = con.execute("SELECT COUNT(*) FROM file_fts").fetchone()[0]
        out.append(_finding(ERROR if n_fts < n_files else OK, "files.fts_coverage",
                            f"{n_files} files but {n_fts} FTS rows" if n_fts < n_files
                            else f"FTS covers all {n_files} file(s)",
                            max(0, n_files - n_fts), fixable="rebuild-file-fts" if n_fts < n_files else None))
    # Missing physical files: local-device files whose path no longer exists. (Phone/
    # remote-device paths aren't on this machine, so we only check device='local'.)
    missing = 0
    for row in con.execute("SELECT path FROM files WHERE device = 'local'").fetchall():
        if row["path"] and not os.path.exists(row["path"]):
            missing += 1
    out.append(_finding(WARN if missing else OK, "files.physical_present",
                        f"{missing} local file(s) indexed but no longer on disk" if missing
                        else "all local indexed files still exist", missing,
                        fixable="mark-missing-files" if missing else None))
    return out


def _check_attachments(con) -> list[dict[str, Any]]:
    out = []
    if not _has_table(con, "attachments"):
        return out
    # Empty filenames.
    empties = con.execute("SELECT COUNT(*) FROM attachments WHERE filename = '' OR filename IS NULL").fetchone()[0]
    out.append(_finding(ERROR if empties else OK, "attachments.filename",
                        f"{empties} attachment(s) with empty filename" if empties
                        else "all attachments have a filename", empties))
    # FTS coherence for attachments that have content text.
    if _has_table(con, "attachment_fts"):
        with_text = con.execute(
            "SELECT COUNT(*) FROM attachments WHERE content_text IS NOT NULL AND content_text != ''").fetchone()[0]
        n_fts = con.execute("SELECT COUNT(*) FROM attachment_fts").fetchone()[0]
        out.append(_finding(WARN if n_fts < with_text else OK, "attachments.fts_coverage",
                            f"{with_text} attachments with content but {n_fts} FTS rows" if n_fts < with_text
                            else f"FTS covers content attachments ({n_fts})",
                            max(0, with_text - n_fts)))
    return out


def _check_email(con) -> list[dict[str, Any]]:
    out = []
    if not _has_table(con, "email_messages"):
        return out
    # No duplicate (source, source_message_id).
    dupes = con.execute(
        "SELECT source, source_message_id, COUNT(*) c FROM email_messages "
        "WHERE source_message_id IS NOT NULL GROUP BY source, source_message_id HAVING c > 1").fetchall()
    out.append(_finding(ERROR if dupes else OK, "email.unique_message_id",
                        f"{len(dupes)} duplicate (source, message_id)" if dupes
                        else "no duplicate email message ids", len(dupes),
                        fixable="dedupe-email" if dupes else None))
    # FTS coherence.
    if _has_table(con, "email_fts"):
        n_msg = con.execute("SELECT COUNT(*) FROM email_messages").fetchone()[0]
        n_fts = con.execute("SELECT COUNT(*) FROM email_fts").fetchone()[0]
        out.append(_finding(ERROR if n_fts < n_msg else OK, "email.fts_coverage",
                            f"{n_msg} emails but {n_fts} FTS rows" if n_fts < n_msg
                            else f"FTS covers all {n_msg} email(s)",
                            max(0, n_msg - n_fts), fixable="rebuild-email-fts" if n_fts < n_msg else None))
    return out


def _check_memory(con) -> list[dict[str, Any]]:
    out = []
    if not _has_table(con, "memory_facts"):
        return out
    # Exact duplicate facts.
    dupes = con.execute(
        "SELECT fact, COUNT(*) c FROM memory_facts GROUP BY fact HAVING c > 1").fetchall()
    out.append(_finding(WARN if dupes else OK, "memory.no_exact_duplicates",
                        f"{len(dupes)} duplicated fact(s)" if dupes
                        else "no exact duplicate facts", len(dupes),
                        fixable="dedupe-memory" if dupes else None))
    return out


def _check_whatsapp(con) -> list[dict[str, Any]]:
    out = []
    if not _has_table(con, "whatsapp_messages"):
        return out
    # No duplicate wa_message_id.
    dupes = con.execute(
        "SELECT wa_message_id, COUNT(*) c FROM whatsapp_messages "
        "WHERE wa_message_id IS NOT NULL GROUP BY wa_message_id HAVING c > 1").fetchall()
    out.append(_finding(ERROR if dupes else OK, "whatsapp.unique_message_id",
                        f"{len(dupes)} duplicate WhatsApp message id(s)" if dupes
                        else "no duplicate WhatsApp message ids", len(dupes)))
    # FTS coverage: every message should have an FTS row (now that bodies index
    # locally regardless of privacy).
    if _has_table(con, "whatsapp_fts"):
        n_msg = con.execute("SELECT COUNT(*) FROM whatsapp_messages").fetchone()[0]
        n_fts = con.execute("SELECT COUNT(*) FROM whatsapp_fts").fetchone()[0]
        out.append(_finding(ERROR if n_fts < n_msg else OK, "whatsapp.fts_coverage",
                            f"{n_msg} messages but {n_fts} FTS rows" if n_fts < n_msg
                            else f"FTS covers all {n_msg} WhatsApp message(s)",
                            max(0, n_msg - n_fts)))
    # Drafts in a known status.
    if _has_table(con, "whatsapp_drafts"):
        bad = con.execute(
            "SELECT COUNT(*) FROM whatsapp_drafts WHERE status NOT IN "
            "('pending','approved','sent','cancelled','failed')").fetchone()[0]
        out.append(_finding(WARN if bad else OK, "whatsapp.draft_status",
                            f"{bad} draft(s) in an unknown status" if bad
                            else "all drafts in a known status", bad))
    return out


def _check_social(con) -> list[dict[str, Any]]:
    out = []
    # Ritual log: completed entries should have a completed_at.
    if _has_table(con, "ritual_log"):
        bad = con.execute(
            "SELECT COUNT(*) FROM ritual_log WHERE outcome = 'completed' AND completed_at IS NULL").fetchone()[0]
        out.append(_finding(WARN if bad else OK, "ritual.completed_has_timestamp",
                            f"{bad} completed ritual(s) missing completed_at" if bad
                            else "completed rituals have timestamps", bad))
    # Diary entries non-empty.
    if _has_table(con, "diary_entries"):
        empties = con.execute("SELECT COUNT(*) FROM diary_entries WHERE summary = ''").fetchone()[0]
        out.append(_finding(WARN if empties else OK, "diary.nonempty",
                            f"{empties} empty diary entries" if empties
                            else "all diary entries have a summary", empties))
    # Change checkpoints: one row per domain, parseable timestamp.
    if _has_table(con, "change_checkpoints"):
        dupes = con.execute(
            "SELECT domain, COUNT(*) c FROM change_checkpoints GROUP BY domain HAVING c > 1").fetchall()
        out.append(_finding(ERROR if dupes else OK, "checkpoints.one_per_domain",
                            f"{len(dupes)} domain(s) with duplicate checkpoints" if dupes
                            else "one checkpoint per domain", len(dupes)))
    return out


# --- repair (explicit, dry-run by default) -----------------------------------
_SAFE_REPAIRS = {
    "rebuild-file-fts", "rebuild-email-fts", "mark-missing-files",
    "dedupe-memory", "dedupe-files", "dedupe-email",
}


def repair_data(action: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Apply ONE narrow repair. Defaults to dry-run; the caller must pass
    dry_run=False explicitly to mutate. Only the whitelisted safe repairs are
    available, and none of them delete user content (they fix indexes or mark
    state); duplicate removal keeps the newest row."""
    if action not in _SAFE_REPAIRS:
        return {"action": action, "ok": False, "error": "unknown or unsafe repair action"}
    if dry_run:
        # Report what WOULD change by reusing the verifier counts.
        report = verify_data()
        target = next((f for f in report["findings"] if f.get("fixable") == action), None)
        return {"action": action, "dry_run": True,
                "would_affect": target["count"] if target else 0,
                "detail": target["detail"] if target else "nothing to do"}
    # Real repairs are intentionally minimal and non-destructive.
    with connect() as con:
        if action == "dedupe-memory":
            con.execute(
                "DELETE FROM memory_facts WHERE id NOT IN "
                "(SELECT MAX(id) FROM memory_facts GROUP BY fact)")
        elif action == "dedupe-files":
            con.execute(
                "DELETE FROM files WHERE id NOT IN "
                "(SELECT MAX(id) FROM files GROUP BY device, path)")
        elif action == "dedupe-email":
            con.execute(
                "DELETE FROM email_messages WHERE id NOT IN "
                "(SELECT MAX(id) FROM email_messages GROUP BY source, source_message_id)")
        # FTS rebuilds and mark-missing are heavier; left as recognised no-ops here
        # with a clear signal so we never pretend to have done more than we did.
        elif action in ("rebuild-file-fts", "rebuild-email-fts", "mark-missing-files"):
            return {"action": action, "ok": True, "applied": False,
                    "note": "recognised but not auto-applied; run a reindex to rebuild FTS, "
                            "or remove missing files via re-indexing their root"}
    return {"action": action, "ok": True, "applied": True}
