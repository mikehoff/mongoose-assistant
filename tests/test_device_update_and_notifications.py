"""Device-facing notifications list + force-update upload.

These cover the owner/guest gating and the package validation, but never apply a real update
(that's destructive); the stage/check/apply machinery itself is shared with /build/ingest.
"""
import io
import zipfile
from fastapi.testclient import TestClient


def _client():
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


def _owner_token():
    from mongoose import devices as D
    code = D.create_pairing_code(profile="primary_phone")["code"]
    return D.claim_pairing_code(code, "Owner Phone", "phone")["device_token"]


def _guest_token():
    from mongoose import devices as D
    code = D.create_pairing_code(profile="trusted_guest")["code"]
    return D.claim_pairing_code(code, "Guest Phone", "phone")["device_token"]


def _hdr(tok):
    return {"X-Mongoose-Device-Token": tok}


# ---- notifications list ----

def test_device_notifications_owner_ok():
    c = _client()
    r = c.get("/device/notifications", headers=_hdr(_owner_token())).json()
    assert r["ok"] is True
    assert isinstance(r["items"], list)


def test_device_notifications_guest_blocked():
    c = _client()
    r = c.get("/device/notifications", headers=_hdr(_guest_token())).json()
    assert r["ok"] is False
    assert r["items"] == []


# ---- force-update upload: gating + validation ----

def test_update_upload_guest_is_refused():
    c = _client()
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("pyproject.toml", "x")
    r = c.post("/device/update/upload",
               files={"file": ("p.zip", z.getvalue(), "application/zip")},
               headers=_hdr(_guest_token())).json()
    assert r["ok"] is False
    assert "owner" in (r.get("error") or "").lower()


def test_update_upload_rejects_non_zip():
    c = _client()
    r = c.post("/device/update/upload",
               files={"file": ("p.zip", b"not a zip at all", "application/zip")},
               headers=_hdr(_owner_token())).json()
    assert r["ok"] is False
    assert "zip" in (r.get("error") or "").lower()


def test_update_upload_rejects_non_mongoose_package():
    c = _client()
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("random/thing.txt", "hello, not a mongoose package")
    r = c.post("/device/update/upload",
               files={"file": ("p.zip", z.getvalue(), "application/zip")},
               headers=_hdr(_owner_token())).json()
    assert r["ok"] is False
    # validated as not-a-package at the stage step (pyproject.toml + src/mongoose required)
    assert r.get("stage") in ("stage", "manifest", "check")
    assert "package" in (r.get("error") or "").lower() or "valid" in (r.get("error") or "").lower()
