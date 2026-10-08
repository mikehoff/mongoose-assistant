"""Tests for the Google Drive backup integration (mocked Drive API). Verifies graceful degradation when
Drive isn't connected, the minimal drive.file scope, the separate token path, and that backup_to_drive
still does a local backup when Drive is absent."""
from __future__ import annotations

import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "data", "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = os.path.join(d, "data")
    os.environ["GOOGLE_TOKEN_PATH"] = os.path.join(d, "secrets", "google-token.json")
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    os.makedirs(os.path.join(d, "secrets"), exist_ok=True)
    os.chdir(d)
    with open(os.path.join(d, ".env"), "w") as f:
        f.write("NTFY_TOPIC=x\n")
    with open(os.path.join(d, "data", "session_salt"), "w") as f:
        f.write("S")
    from mongoose.db import init_db
    init_db()
    from mongoose.services.memory import ensure_conversation, add_message, DEFAULT_CONVERSATION_ID
    ensure_conversation(DEFAULT_CONVERSATION_ID)
    add_message(DEFAULT_CONVERSATION_ID, "user", "remember the boiler code")
    return d


def test_minimal_scope_is_drive_file():
    _fresh()
    from mongoose import backup_drive as BD
    # the scope must be the minimal app-files-only scope, NOT full drive
    assert BD.DRIVE_SCOPES == ["https://www.googleapis.com/auth/drive.file"]


def test_drive_token_is_separate_from_calendar_token():
    _fresh()
    from mongoose import backup_drive as BD
    from mongoose.settings import get_settings
    # the Drive token path is distinct from the google (calendar) token path
    assert str(BD._drive_token_path()) != get_settings().google_token_path
    assert BD._drive_token_path().name == "google-drive-token.json"


def test_is_connected_false_without_token():
    _fresh()
    from mongoose import backup_drive as BD
    assert BD.is_connected() is False


def test_backup_to_drive_does_local_when_drive_absent():
    _fresh()
    from mongoose import backup_drive as BD
    r = BD.backup_to_drive()
    # local backup still happens; drive is skipped, not an error
    assert r["ok"] is True
    assert r["drive_uploaded"] is False
    assert r["local_backup"]["ok"] is True
    assert "skipped" in r["drive"]


def test_backup_to_drive_uploads_when_connected(monkeypatch):
    """With a (faked) connected Drive, backup_to_drive uploads the archive and applies the Drive rota."""
    _fresh()
    from mongoose import backup_drive as BD
    # fake "connected"
    monkeypatch.setattr(BD, "is_connected", lambda: True)
    uploaded = {}

    def fake_upload(archive):
        uploaded["archive"] = str(archive)
        return {"ok": True, "file_id": "fake123", "name": os.path.basename(str(archive)), "bytes": 100}

    def fake_rota(**kwargs):
        return {"ok": True, "kept": 1, "trashed": 0}

    monkeypatch.setattr(BD, "upload_backup", fake_upload)
    monkeypatch.setattr(BD, "apply_drive_rota", fake_rota)
    r = BD.backup_to_drive()
    assert r["ok"] is True
    assert r["drive_uploaded"] is True
    assert uploaded["archive"].endswith(".tar.gz")
    assert r["drive_rota"]["kept"] == 1
