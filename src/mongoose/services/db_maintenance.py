"""Database retention and maintenance for an always-on install.

Mongoose runs for months on the Mac mini and several tables are pure telemetry:
diagnostics events, device events, audit entries, weather caches, crawl runs. Nothing
ever deleted from them, so the database grows without bound and every backup carries
the whole history of every temperature reading. This service applies conservative
retention to an EXPLICIT allowlist of log/cache tables, then keeps SQLite itself
healthy (WAL checkpoint, planner statistics, and a space-reclaiming VACUUM only when
the freelist is genuinely large).

Safety posture:
  - Allowlist only. User data is never touched: knowledge records/archive, WhatsApp
    messages, body/wellness data, diaries, conversations, memory facts, song exchanges,
    tasks, events, and every rtm_* table (statutory money records) are all untouchable
    by construction; they are simply not in the map.
  - Retention compares on the date prefix (substr(col,1,10) < 'YYYY-MM-DD'), which is
    correct for every ISO-ish timestamp format in use regardless of timezone suffix.
  - MONGOOSE_RETENTION_DISABLED=1 turns the whole thing into a no-op that reports why.
  - surpriser_log keeps a full year because it deduplicates delight suggestions;
    trimming it early would make Mongoose repeat itself.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

# table -> (timestamp column, days kept). Explicit allowlist; add deliberately.
RETENTION_DAYS: dict[str, tuple[str, int]] = {
    "diag_events": ("created_at", 30),
    "weather_cache": ("fetched_at", 7),
    "weather_consensus": ("updated_at", 7),
    "crawl_runs": ("started_at", 60),
    "delight_cache": ("created_at", 60),
    "device_events": ("created_at", 90),
    "bug_events": ("created_at", 90),
    "audit_log": ("created_at", 180),
    "knowledge_activation_log": ("created_at", 180),
    "self_heal_incidents": ("detected_at", 180),
    "self_harden_observations": ("created_at", 180),
    "surpriser_log": ("created_at", 365),
}

# table -> (order column, rows kept). For tables where "newest N" fits better than age.
# (change_checkpoints was considered and deliberately excluded: it is keyed one-row-per-domain
# and cannot grow.)
RETENTION_KEEP_NEWEST: dict[str, tuple[str, int]] = {}

# VACUUM is a full rewrite; only worth it when a real fraction of the file is dead.
_VACUUM_FREELIST_FRACTION = 0.10
_VACUUM_MIN_DB_BYTES = 20 * 1024 * 1024


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _table_exists(con, name: str) -> bool:
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(row)


def apply_retention() -> dict[str, Any]:
    """Delete expired rows from the allowlisted log/cache tables. Returns per-table counts."""
    if os.environ.get("MONGOOSE_RETENTION_DISABLED", "").strip() in ("1", "true", "yes"):
        return {"ok": True, "skipped": "MONGOOSE_RETENTION_DISABLED is set", "deleted": {}}
    from ..db import connect
    deleted: dict[str, int] = {}
    with connect() as con:
        for table, (col, days) in RETENTION_DAYS.items():
            if not _table_exists(con, table):
                continue
            cur = con.execute(
                f"DELETE FROM {table} WHERE substr({col},1,10) < ?", (_cutoff(days),))
            if cur.rowcount:
                deleted[table] = cur.rowcount
        for table, (col, keep) in RETENTION_KEEP_NEWEST.items():
            if not _table_exists(con, table):
                continue
            cur = con.execute(
                f"DELETE FROM {table} WHERE rowid NOT IN "
                f"(SELECT rowid FROM {table} ORDER BY {col} DESC LIMIT ?)", (keep,))
            if cur.rowcount:
                deleted[table] = deleted.get(table, 0) + cur.rowcount
        con.commit()
    return {"ok": True, "deleted": deleted, "rows_deleted": sum(deleted.values())}


def db_health() -> dict[str, Any]:
    """Cheap health facts about the SQLite file: size, freelist fraction, quick_check verdict."""
    from ..settings import get_settings
    from ..db import connect
    out: dict[str, Any] = {}
    try:
        out["db_bytes"] = os.path.getsize(get_settings().db_path)
    except Exception:
        out["db_bytes"] = 0
    with connect() as con:
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
        out["page_count"] = int(page_count)
        out["freelist_count"] = int(freelist)
        out["freelist_fraction"] = round(freelist / page_count, 4) if page_count else 0.0
        qc = con.execute("PRAGMA quick_check(1)").fetchone()
        out["quick_check"] = str(qc[0]) if qc else "unknown"
    return out


def run_maintenance() -> dict[str, Any]:
    """Nightly pass: retention, WAL checkpoint, planner stats, conditional VACUUM."""
    result = apply_retention()
    if result.get("skipped"):
        return result
    from ..db import connect
    health = db_health()
    vacuumed = False
    with connect() as con:
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        try:
            con.execute("PRAGMA optimize")
        except Exception:
            pass
    if (health.get("freelist_fraction", 0) >= _VACUUM_FREELIST_FRACTION
            and health.get("db_bytes", 0) >= _VACUUM_MIN_DB_BYTES):
        # VACUUM must run outside a transaction on its own connection.
        import sqlite3
        from ..settings import get_settings
        try:
            con2 = sqlite3.connect(str(get_settings().db_path))
            con2.execute("VACUUM")
            con2.close()
            vacuumed = True
        except Exception:
            vacuumed = False
    result["vacuumed"] = vacuumed
    result["health"] = db_health()
    return result
