"""Single brain across access points + OAuth/email endpoints (reconciled shapes)."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    for k, v in env.items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


# --- Single brain: the unique, valuable tests ---

def test_classify_role_routes_task_queries_to_tool():
    from mongoose.role_router import classify_role
    assert classify_role("what's my task list") == "tool"
    assert classify_role("what folders are there in TODO") == "tool"
    assert classify_role("show me my tasks") == "tool"
    assert classify_role("what integrations are working") == "tool"
    assert classify_role("turn on the lights") == "tool"
    assert classify_role("hi there") == "chat"
    assert classify_role("tell me a joke") == "chat"


def test_chat_with_memory_injects_tool_routing():
    import inspect
    import mongoose.llm as L
    src = inspect.getsource(L._chat_turn)  # turn body moved under the per-conversation turn lock
    assert "classify_role" in src
    assert "run_tool_turn" in src


def test_llm_chat_uses_default_conversation_for_chat_role():
    import inspect
    import mongoose.app as A
    src = inspect.getsource(A.llm_chat)
    assert "DEFAULT_CONVERSATION_ID" in src


# --- OAuth: function-based port (reconciled shape) ---

def test_oauth_port_is_configurable_via_env(monkeypatch):
    from mongoose.oauth import google_oauth_redirect_port, google_oauth_redirect_uri
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "8877")
    assert google_oauth_redirect_port() == 8877
    assert google_oauth_redirect_uri() == "http://localhost:8877/"


def test_oauth_redirect_port_defaults_to_8766(monkeypatch):
    from mongoose.oauth import google_oauth_redirect_port
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_PORT", raising=False)
    assert google_oauth_redirect_port() == 8766


def test_oauth_redirect_port_safe_on_bad_value(monkeypatch):
    from mongoose.oauth import google_oauth_redirect_uri
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "not-a-port")
    assert google_oauth_redirect_uri() == "http://localhost:8766/"


def test_cockpit_oauth_config_endpoint_returns_live_port():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    c = TestClient(A.app)
    r = c.get("/cockpit/oauth-config")
    assert r.status_code == 200
    d = r.json()
    assert "localhost:8766/" in d["google_redirect_uri"]


# --- Email endpoints (reconciled, nonce-gated shape) ---

def test_email_detail_404_on_missing():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        nonce = c.get("/cockpit/nonce").json()["cockpit_nonce"]
        r = c.get("/email/message/999999", headers={"X-Cockpit-Nonce": nonce})
        assert r.status_code == 404


def test_email_detail_returns_body_and_download():
    _fresh()
    import base64
    from mongoose.services.email_index import EmailIn, ingest_email
    raw = b"Subject: RTM\nFrom: a@b.com\n\nBody text"
    created = ingest_email(EmailIn(source="imap", source_message_id="<x1>", mailbox="INBOX",
        sender="a@b.com", recipients="me@x.com", subject="RTM", body="Body text",
        raw={"raw_rfc822_b64": base64.b64encode(raw).decode()}))
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        nonce = c.get("/cockpit/nonce").json()["cockpit_nonce"]
        d = c.get(f"/email/message/{created['id']}", headers={"X-Cockpit-Nonce": nonce})
        assert d.status_code == 200
        assert d.json()["message"]["body"] == "Body text"
        txt = c.get(f"/email/message/{created['id']}/download?format=txt", headers={"X-Cockpit-Nonce": nonce})
        assert txt.status_code == 200
        assert b"Body text" in txt.content
        eml = c.get(f"/email/message/{created['id']}/download?format=eml", headers={"X-Cockpit-Nonce": nonce})
        assert eml.content == raw  # authentic raw bytes
