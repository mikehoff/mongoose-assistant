"""Brain preservation.

The "brain", the thing that makes Mongoose feel like one continuous resident companion, is well
contained and should NOT have to suffer when code is updated. A code update changes src/; it must not
disturb the conversation, the memory, the learned routing preferences, or the identity continuity. The
personality lives in code (and is essentially fine across versions); the local model runs in Ollama, a
SEPARATE process a Mongoose restart never touches, so the model stays warm. What actually needs
protecting is the durable brain STATE on disk.

THE BRAIN, concretely:
  - data/mongoose.sqlite3  : the one SQLite database. It holds the permanent conversation (conversations
                             + messages), long-term memory (memory_facts + the memory FTS index),
                             learned routing preferences, and app_state (onboarding/runtime flags).
  - data/session_salt      : the per-install secret that keeps identity/session continuity stable.

This module gives updates (download-driven OR self-healer-driven) three guarantees:
  1. SNAPSHOT, take a consistent copy of the brain before anything risky (a migration, a code swap),
                 using SQLite's online backup API so it's safe even while the server is running.
  2. VERIFY: integrity-check the brain (PRAGMA integrity_check + the key tables are present and
                 readable) before AND after, so a corrupting migration is caught, not shipped.
  3. CARRY: the brain is never copied into a candidate tree and never overwritten by it; it stays
                 in place in data/, and an update operates around it. A snapshot is the rollback point
                 for the brain specifically, separate from the full-tree backup.

It does NOT change code, does NOT call the network, and does NOT restart anything. It is the safety
substrate the staged updater and the self-healer build on so the core context never has to cold-wipe.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import get_settings

# The tables whose presence + readability we treat as "the brain is intact". These are the
# continuity-bearing ones; a migration that drops/wrecks one of these is a brain regression.
BRAIN_TABLES = ("conversations", "messages", "memory_facts", "routing_preferences", "app_state")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def brain_paths() -> dict[str, Path]:
    """The concrete on-disk artifacts that constitute the brain."""
    s = get_settings()
    db = Path(s.db_path)
    data_dir = db.parent
    return {
        "db": db,
        "session_salt": data_dir / "session_salt",
        "data_dir": data_dir,
    }


def snapshot_brain(label: str = "preupdate") -> dict[str, Any]:
    """Take a CONSISTENT snapshot of the brain into data/brain_snapshots/<label>-<stamp>/.

    Uses SQLite's online backup API for the database so the copy is transactionally consistent even if
    the live server is mid-write, no need to stop the brain to protect it. Copies session_salt too.
    Returns a record describing the snapshot (its path is the brain rollback point)."""
    paths = brain_paths()
    db = paths["db"]
    out_dir = paths["data_dir"] / "brain_snapshots" / f"{label}-{_now_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {"ok": False, "snapshot_dir": str(out_dir), "label": label,
                              "created_at": datetime.now(timezone.utc).isoformat()}

    # Database: online backup (consistent even under concurrent writes).
    if db.exists():
        dest_db = out_dir / db.name
        try:
            src = sqlite3.connect(str(db))
            dst = sqlite3.connect(str(dest_db))
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
            result["db_snapshot"] = str(dest_db)
            result["db_bytes"] = dest_db.stat().st_size
        except Exception as exc:  # noqa: BLE001
            # Fall back to a file copy if the backup API is unavailable for any reason.
            _ = exc
            try:
                shutil.copy2(db, dest_db)
                result["db_snapshot"] = str(dest_db)
                result["db_bytes"] = dest_db.stat().st_size
                result["db_snapshot_method"] = "filecopy_fallback"
            except Exception as exc2:  # noqa: BLE001
                result["error"] = f"db snapshot failed: {exc2}"
                return result
    else:
        result["error"] = f"no brain database at {db}"
        return result

    # session_salt: identity continuity.
    salt = paths["session_salt"]
    if salt.exists():
        try:
            shutil.copy2(salt, out_dir / salt.name)
            result["session_salt_snapshot"] = str(out_dir / salt.name)
        except Exception:
            pass

    result["ok"] = True
    return result


def verify_brain(db_path: Path | None = None) -> dict[str, Any]:
    """Integrity-check the brain: SQLite PRAGMA integrity_check passes AND every continuity-bearing
    table exists and is readable. Returns {ok, integrity, tables: {name: row_count|error}, problems}.
    Used before a migration (is the brain healthy to start?) and after (did the migration hurt it?)."""
    db = Path(db_path) if db_path else brain_paths()["db"]
    out: dict[str, Any] = {"ok": False, "db": str(db), "tables": {}, "problems": []}
    if not db.exists():
        out["problems"].append(f"no brain database at {db}")
        return out
    try:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        out["integrity"] = integ
        if integ != "ok":
            out["problems"].append(f"integrity_check: {integ}")
        for t in BRAIN_TABLES:
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                out["tables"][t] = n
            except Exception as exc:  # noqa: BLE001
                out["tables"][t] = f"ERROR: {exc}"
                out["problems"].append(f"table {t} not readable: {exc}")
        con.close()
    except Exception as exc:  # noqa: BLE001
        out["problems"].append(f"could not open brain: {exc}")
        return out
    out["ok"] = not out["problems"]
    return out


def restore_brain(snapshot_dir: str | Path) -> dict[str, Any]:
    """Restore the brain from a snapshot directory (the brain-specific rollback). Restores the DB and
    session_salt back into data/. Verifies the snapshot is intact BEFORE overwriting the live brain, so
    a bad snapshot can't make things worse. Does not touch code."""
    snap = Path(snapshot_dir)
    paths = brain_paths()
    db = paths["db"]
    if not snap.exists():
        return {"ok": False, "error": f"no snapshot at {snap}"}
    snap_db = snap / db.name
    if not snap_db.exists():
        return {"ok": False, "error": f"snapshot has no database ({snap_db})"}
    # verify the snapshot before trusting it
    pre = verify_brain(snap_db)
    if not pre["ok"]:
        return {"ok": False, "error": "snapshot failed integrity verification, refusing to restore",
                "verify": pre}
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snap_db, db)
        snap_salt = snap / "session_salt"
        if snap_salt.exists():
            shutil.copy2(snap_salt, paths["session_salt"])
        return {"ok": True, "restored_db": str(db), "from": str(snap)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"restore failed: {exc}"}


