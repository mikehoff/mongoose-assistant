"""Token-gated one-shot build ingest: POST a zip -> stage -> check -> apply, with no human click.
The load-bearing safety property: a build that FAILS its checks is staged but NEVER applied. Also:
the endpoint is token-only (no cockpit nonce), and refuses entirely if no API token is configured."""
import os
import tempfile


def _fresh(token=None):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    if token is None:
        os.environ.pop("MONGOOSE_API_TOKEN", None)
    else:
        os.environ["MONGOOSE_API_TOKEN"] = token
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _client():
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A


def test_ingest_refuses_when_no_token_configured():
    _fresh(token=None)
    c, _ = _client()
    r = c.post("/build/ingest", json={"source": "/tmp/x.zip"})
    assert r.status_code == 403
    assert "MONGOOSE_API_TOKEN" in r.json()["detail"]


def test_ingest_rejects_wrong_token():
    _fresh(token="secret-abc")
    c, _ = _client()
    r = c.post("/build/ingest", json={"source": "/tmp/x.zip"},
               headers={"x-mongoose-token": "wrong"})
    assert r.status_code == 403


def test_ingest_rejects_cockpit_nonce_alone():
    """apply is destructive: a browser nonce must NOT be accepted, only the API token."""
    _fresh(token="secret-abc")
    c, A = _client()
    r = c.post("/build/ingest", json={"source": "/tmp/x.zip"},
               headers={"x-cockpit-nonce": A._COCKPIT_NONCE})
    assert r.status_code == 403


def test_failing_checks_are_staged_but_not_applied(monkeypatch):
    _fresh(token="secret-abc")
    c, _ = _client()
    import mongoose.updater as U
    applied = {"n": 0}
    monkeypatch.setattr(U, "stage_update_candidate",
                        lambda src: {"ok": True, "stage_id": "stage-1"})
    # checks FAIL
    monkeypatch.setattr(U, "check_update_stage",
                        lambda sid, **kw: {"stage_id": sid, "ok": False,
                                           "checks": [{"name": "pyflakes", "ok": False}]})
    monkeypatch.setattr(U, "approve_update_stage",
                        lambda sid: applied.__setitem__("approved", True))
    monkeypatch.setattr(U, "apply_update_stage",
                        lambda sid, **kw: applied.__setitem__("n", applied["n"] + 1) or 0)
    # source must exist on disk to pass the early existence check
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".zip"); os.close(fd)
    r = c.post("/build/ingest", json={"source": tmp},
               headers={"x-mongoose-token": "secret-abc"})
    body = r.json()
    assert body["ok"] is False
    assert body["stage"] == "check"
    assert applied["n"] == 0          # apply was NEVER called
    assert "approved" not in applied  # approve was NEVER called


def test_passing_checks_apply_end_to_end(monkeypatch):
    _fresh(token="secret-abc")
    c, _ = _client()
    import mongoose.updater as U
    calls = []
    monkeypatch.setattr(U, "stage_update_candidate", lambda src: {"ok": True, "stage_id": "s2"})
    monkeypatch.setattr(U, "check_update_stage",
                        lambda sid, **kw: {"stage_id": sid, "ok": True,
                                           "checks": [{"name": "manifest", "ok": True},
                                                      {"name": "compileall", "ok": True}]})
    monkeypatch.setattr(U, "approve_update_stage", lambda sid: calls.append(("approve", sid)))
    monkeypatch.setattr(U, "apply_update_stage",
                        lambda sid, **kw: calls.append(("apply", sid, kw)) or 0)
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".zip"); os.close(fd)
    r = c.post("/build/ingest", json={"source": tmp},
               headers={"x-mongoose-token": "secret-abc"})
    body = r.json()
    assert body["ok"] is True and body["stage"] == "applied"
    assert ("approve", "s2") in calls
    # apply was called with yes=True (the no-click property)
    assert any(k[0] == "apply" and k[2].get("yes") is True for k in calls)


def test_missing_source_errors():
    _fresh(token="secret-abc")
    c, _ = _client()
    r = c.post("/build/ingest", json={}, headers={"x-mongoose-token": "secret-abc"})
    assert r.json()["ok"] is False
    assert "need an uploaded" in r.json()["error"]
