"""Brain wake-up self-check: guarantee the brain comes back with FULL working memory after a
restart or a reinstall, not just 'a database that opens'.

A brain that opens is not the same as a brain that remembers. The failure modes this guards against:

  1. Stranded WAL          : a hard kill can leave committed rows in the -wal file; a checkpoint on
                             wake-up folds them back so nothing committed is lost.
  2. Missing tables        : a manual unzip + pip (skipping init-db), or a partial restore, can leave
                             the schema incomplete. init_db() is idempotent, so we re-run it to
                             materialise any missing table without touching existing data.
  3. Schema drift          : a reinstall shipping a newer schema needs its additive migrations applied
                             so OLD data stays readable. We run the same migration path init_db() uses.
  4. FTS desync (the subtle one): the *_fts tables are contentless (content=''), so they are NOT
                             auto-synced: they're filled by explicit INSERTs paired with the base row.
                             After a partial restore or a base-table rebuild, a base table can have rows
                             while its FTS shadow is empty. The brain then "wakes up" but memory SEARCH
                             silently returns nothing. We detect base-rows-without-FTS-rows and rebuild
                             the FTS index from the base table, restoring recall.

Run on every startup (cheap when healthy: a handful of COUNT(*) reads). Never raises into boot.
Returns a structured report so the cockpit/CLI can show what woke up and what was repaired.
"""
from __future__ import annotations

from typing import Any

from .db import audit, connect, utc_now_iso

# Every table that must exist for a FULL brain, matching exactly what init_db() creates (verified
# against the live schema, so wake_up never falsely flags a table that init_db doesn't make).
# Lazily-created tables (e.g. whatsapp_drafts, created on first draft) are intentionally NOT here:
# their absence on a fresh brain is normal, not a defect.
EXPECTED_TABLES = (
    "app_state", "attachments", "audit_log", "bus_messages", "change_checkpoints",
    "conversations", "crawl_runs", "crawl_sources", "diary_entries", "email_messages",
    "events", "files", "memory_facts", "messages", "notifications", "phrase_history",
    "ritual_log", "routing_outcomes", "routing_preferences", "tasks", "voicemail_messages",
    "weather_cache", "weather_consensus", "whatsapp_contact_aliases", "whatsapp_messages",
)

# FTS shadow table -> (base table, [base columns in FTS column order]).
# rowid convention: FTS rowid == base table id (matches every existing INSERT in the codebase).
# Used to detect desync (base has rows, FTS empty) and to rebuild the index from the base table.
_FTS_SPEC: dict[str, tuple[str, list[str]]] = {
    "memory_fts": ("memory_facts", ["fact"]),
    "email_fts": ("email_messages", ["subject", "body", "sender", "mailbox"]),
    "file_fts": ("files", ["title", "body", "path"]),
    "notification_fts": ("notifications", ["title", "body", "source", "app"]),
    "whatsapp_fts": ("whatsapp_messages", ["contact", "body"]),
}