def list_snapshots(limit: int = 20) -> list[dict[str, Any]]:
    """List brain snapshots, newest first."""
    base = brain_paths()["data_dir"] / "brain_snapshots"
    if not base.exists():
        return []
    rows = []
    for d in sorted(base.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir():
            continue
        db_files = list(d.glob("*.sqlite3"))
        rows.append({
            "snapshot_dir": str(d),
            "name": d.name,
            "db_bytes": db_files[0].stat().st_size if db_files else 0,
            "has_salt": (d / "session_salt").exists(),
        })
        if len(rows) >= limit:
            break
    return rows


def _snapshot_stamp(name: str):
    """The datetime encoded in a snapshot dir name (label-YYYYmmddTHHMMSS). None if unparseable."""
    from datetime import datetime as _dt
    tail = name.rsplit("-", 1)[-1]
    for fmt, width in (("%Y%m%dT%H%M%S", 15), ("%Y%m%d", 8)):
        try:
            return _dt.strptime(tail[:width], fmt)
        except Exception:
            continue
    return None


def prune_snapshots(keep: int = 10, *, spread: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Keep `keep` brain snapshots SPREAD ACROSS TIME, rather than the newest N in a row.

    Seven consecutive dailies are seven copies of the same week: if a problem is only noticed a
    fortnight later, every surviving snapshot already contains it. So the budget is split into tiers ,
    recent days, then one per week, then one per month , which reaches back months for the same disk.
    The newest is always kept, and an undateable directory is kept rather than deleted on a guess.
    Pass spread=False for the old newest-N behaviour; dry_run=True to see the plan without deleting.
    """
    base = brain_paths()["data_dir"] / "brain_snapshots"
    if not base.exists():
        return {"deleted": 0, "kept": 0, "kept_names": []}
    # Sort by the DATE in the name, not the name itself: labels differ ("daily-", "preupdate-") and a
    # plain string sort puts every "preupdate-" above every "daily-" regardless of age, so the oldest
    # snapshot could be treated as the newest and the wrong ones kept.
    from datetime import datetime as _dt
    _floor = _dt.min
    dirs = sorted([d for d in base.iterdir() if d.is_dir()],
                  key=lambda p: (_snapshot_stamp(p.name) or _floor), reverse=True)
    keep = max(1, int(keep))

    if not spread:
        keep_set = set(dirs[:keep])
    else:
        # With keep=7: 3 daily + 2 weekly + 2 monthly.
        n_daily = max(1, keep // 2)
        n_weekly = max(1, (keep - n_daily) // 2)
        n_monthly = max(0, keep - n_daily - n_weekly)
        keep_set: set = set()
        seen_days: set = set()
        seen_weeks: set = set()
        seen_months: set = set()
        for d in dirs:                          # newest first
            ts = _snapshot_stamp(d.name)
            if ts is None:
                keep_set.add(d)                 # undateable: never delete on a guess
                continue
            day, week, month = ts.strftime("%Y%m%d"), ts.strftime("%G-W%V"), ts.strftime("%Y%m")
            if len(seen_days) < n_daily and day not in seen_days:
                seen_days.add(day); keep_set.add(d); continue
            if len(seen_weeks) < n_weekly and week not in seen_weeks:
                seen_weeks.add(week); keep_set.add(d); continue
            if len(seen_months) < n_monthly and month not in seen_months:
                seen_months.add(month); keep_set.add(d); continue
        if dirs:
            keep_set.add(dirs[0])               # the newest is always kept
            # ...and the OLDEST, so the kept set always spans the widest window available. Without
            # this, a run of same-month snapshots exhausts the monthly tier early and the deepest
            # history is pruned, leaving seven copies of one week , the thing tiering exists to avoid.
            keep_set.add(dirs[-1])
        # A "preupdate" snapshot is the state immediately BEFORE a risky change, which is exactly what
        # you reach for when an update turns out to have broken something. Keep the most recent one
        # regardless of tier; it is a different kind of record from a routine daily.
        for d in dirs:
            if d.name.startswith("preupdate"):
                keep_set.add(d)
                break

    doomed = [d for d in dirs if d not in keep_set]
    if dry_run:
        return {"deleted": 0, "would_delete": [d.name for d in doomed], "kept": len(keep_set),
                "kept_names": sorted((d.name for d in keep_set), reverse=True)}
    deleted = 0
    for d in doomed:
        try:
            shutil.rmtree(d)
            deleted += 1
        except Exception:
            pass
    return {"deleted": deleted, "kept": len(keep_set),
            "kept_names": sorted((d.name for d in keep_set), reverse=True)}


def preserve_around_update() -> dict[str, Any]:
    """The pre-update guarantee, in one call: verify the brain is healthy, then snapshot it. Returns a
    record the updater records so it can restore the brain specifically if the migration/update hurts
    it. Safe to call while the server runs. Changes no code, restarts nothing."""
    pre = verify_brain()
    snap = snapshot_brain(label="preupdate")
    return {
        "ok": snap.get("ok", False),
        "pre_verify": pre,
        "snapshot": snap,
        "note": ("Brain snapshotted before update. If the update or migration damages the brain, "
                 "restore it with this snapshot, code and brain roll back independently."),
    }
