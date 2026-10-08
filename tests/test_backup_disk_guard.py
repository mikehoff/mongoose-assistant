"""Backups must never fill the disk.

Enabling the grandfather-father-son rota (7 daily + 4 weekly + 12 monthly) on a machine whose
archives are ~1GB each commits to ~21GB. On a volume with a few GB free that fills the disk in days,
which breaks SQLite writes and takes the whole assistant down. A stale backup is survivable; a full
disk is not.
"""
from pathlib import Path

from mongoose import backup as B


def _fake_archive(root: Path, stamp: str, size: int = 1024) -> Path:
    p = root / f"mongoose-backup-daily-{stamp}.tar.gz"
    p.write_bytes(b"x" * size)
    return p


def test_guard_prunes_oldest_first_when_space_is_short(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_backup_root", lambda: tmp_path)
    old = _fake_archive(tmp_path, "20260101T000000")
    _fake_archive(tmp_path, "20260201T000000")
    new = _fake_archive(tmp_path, "20260301T000000")
    # pretend the volume is nearly full so the guard must free space
    monkeypatch.setattr(B, "free_bytes", lambda path=None: 0)
    B.ensure_room_for_backup()
    assert not old.exists(), "the oldest archive should go first"
    assert new.exists(), "the newest archive must never be the one deleted"


def test_guard_never_deletes_the_last_remaining_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_backup_root", lambda: tmp_path)
    only = _fake_archive(tmp_path, "20260301T000000")
    monkeypatch.setattr(B, "free_bytes", lambda path=None: 0)   # hopelessly full
    out = B.ensure_room_for_backup()
    assert only.exists(), "deleting the only backup to make room for a backup is self-defeating"
    assert out["ok"] is False, "and it must report that there is still no room"


def test_create_backup_refuses_instead_of_filling_the_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_backup_root", lambda: tmp_path)
    _fake_archive(tmp_path, "20260301T000000")
    monkeypatch.setattr(B, "free_bytes", lambda path=None: 0)
    res = B.create_backup(label="should-refuse")
    assert res["ok"] is False
    assert "disk" in (res.get("error") or "").lower()


def test_headroom_is_configurable(monkeypatch):
    monkeypatch.setenv("MONGOOSE_BACKUP_MIN_FREE_GB", "7")
    assert B._min_free_bytes() == 7 * 1024 ** 3
    monkeypatch.setenv("MONGOOSE_BACKUP_MIN_FREE_GB", "not-a-number")
    assert B._min_free_bytes() == 3 * 1024 ** 3, "a bad value must fall back, not crash the backup job"
