"""Mongoose backup.

A backup is the irreplaceable part of a Mongoose install bundled into one portable archive, so the
whole thing can be restored after a disk loss, a botched migration, or a move to a new machine. It is
NOT a full code backup (the code is reproducible, it's on GitHub / in the release zips). What can't be
regenerated is:

  - the BRAIN: data/mongoose.sqlite3 (the permanent conversation, long-term memory, learned routing
    preferences, app_state), taken as a CONSISTENT snapshot via the brain_preservation online-backup
    path so it's safe to back up while the server is running;
  - IDENTITY: data/session_salt (keeps sessions/identity continuity stable);
  - CONFIG: .env (your settings, tokens references, DDNS/ntfy/Drive config). This is the operational
    soul of the install.

A backup archive is a .tar.gz containing those plus a manifest.json (version, timestamp, what's
inside, integrity hashes). Restoring unpacks them back into place, brain and config return together.

This module also keeps a LOCAL backup rota (grandfather-father-son: 7 daily + 4 weekly + 12 monthly) so
there is always a sensible local history even before/without Google Drive. The Drive integration
(backup_drive.py) uploads these same archives off-machine with the same rota.

Changes no code, restarts nothing. Safe to run on a schedule while the server is live.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import get_settings
from . import brain_preservation as BP

BACKUP_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y%m%dT%H%M%S")


def _backup_root() -> Path:
    root = get_settings().data_dir / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def free_bytes(path: Path | None = None) -> int:
    """Free space on the volume holding the backup directory."""
    import shutil as _sh
    try:
        return int(_sh.disk_usage(str(path or _backup_root())).free)
    except Exception:
        return 0


def _min_free_bytes() -> int:
    """Headroom the machine must keep AFTER a backup. Override with MONGOOSE_BACKUP_MIN_FREE_GB."""
    import os as _os
    try:
        gb = float(_os.environ.get("MONGOOSE_BACKUP_MIN_FREE_GB", "3"))
    except ValueError:
        gb = 3.0
    return int(gb * 1024 ** 3)


def _largest_archive_bytes() -> int:
    sizes = [p.stat().st_size for p in _backup_root().glob("mongoose-backup-*.tar.gz") if p.exists()]
    return max(sizes) if sizes else 1024 ** 3   # assume ~1GB when there is nothing to measure yet


def ensure_room_for_backup() -> dict[str, Any]:
    """Make room for one more archive, oldest first, never dropping the last remaining backup.

    A backup must never be the thing that fills the disk: a full volume breaks SQLite writes and takes
    the whole assistant down, which is far worse than a missing snapshot. Each archive here is ~1GB
    while the full grandfather-father-son rota wants up to 23 of them, so on a tight disk the rota
    alone is not a safety mechanism , this is.
    """
    need = _largest_archive_bytes() + _min_free_bytes()
    freed: list[str] = []
    archives = sorted(_backup_root().glob("mongoose-backup-*.tar.gz"),
                      key=lambda p: _parse_stamp(p.name) or _now())
    while free_bytes() < need and len(archives) > 1:      # never delete the last remaining backup
        victim = archives.pop(0)                          # oldest first
        try:
            victim.unlink()
            freed.append(victim.name)
        except Exception:
            break
    return {"ok": free_bytes() >= need, "freed": freed,
            "free_bytes": free_bytes(), "needed_bytes": need}


def create_backup(label: str = "auto") -> dict[str, Any]:
    """Build a brain+config backup archive. Returns a record with the archive path, size, and what
    went in. The brain is snapshotted consistently (safe while running); .env and session_salt are
    copied as-is. The archive is self-describing via an embedded manifest.

    Refuses rather than filling the volume when space is short even after pruning: an assistant with a
    slightly stale backup still works, an assistant with a full disk does not."""
    room = ensure_room_for_backup()
    if not room["ok"]:
        return {"ok": False, "disk_guard": room, "error": (
            f"not enough disk for a backup: {room['free_bytes'] // 1024**2}MB free, about "
            f"{room['needed_bytes'] // 1024**2}MB needed (pruned {len(room['freed'])} old archive(s) "
            "first). Free space, or lower MONGOOSE_BACKUP_MIN_FREE_GB.")}
    paths = BP.brain_paths()
    data_dir = paths["data_dir"]

    # 1. consistent brain snapshot into a temp staging area
    staging = Path(tempfile.mkdtemp(prefix="mongoose-backup-"))
    contents: dict[str, Any] = {}
    problems: list[str] = []
    try:
        snap = BP.snapshot_brain(label="backup")
        if not snap.get("ok"):
            return {"ok": False, "error": "brain snapshot failed: " + str(snap.get("error"))}
        snap_dir = Path(snap["snapshot_dir"])

        # copy the snapshotted DB (consistent) into staging
        for db_file in snap_dir.glob("*.sqlite3"):
            dest = staging / db_file.name
            shutil.copy2(db_file, dest)
            contents["brain_db"] = {"name": db_file.name, "sha256": _sha256(dest), "bytes": dest.stat().st_size}
        # session_salt from the snapshot (or live)
        salt_src = snap_dir / "session_salt"
        if not salt_src.exists():
            salt_src = data_dir / "session_salt"
        if salt_src.exists():
            shutil.copy2(salt_src, staging / "session_salt")
            contents["session_salt"] = {"sha256": _sha256(staging / "session_salt")}
        # the snapshot was a means to a consistent copy; clean it up so snapshots don't accumulate here
        try:
            shutil.rmtree(snap_dir)
        except Exception:
            pass

        # 2. config: .env (the operational soul). Never include secrets the user didn't ask for; .env
        #    IS the config. (secrets/ and certs/ are included if present , they're install-specific.)
        repo_root = Path(".").resolve()
        env_file = repo_root / ".env"
        if env_file.exists():
            shutil.copy2(env_file, staging / "env.backup")  # renamed so it won't be auto-loaded if extracted loose
            contents["env"] = {"sha256": _sha256(staging / "env.backup")}
        else:
            problems.append("no .env found, config not in this backup")
        for extra_dir in ("secrets", "certs"):
            src = repo_root / extra_dir
            if src.exists() and any(src.iterdir()):
                shutil.copytree(src, staging / extra_dir)
                contents[extra_dir] = {"included": True}

        # 3. manifest
        manifest = {
            "backup_version": BACKUP_VERSION,
            "mongoose_version": _version(),
            "created_at": _now().isoformat(),
            "label": label,
            "contents": contents,
            "kind": "brain+config",
            "problems": problems,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # 4. tar.gz it
        archive = _backup_root() / f"mongoose-backup-{label}-{_stamp()}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for item in staging.iterdir():
                tar.add(item, arcname=item.name)

        return {
            "ok": True,
            "archive": str(archive),
            "bytes": archive.stat().st_size,
            "sha256": _sha256(archive),
            "manifest": manifest,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def list_backups(limit: int = 50) -> list[dict[str, Any]]:
    """Local backup archives, newest first, with parsed metadata."""
    rows = []
    for p in sorted(_backup_root().glob("mongoose-backup-*.tar.gz"),
                    key=lambda x: x.name, reverse=True):
        rows.append({
            "name": p.name,
            "path": str(p),
            "bytes": p.stat().st_size,
            "tier": _tier_of(p.name),
        })
        if len(rows) >= limit:
            break
    return rows


def restore_backup(archive_path: str | Path, *, restore_config: bool = True) -> dict[str, Any]:
    """Restore brain (+ optionally config) from a backup archive. Verifies the manifest and the brain
    DB's integrity BEFORE overwriting the live brain. Code is untouched. After this, restart Mongoose
    so it reads the restored brain/config."""
    archive = Path(archive_path)
    if not archive.exists():
        return {"ok": False, "error": f"no backup at {archive}"}
    paths = BP.brain_paths()
    repo_root = Path(".").resolve()

    extract = Path(tempfile.mkdtemp(prefix="mongoose-restore-"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, extract)
        manifest_p = extract / "manifest.json"
        manifest = json.loads(manifest_p.read_text()) if manifest_p.exists() else {}

        # find + verify the brain db
        db_files = list(extract.glob("*.sqlite3"))
        if not db_files:
            return {"ok": False, "error": "backup contains no brain database"}
        verify = BP.verify_brain(db_files[0])
        if not verify.get("ok"):
            return {"ok": False, "error": "backed-up brain failed integrity verification, refusing to restore",
                    "verify": verify}

        # restore brain db + salt
        shutil.copy2(db_files[0], paths["db"])
        restored = {"brain_db": True}
        salt = extract / "session_salt"
        if salt.exists():
            shutil.copy2(salt, paths["session_salt"])
            restored["session_salt"] = True

        # restore config if asked
        if restore_config:
            env_backup = extract / "env.backup"
            if env_backup.exists():
                shutil.copy2(env_backup, repo_root / ".env")
                restored["env"] = True
            for extra_dir in ("secrets", "certs"):
                src = extract / extra_dir
                if src.exists():
                    dest = repo_root / extra_dir
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest)
                    restored[extra_dir] = True

        return {"ok": True, "restored": restored, "manifest": manifest,
                "note": "Restart Mongoose so it reads the restored brain and config."}
    finally:
        shutil.rmtree(extract, ignore_errors=True)


def apply_rota(now: datetime | None = None) -> dict[str, Any]:
    """Grandfather-father-son retention on the LOCAL archives: keep the newest 7 DAILY, 4 WEEKLY (one
    per ISO week), 12 MONTHLY (one per calendar month). Older/redundant archives are deleted. Returns
    what was kept and deleted. This is the same scheme the Drive uploader mirrors."""
    now = now or _now()
    archives = sorted(_backup_root().glob("mongoose-backup-*.tar.gz"),
                      key=lambda p: _parse_stamp(p.name) or now, reverse=True)
    keep: set[Path] = set()
    seen_days: set[str] = set()
    seen_weeks: set[str] = set()
    seen_months: set[str] = set()

    for p in archives:
        dt = _parse_stamp(p.name)
        if not dt:
            keep.add(p)  # don't delete things we can't date
            continue
        day = dt.strftime("%Y-%m-%d")
        week = f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        month = dt.strftime("%Y-%m")
        # newest 7 distinct days
        if len(seen_days) < 7 and day not in seen_days:
            seen_days.add(day)
            keep.add(p)
        # newest 4 distinct weeks
        if len(seen_weeks) < 4 and week not in seen_weeks:
            seen_weeks.add(week)
            keep.add(p)
        # newest 12 distinct months
        if len(seen_months) < 12 and month not in seen_months:
            seen_months.add(month)
            keep.add(p)

    deleted = []
    for p in archives:
        if p not in keep:
            try:
                p.unlink()
                deleted.append(p.name)
            except Exception:
                pass
    return {"kept": len(keep), "deleted": len(deleted), "deleted_names": deleted}


# ----- helpers -----

def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "?"


def _parse_stamp(name: str) -> datetime | None:
    # mongoose-backup-<label>-YYYYmmddTHHMMSS.tar.gz
    import re
    m = re.search(r"(\d{8}T\d{6})", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _tier_of(name: str) -> str:
    dt = _parse_stamp(name)
    if not dt:
        return "unknown"
    age_days = (_now() - dt).days
    if age_days <= 7:
        return "daily"
    if age_days <= 31:
        return "weekly"
    return "monthly"


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract refusing path traversal and symlinks (defensive even for our own archives)."""
    base = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise ValueError(f"unsafe archive member (link): {member.name}")
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(base)):
            raise ValueError(f"unsafe archive member (traversal): {member.name}")
    tar.extractall(dest)
