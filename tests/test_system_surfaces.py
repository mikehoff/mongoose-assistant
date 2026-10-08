"""The System tab's newly-surfaced features: data export (create/list/download), staged self-update
listing, and the proactive-suggestions view with feedback. All owner-only (nonce-gated)."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

import mongoose.app as A


def _client():
    return TestClient(A.app), {"x-cockpit-nonce": A._COCKPIT_NONCE}


def test_all_new_system_endpoints_are_nonce_gated():
    c = TestClient(A.app)
    checks = [("post", "/cockpit/dump/create"), ("post", "/cockpit/dump/list"),
              ("post", "/cockpit/dump/redeem"), ("get", "/cockpit/suggestions"),
              ("post", "/cockpit/suggestions/feedback"), ("post", "/cockpit/update-stage/list"),
              ("post", "/cockpit/update/fetch"), ("post", "/cockpit/update-stage/check"),
              ("post", "/cockpit/update-stage/approve")]
    for method, path in checks:
        r = c.get(path) if method == "get" else c.post(path, json={})
        assert r.status_code in (401, 403), f"{path} not gated ({r.status_code})"


def test_data_export_create_list_and_download():
    c, H = _client()
    r = c.post("/cockpit/dump/create", json={"kind": "everything"}, headers=H).json()
    assert r.get("ok") and r.get("download_url")
    dl = c.get(r["download_url"])
    assert dl.status_code == 200 and len(dl.content) > 0
    lst = c.post("/cockpit/dump/list", json={}, headers=H).json()
    assert lst.get("ok") and "everything" in (lst.get("kinds") or [])
    assert any(d.get("dump_id") == r.get("dump_id") for d in lst.get("dumps") or [])


def test_data_export_rejects_unknown_kind():
    c, H = _client()
    r = c.post("/cockpit/dump/create", json={"kind": "definitely_not_a_kind"}, headers=H).json()
    assert r.get("ok") is False


def test_staged_update_list_is_safe_when_empty():
    c, H = _client()
    r = c.post("/cockpit/update-stage/list", json={}, headers=H).json()
    assert "stages" in r and isinstance(r["stages"], list)


def test_suggestion_feedback_validates_and_is_graceful():
    c, H = _client()
    # invalid feedback value rejected
    bad = c.post("/cockpit/suggestions/feedback", json={"signature": "x", "feedback": "bogus"}, headers=H).json()
    assert bad.get("ok") is False
    # valid value on a missing signature fails gracefully (no crash)
    miss = c.post("/cockpit/suggestions/feedback", json={"signature": "nope", "feedback": "dismissed"}, headers=H).json()
    assert miss.get("ok") is False
    # the read view returns a list
    view = c.get("/cockpit/suggestions", headers=H).json()
    assert isinstance(view.get("suggestions"), list)
