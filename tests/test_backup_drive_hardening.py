"""Hardening tests for the Google Drive backup: retry on transient errors, upload verification
(size/checksum), the rota never deleting a good backup for an unverified one, and failures being
recorded (audit + app_state + self-harden) rather than silent."""
from __future__ import annotations

import pytest

# Skip this test file if googleapiclient is not installed (it's under the 'connectors' extra).
# This prevents the core QA gate from failing due to a missing optional dependency.
pytest.importorskip("googleapiclient", reason="googleapiclient not installed (connectors extra)")

import os
import socket
import tempfile
import unittest.mock as M


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "data", "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = os.path.join(d, "data")
    os.environ["GOOGLE_TOKEN_PATH"] = os.path.join(d, "secrets", "google-token.json")
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    os.makedirs(os.path.join(d, "secrets"), exist_ok=True)
    os.chdir(d)
    with open(os.path.join(d, ".env"), "w") as f:
        f.write("X=1\n")
    with open(os.path.join(d, "data", "session_salt"), "w") as f:
        f.write("S")
    from mongoose.db import init_db
    init_db()
    from mongoose.services.memory import ensure_conversation, add_message, DEFAULT_CONVERSATION_ID
    ensure_conversation(DEFAULT_CONVERSATION_ID)
    add_message(DEFAULT_CONVERSATION_ID, "user", "data")
    return d


# ---- retry discipline ----

def test_retry_retries_transient_then_succeeds():
    _fresh()
    import mongoose.backup_drive as BD
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise socket.timeout("transient")
        return "ok"
    with M.patch.object(BD.time, "sleep"):
        assert BD._retry(flaky, "t") == "ok"
    assert calls["n"] == 3


def test_retry_does_not_retry_fatal():
    _fresh()
    import mongoose.backup_drive as BD
    calls = {"n": 0}

    def fatal():
        calls["n"] += 1
        raise ValueError("not retryable")
    import pytest
    with pytest.raises(ValueError):
        BD._retry(fatal, "t")
    assert calls["n"] == 1


# ---- upload verification ----

class _FakeFiles:
    def __init__(self, drive_size, drive_md5):
        self.ds = drive_size
        self.dm = drive_md5
        self.deleted = []

    def list(self, **k):
        return M.MagicMock(execute=lambda: {"files": []})

    def create(self, **k):
        return M.MagicMock(execute=lambda: {"id": "FID", "name": "x", "size": str(self.ds),
                                            "md5Checksum": self.dm})

    def update(self, **k):
        return M.MagicMock(execute=lambda: {"id": "FID", "name": "x", "size": str(self.ds),
                                            "md5Checksum": self.dm})

    def delete(self, **k):
        self.deleted.append(k.get("fileId"))
        return M.MagicMock(execute=lambda: {})


class _FakeService:
    def __init__(self, files):
        self._f = files

    def files(self):
        return self._f


def _patched_upload(BD, fake_files):
    return [
        M.patch.object(BD, "_service", return_value=_FakeService(fake_files)),
        M.patch.object(BD, "_ensure_folder", return_value="FOLDER"),
        M.patch.object(BD, "_find_existing", return_value=None),
        M.patch("googleapiclient.http.MediaFileUpload", M.MagicMock()),
    ]


def test_corrupt_upload_is_rejected_and_deleted():
    _fresh()
    import mongoose.backup_drive as BD
    from mongoose import backup as B
    made = B.create_backup("daily")
    import pathlib
    real_size = pathlib.Path(made["archive"]).stat().st_size
    ff = _FakeFiles(drive_size=real_size + 999, drive_md5="wrong")
    patches = _patched_upload(BD, ff)
    for p in patches:
        p.start()
    try:
        up = BD.upload_backup(made["archive"])
    finally:
        for p in patches:
            p.stop()
    assert up["ok"] is False
    assert "FID" in ff.deleted  # the bad upload was removed


