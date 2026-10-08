"""Backup location transparency: folder link, GFS breakdown, connect/backup-now endpoints."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    return d


def test_backup_status_includes_policy_and_local_gfs():
    _fresh()
    from mongoose.backup_drive import backup_status
    s = backup_status()
    assert "policy" in s
    assert "grandfather-father-son" in s["policy"]
    assert "local_gfs" in s
    assert "drive_connected" in s


def test_drive_folder_info_graceful_when_not_connected():
    _fresh()
    from mongoose.backup_drive import drive_folder_info
    info = drive_folder_info()
    assert info["connected"] is False
    assert info["folder_name"] == "Mongoose Backups"


def test_classify_gfs_buckets():
    _fresh()
    from mongoose.backup_drive import _classify_gfs
    names = [
        "mongoose-20260601T120000.tgz",  # daily
        "mongoose-20260531T120000.tgz",  # daily
        "mongoose-20260524T120000.tgz",  # prior week
        "mongoose-20260401T120000.tgz",  # prior month
    ]
    g = _classify_gfs(names)
    assert g["daily"] >= 1
    assert g["weekly"] >= 1
    assert g["monthly"] >= 1
    assert "7 daily" in g["policy"]


def test_cockpit_drive_folder_endpoint():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        r = c.get("/cockpit/drive-folder")
        assert r.status_code == 200
        assert r.json()["folder_name"] == "Mongoose Backups"


def test_cockpit_backup_status_endpoint_shows_policy():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        r = c.get("/cockpit/backup-status")
        assert r.status_code == 200
        assert "policy" in r.json()


def test_backup_and_drive_endpoints_registered():
    _fresh()
    import mongoose.app as A
    routes = {r.path for r in A.app.routes}
    assert "/cockpit/drive-connect" in routes
    assert "/cockpit/backup-now" in routes
    assert "/cockpit/drive-folder" in routes
