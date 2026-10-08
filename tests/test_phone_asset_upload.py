"""Phone file upload -> asset store -> brain tracking -> self-update / RTM-enrichment routing.

End to end: a paired device POSTs file bytes to /device/upload; it gets a stable asset id, the single
brain records the asset, and the classification routes it , a correctly-structured Mongoose build zip is
made available for self-update; other code/archives are made available for RTM enrichment.
"""
from __future__ import annotations

import io
import zipfile

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    return A, TestClient(A.app)


def _make_build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mongoose-alpha/pyproject.toml", "[project]\nname='mongoose'\nversion='9.9.9'\n")
        z.writestr("mongoose-alpha/src/mongoose/__init__.py", "__version__='9.9.9'\n")
    return buf.getvalue()


def test_store_asset_classifies_build_zip_for_self_update(client):
    A, _ = client
    from mongoose.services import assets
    rec = assets.store_asset(_make_build_zip(), "mongoose-alpha-9.9.9.zip",
                             uploaded_by="dev1", source_surface="phone")
    assert rec["ok"] and rec["asset_id"].startswith("asset_")
    assert rec["kind"] == "build_zip"
    assert rec["available_for_self_update"] is True
    # brain tracks it
    got = assets.get_asset(rec["asset_id"])
    assert got and got["sha256"] == rec["sha256"]
    # and it shows up as a self-update candidate
    cands = assets.self_update_candidates()
    assert any(c["asset_id"] == rec["asset_id"] for c in cands)


def test_store_asset_routes_code_to_rtm(client):
    A, _ = client
    from mongoose.services import assets
    rec = assets.store_asset(b"print('hello')\n", "helper.py", uploaded_by="dev1")
    assert rec["kind"] == "code"
    assert rec["available_for_rtm"] is True
    assert rec["available_for_self_update"] is False
    # surfaced to the RTM enrichment bridge
    from mongoose.services.rtm_intake import enrichment_assets
    assert any(a["asset_id"] == rec["asset_id"] for a in enrichment_assets())


def test_store_asset_is_idempotent_by_content(client):
    A, _ = client
    from mongoose.services import assets
    data = b"some bytes here for dedupe"
    a1 = assets.store_asset(data, "x.txt")
    a2 = assets.store_asset(data, "x.txt")
    assert a1["asset_id"] == a2["asset_id"]
    assert a2.get("deduped") is True


def test_upload_endpoint_requires_device_token(client):
    A, c = client
    # no device token -> 403
    r = c.post("/device/upload", content=b"abc", headers={"x-filename": "x.txt"})
    assert r.status_code == 403


def test_upload_endpoint_stores_and_classifies(client, monkeypatch):
    A, c = client
    # Bypass device auth by patching _device_from_request to a fake paired device.
    monkeypatch.setattr(A, "_device_from_request",
                        lambda request: {"device_id": "dev-xyz", "profile": "home_tablet"})
    r = c.post("/device/upload", content=_make_build_zip(),
               headers={"x-filename": "mongoose-alpha-9.9.9.zip"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["asset_id"].startswith("asset_")
    assert body["available_for_self_update"] is True
    assert "stage-asset" in body.get("next", "")
    # list + fetch via the device endpoints
    r2 = c.get("/device/assets")
    assert r2.status_code == 200 and any(a["asset_id"] == body["asset_id"] for a in r2.json()["assets"])
    r3 = c.get(f"/device/assets/{body['asset_id']}")
    assert r3.status_code == 200 and r3.json()["asset_id"] == body["asset_id"]