def test_verified_upload_is_accepted():
    _fresh()
    import mongoose.backup_drive as BD
    from mongoose import backup as B
    import pathlib
    made = B.create_backup("daily")
    real_size = pathlib.Path(made["archive"]).stat().st_size
    real_md5 = BD._local_md5(pathlib.Path(made["archive"]))
    ff = _FakeFiles(drive_size=real_size, drive_md5=real_md5)
    patches = _patched_upload(BD, ff)
    for p in patches:
        p.start()
    try:
        up = BD.upload_backup(made["archive"])
    finally:
        for p in patches:
            p.stop()
    assert up["ok"] is True
    assert up["verified"] is True


# ---- safe rota ordering ----

def test_failed_upload_does_not_run_rota():
    _fresh()
    import mongoose.backup_drive as BD
    rota = {"n": 0}
    with M.patch.object(BD, "is_connected", return_value=True), \
         M.patch.object(BD, "upload_backup", return_value={"ok": False, "error": "verification failed"}), \
         M.patch.object(BD, "apply_drive_rota", lambda **k: rota.__setitem__("n", rota["n"] + 1)):
        r = BD.backup_to_drive()
    assert r["drive_uploaded"] is False
    assert rota["n"] == 0  # rota must NOT run after a failed upload


def test_successful_upload_runs_rota_protecting_new_file():
    _fresh()
    import mongoose.backup_drive as BD
    captured = {"protect": None}

    def fake_rota(*, protect_name=None):
        captured["protect"] = protect_name
        return {"ok": True, "kept": 1, "trashed": 0}
    with M.patch.object(BD, "is_connected", return_value=True), \
         M.patch.object(BD, "upload_backup", return_value={"ok": True, "file_id": "F",
                        "name": "mongoose-backup-daily-X.tar.gz", "bytes": 100, "verified": True}), \
         M.patch.object(BD, "apply_drive_rota", fake_rota):
        r = BD.backup_to_drive()
    assert r["drive_uploaded"] is True
    assert captured["protect"] is not None  # rota protects the just-uploaded archive


# ---- failure visibility (integration) ----

def test_failure_is_recorded_not_silent():
    _fresh()
    import mongoose.backup_drive as BD
    from mongoose.db import get_app_state, connect
    with M.patch.object(BD, "is_connected", return_value=True), \
         M.patch.object(BD, "upload_backup", return_value={"ok": False, "error": "boom"}), \
         M.patch.object(BD, "apply_drive_rota", lambda **k: None):
        BD.backup_to_drive()
    # app_state error stamped
    assert get_app_state("drive_backup_last_error", "")
    # audit failure row
    with connect() as c:
        aud = c.execute("SELECT 1 FROM audit_log WHERE module='backup_drive' AND success=0").fetchall()
    assert len(aud) > 0
    # self-harden observation under the backup_durability claim
    with connect() as c:
        obs = c.execute("SELECT severity FROM self_harden_observations WHERE claim_key='backup_durability'").fetchall()
    assert len(obs) > 0
    assert obs[0][0] == "P0"


def test_success_clears_error_and_stamps_ok():
    _fresh()
    import mongoose.backup_drive as BD
    from mongoose.db import get_app_state, set_app_state
    set_app_state("drive_backup_last_error", "old error")
    with M.patch.object(BD, "is_connected", return_value=True), \
         M.patch.object(BD, "upload_backup", return_value={"ok": True, "file_id": "F",
                        "name": "mongoose-backup-daily-X.tar.gz", "bytes": 100, "verified": True}), \
         M.patch.object(BD, "apply_drive_rota", lambda **k: {"ok": True, "kept": 1, "trashed": 0}):
        BD.backup_to_drive()
    assert get_app_state("drive_backup_last_error", "") == ""  # cleared on success
    assert get_app_state("drive_backup_last_ok_at", "")        # stamped


def test_backup_durability_claim_in_registry():
    _fresh()
    from mongoose.services.self_harden import claims
    keys = [c["key"] for c in claims()]
    assert "backup_durability" in keys


def test_backup_status_surfaces_lane():
    _fresh()
    import mongoose.backup_drive as BD
    st = BD.backup_status()
    assert "last_ok_at" in st and "last_error" in st and "local_count" in st and "drive_connected" in st
