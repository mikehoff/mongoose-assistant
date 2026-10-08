"""The in-cockpit 'drop a zip and update' flow: /build/upload accepts a zip and returns a stageable path."""
from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def _nonce():
    import mongoose.app as A
    return A._COCKPIT_NONCE


def test_upload_requires_nonce(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    # no nonce -> 403
    r = c.post("/build/upload", content=b"PK\x03\x04zip", headers={"X-Filename": "x.zip"})
    assert r.status_code == 403


def test_upload_rejects_non_zip(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.post("/build/upload", content=b"this is not a zip",
               headers={"X-Cockpit-Nonce": _nonce(), "X-Filename": "notes.txt"})
    j = r.json()
    assert j["ok"] is False and "zip" in j["error"].lower()


def test_upload_rejects_empty(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.post("/build/upload", content=b"", headers={"X-Cockpit-Nonce": _nonce()})
    assert r.json()["ok"] is False


def test_upload_accepts_zip_and_returns_source(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    import os
    c = TestClient(A.app)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pyproject.toml", "[project]\nname='mongoose'")
        z.writestr("src/mongoose/__init__.py", "__version__='1.1'")
    r = c.post("/build/upload", content=buf.getvalue(),
               headers={"X-Cockpit-Nonce": _nonce(), "X-Filename": "mongoose-1.1.19-sillygoose.zip"})
    j = r.json()
    assert j["ok"] is True
    assert j["source"] and os.path.exists(j["source"])     # a real path the stage endpoint can use
    assert j["filename"].endswith(".zip")
    assert j["bytes"] > 0


def test_cockpit_has_upload_ui():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    # the drop zone, the buttons, and the wiring all present
    assert "updrop" in h and "Drop a build .zip" in h
    assert "Update now" in h
    assert "/build/upload" in h and "updateUploadThen" in h


def test_deploy_accepts_uploaded_source_path(monkeypatch, tmp_path):
    """The drag/drop UI uploads a zip, then calls /build/deploy with {source:path}.
    That path must be accepted, not rejected as 'url or stage_id only'."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    import mongoose.updater as U
    c = TestClient(A.app)
    src = tmp_path / "candidate.zip"
    src.write_bytes(b"PK\x03\x04fake")
    calls = []
    monkeypatch.setattr(U, "stage_update_candidate", lambda p: calls.append(("stage", str(p))) or {"ok": True, "stage_id": "s-upload"})
    monkeypatch.setattr(U, "check_update_stage", lambda sid, **kw: calls.append(("check", sid, kw)) or {"ok": True, "checks": [{"name": "manifest", "ok": True}]})
    monkeypatch.setattr(U, "approve_update_stage", lambda sid: calls.append(("approve", sid)))
    monkeypatch.setattr(U, "apply_update_stage", lambda sid, **kw: calls.append(("apply", sid, kw)) or 0)

    r = c.post("/build/deploy", json={"source": str(src)}, headers={"X-Cockpit-Nonce": _nonce()})
    j = r.json()
    assert j["ok"] is True
    assert ("stage", str(src)) in calls
    assert any(ca[0] == "apply" and ca[2].get("yes") is True for ca in calls)


def test_deploy_missing_source_reports_source_in_contract(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.post("/build/deploy", json={}, headers={"X-Cockpit-Nonce": _nonce()})
    j = r.json()
    assert j["ok"] is False
    assert "source" in j["error"]
