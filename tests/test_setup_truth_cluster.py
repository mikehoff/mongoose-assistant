"""Tests for the Cockpit setup-truth cluster (v0.52.26): OAuth integrations must not read as
'configured' from scaffolding alone; the three-state auth model; the Google JSON upload and OAuth
sign-in endpoints; search must surface email after IMAP even with zero files indexed; and
'do something cool' must recognise a partially-configured setup (IMAP/HA) instead of claiming nothing
is connected."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


def _fresh(tmp_cwd=False):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    for k in ["GOOGLE_CLIENT_SECRETS_PATH", "GOOGLE_TOKEN_PATH", "MS_CLIENT_ID", "MS_TOKEN_CACHE",
              "IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "HA_URL", "HA_TOKEN"]:
        os.environ.pop(k, None)
    if tmp_cwd:
        os.chdir(d)
    from mongoose.db import init_db
    init_db()
    return d


@pytest.fixture(autouse=True)
def _restore_cwd():
    """Several tests chdir into a temp dir (so ./secrets resolves there). Restore the original cwd
    afterwards so global cwd state never leaks into other tests."""
    import os as _os
    prev = _os.getcwd()
    try:
        yield
    finally:
        try:
            _os.chdir(prev)
        except Exception:
            pass


# ---- OAuth three-state truth model ----

def test_google_not_configured_from_client_json_alone():
    """The core false-'configured' bug: a Google client JSON existing must NOT make Google read as
    configured. It needs an actual token (sign-in)."""
    _fresh(tmp_cwd=True)
    import mongoose.wizard as W
    from mongoose.setup_wizard import INTEGRATIONS
    g = [i for i in INTEGRATIONS if i.slug == "google_calendar"][0]
    os.makedirs("secrets", exist_ok=True)
    open("secrets/google-client-secrets.json", "w").write('{"installed":{"client_id":"x"}}')
    env = {"GOOGLE_CLIENT_SECRETS_PATH": "./secrets/google-client-secrets.json"}
    assert W._is_configured(g, env) is False
    assert W._auth_state(g, env) == "needs_signin"


def test_google_configured_only_with_token():
    _fresh(tmp_cwd=True)
    import mongoose.wizard as W
    from mongoose.setup_wizard import INTEGRATIONS
    g = [i for i in INTEGRATIONS if i.slug == "google_calendar"][0]
    os.makedirs("secrets", exist_ok=True)
    open("secrets/google-client-secrets.json", "w").write('{"installed":{"client_id":"x"}}')
    open("secrets/google-token.json", "w").write('{"token": "ya29.' + 'a' * 200 + '", "refresh_token": "1//' + 'z' * 80 + '"}')
    env = {"GOOGLE_CLIENT_SECRETS_PATH": "./secrets/google-client-secrets.json"}
    assert W._is_configured(g, env) is True
    assert W._auth_state(g, env) == "signed_in"


def test_microsoft_not_configured_from_client_id_alone():
    _fresh(tmp_cwd=True)
    import mongoose.wizard as W
    from mongoose.setup_wizard import INTEGRATIONS
    ms = [i for i in INTEGRATIONS if i.slug == "microsoft"][0]
    env = {"MS_CLIENT_ID": "abc-123"}
    assert W._is_configured(ms, env) is False
    assert W._auth_state(ms, env) == "needs_signin"


def test_auth_state_three_values():
    _fresh(tmp_cwd=True)
    from mongoose.setup_wizard import _google_calendar_auth_state
    assert _google_calendar_auth_state({}) == "not_set"
    os.makedirs("secrets", exist_ok=True)
    open("secrets/google-client-secrets.json", "w").write("{}")
    assert _google_calendar_auth_state({"GOOGLE_CLIENT_SECRETS_PATH": "./secrets/google-client-secrets.json"}) == "needs_signin"
    open("secrets/google-token.json", "w").write('{"token": "ya29.' + 'a' * 200 + '", "refresh_token": "1//' + 'z' * 80 + '"}')
    assert _google_calendar_auth_state({"GOOGLE_CLIENT_SECRETS_PATH": "./secrets/google-client-secrets.json"}) == "signed_in"


def test_wizard_payload_carries_auth_state():
    """The browser wizard payload must expose auth_state so the UI can render the three states."""
    _fresh(tmp_cwd=True)
    from mongoose.wizard import wizard_schema
    payload = wizard_schema()
    found = {}
    for st in payload["stages"]:
        for ig in st["integrations"]:
            found[ig["slug"]] = ig
    assert "auth_state" in found.get("google_calendar", {})
    assert found["google_calendar"]["auth_state"] == "not_set"
    assert found["google_calendar"]["configured"] is False


# ---- upload + authorize endpoints ----

def test_upload_google_json_rejects_non_json():
    _fresh(tmp_cwd=True)
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app)
    r = c.post("/cockpit/wizard/upload-google-json",
               json={"content": "not json at all"},
               headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_upload_google_json_rejects_wrong_shape():
    _fresh(tmp_cwd=True)
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app)
    r = c.post("/cockpit/wizard/upload-google-json",
               json={"content": '{"hello":"world"}'},
               headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.json()["ok"] is False  # no installed/web client_id


def test_upload_google_json_accepts_valid_client_file():
    _fresh(tmp_cwd=True)
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app)
    good = '{"installed":{"client_id":"abc.apps.googleusercontent.com","client_secret":"s"}}'
    r = c.post("/cockpit/wizard/upload-google-json",
               json={"content": good},
               headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    body = r.json()
    assert body["ok"] is True
    assert body.get("next") == "sign_in"
    assert os.path.exists(body["path"])


def test_upload_google_json_requires_nonce():
    _fresh(tmp_cwd=True)
    from mongoose.app import app
    c = TestClient(app)
    r = c.post("/cockpit/wizard/upload-google-json", json={"content": "{}"})
    assert r.status_code == 403


def test_authorize_rejects_bad_provider():
    _fresh(tmp_cwd=True)
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app)
    r = c.post("/cockpit/wizard/authorize/yahoo", headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.json()["ok"] is False


def test_authorize_requires_nonce():
    _fresh(tmp_cwd=True)
    from mongoose.app import app
    c = TestClient(app)
    r = c.post("/cockpit/wizard/authorize/google")
    assert r.status_code == 403


# ---- search surfaces email after IMAP even with zero files ----

def test_search_empty_index_false_when_email_present():
    _fresh()
    from mongoose.db import connect
    from mongoose.services.find import find
    with connect() as con:
        con.execute("INSERT INTO email_messages (source, subject, body, sender, mailbox, indexed_at) "
                    "VALUES ('imap','Quarterly report','the body text here','a@b.com','INBOX','2026-01-01')")
        con.commit()
    r = find("report")
    assert r["empty_index"] is False
    assert r["indexed_counts"]["email"] == 1
    assert r["indexed_counts"]["files"] == 0


def test_search_empty_index_true_when_all_empty():
    _fresh()
    from mongoose.services.find import find
    r = find("anything")
    assert r["empty_index"] is True


def test_search_files_only_kind_respects_emptiness():
    _fresh()
    from mongoose.db import connect
    from mongoose.services.find import find
    with connect() as con:
        con.execute("INSERT INTO email_messages (source, subject, body, sender, mailbox, indexed_at) "
                    "VALUES ('imap','x','y','a@b.com','INBOX','2026-01-01')")
        con.commit()
    # asking only for files, with none indexed, is correctly empty
    r = find("x", kinds=("file",))
    assert r["empty_index"] is True


# ---- do something cool recognises partial config ----

def test_connected_domains_detects_imap_and_ha():
    _fresh()
    os.environ["IMAP_HOST"] = "imap.gmail.com"
    os.environ["IMAP_USERNAME"] = "alex@example.com"
    os.environ["IMAP_PASSWORD"] = "app-pw"
    os.environ["HA_URL"] = "http://192.168.1.20:8123"
    os.environ["HA_TOKEN"] = "tok"
    from mongoose.services.tutorial import _connected_domains
    names = _connected_domains()
    assert any("Home Assistant" in n for n in names)
    assert any("IMAP" in n or "Email" in n for n in names)
    for k in ["IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "HA_URL", "HA_TOKEN"]:
        os.environ.pop(k, None)


def test_connected_domains_empty_on_bare_install(monkeypatch):
    _fresh()
    # searxng_url deliberately DEFAULTS to a local instance URL (settings.py: override
    # MONGOOSE_SEARXNG_URL or set it empty), so a genuinely bare install for this test's
    # purposes must explicitly blank it; otherwise "Web search (SearXNG)" is legitimately
    # counted as connected-by-default.
    monkeypatch.setenv("MONGOOSE_SEARXNG_URL", "")
    from mongoose.services.tutorial import _connected_domains
    assert _connected_domains() == []