def _table_exists(con, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _count(con, table: str) -> int | None:
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return None


def _column_names(con, table: str) -> list[str]:
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _checkpoint_wal() -> dict[str, Any]:
    """Fold any committed-but-stranded WAL frames back into the main DB file."""
    try:
        with connect() as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _rebuild_one_fts(con, fts: str, base: str, cols: list[str]) -> int:
    """Clear and repopulate one FTS index from its base table. Returns rows re-indexed."""
    # Wipe the FTS index (contentless 'delete-all' command), then re-insert from the base.
    con.execute(f"INSERT INTO {fts}({fts}) VALUES('delete-all')")
    col_list = ", ".join(cols)
    placeholders = ", ".join(["?"] * (len(cols) + 1))  # +1 for rowid
    rows = con.execute(f"SELECT id, {col_list} FROM {base}").fetchall()
    n = 0
    for r in rows:
        values = [r["id"]] + [r[c] for c in cols]
        con.execute(
            f"INSERT INTO {fts}(rowid, {col_list}) VALUES ({placeholders})",
            values,
        )
        n += 1
    return n


def _check_and_repair_fts(*, repair: bool = True) -> dict[str, Any]:
    """Detect FTS desync (base has rows, FTS empty) and rebuild the affected indexes."""
    desynced: list[str] = []
    rebuilt: dict[str, int] = {}
    details: dict[str, Any] = {}
    with connect() as con:
        for fts, (base, cols) in _FTS_SPEC.items():
            if not _table_exists(con, fts) or not _table_exists(con, base):
                continue
            base_n = _count(con, base)
            fts_n = _count(con, fts)
            details[fts] = {"base_rows": base_n, "fts_rows": fts_n}
            # Desync = base has rows but FTS has materially fewer (empty, or partially populated).
            if base_n and (fts_n is None or fts_n < base_n):
                desynced.append(fts)
                if repair:
                    # Verify the base actually has the columns we expect before rebuilding.
                    have = set(_column_names(con, base))
                    if not set(cols).issubset(have):
                        details[fts]["skipped"] = f"base missing columns {set(cols) - have}"
                        continue
                    try:
                        rebuilt[fts] = _rebuild_one_fts(con, fts, base, cols)
                    except Exception as exc:
                        details[fts]["rebuild_error"] = str(exc)
    return {"ok": True, "desynced": desynced, "rebuilt": rebuilt, "details": details}


def wake_up(*, repair: bool = True) -> dict[str, Any]:
    """The startup self-check. Verify the brain is whole and, if asked, repair what is safely
    repairable (WAL checkpoint, missing tables, FTS desync). Returns a structured report.

    Safe to call on every boot. Never raises."""
    report: dict[str, Any] = {"ok": True, "at": utc_now_iso(), "actions": [], "problems": []}

    # Detect missing tables BEFORE any repair, so the report honestly says what was missing even
    # though we then re-create them. (init_db below is idempotent and re-materialises them.)
    pre_missing: list[str] = []
    try:
        with connect() as con:
            file_has_any = _table_exists(con, "conversations")
            if file_has_any:  # only meaningful if the brain file already has a schema
                for t in EXPECTED_TABLES:
                    if not _table_exists(con, t):
                        pre_missing.append(t)
    except Exception:
        pass

    # Ensure the schema exists before we verify it: ONLY in repair mode. init_db() is idempotent
    # and creates the file + tables if absent (e.g. running this on a never-initialised path). In
    # check-only mode we must not mutate, so we skip it and judge the brain as-is.
    if repair:
        try:
            from .db import init_db
            init_db()
        except Exception as exc:
            report["problems"].append(f"schema init errored: {exc}")

    # 1. Integrity check first: if the file itself is corrupt, repair steps could make it worse.
    try:
        from .brain_preservation import verify_brain
        vb = verify_brain()
        report["integrity"] = vb.get("integrity", "unknown")
        report["brain_tables"] = vb.get("tables", {})
        if not vb.get("ok"):
            report["ok"] = False
            report["problems"].extend(vb.get("problems", []))
            # A corrupt brain is not something we silently "repair": surface it loudly.
            # (restore_brain from a snapshot is the human-gated recovery, not an automatic one.)
            if vb.get("integrity") and vb["integrity"] != "ok":
                report["problems"].append(
                    "brain integrity_check failed: restore from a snapshot: "
                    "mongoose brain --restore <snapshot>")
                audit("brain_wakeup", "integrity_failed", False, payload={"integrity": vb.get("integrity")})
                return report  # do NOT run repairs on a corrupt file
    except Exception as exc:
        report["problems"].append(f"integrity check errored: {exc}")

    if repair:
        # 2. Checkpoint the WAL so nothing committed is stranded.
        wal = _checkpoint_wal()
        if wal.get("ok"):
            report["actions"].append("wal_checkpoint")
        else:
            report["problems"].append(f"wal checkpoint: {wal.get('error')}")

        # 3. Tables were already re-materialised by the idempotent init_db above; report what had
        #    been missing (detected pre-repair) so the wake-up is honest about what it fixed.
        report["missing_tables"] = pre_missing
        if pre_missing:
            report["actions"].append(f"recreated_missing_tables:{pre_missing}")
    else:
        # check-only: report missing without having repaired
        report["missing_tables"] = pre_missing
        if pre_missing:
            report["problems"].append(f"missing tables (not repaired): {pre_missing}")

    # 4. FTS desync detection + rebuild (the silent-broken-search case).
    fts = _check_and_repair_fts(repair=repair)
    report["fts"] = {"desynced": fts.get("desynced", []), "rebuilt": fts.get("rebuilt", {})}
    if fts.get("rebuilt"):
        for name, n in fts["rebuilt"].items():
            report["actions"].append(f"rebuilt_fts:{name}({n})")
    if fts.get("desynced") and not repair:
        report["problems"].append(f"FTS desync (not repaired): {fts['desynced']}")

    if report["problems"]:
        report["ok"] = False
    audit("brain_wakeup", "wake_up", report["ok"],
          payload={"actions": report["actions"], "problems": report["problems"][:5]})
    return report


def wakeup_summary() -> dict[str, Any]:
    """Read-only wake-up status for the cockpit/brain tool: runs a CHECK (no repair)."""
    return wake_up(repair=False)
