"""Regression tests for the v0.52.9 fixes: phone access actually works (not just a warning), the
core/liveness contradiction is gone, ntfy/MS messaging is actionable, reindex+selfcheck endpoints
exist, and the conversations count is replaced by a meaningful chat-history message count."""
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolate_env_and_path():
    """These tests reassign setup_wizard.ENV_PATH and let enable-phone write env vars; restore both
    so we never pollute other test files (write_env/read_env tests rely on the default ENV_PATH)."""
    import mongoose.setup_wizard as SW
    saved_path = SW.ENV_PATH
    saved_env = {k: os.environ.get(k) for k in
                 ("MONGOOSE_HOST", "MONGOOSE_ALLOW_REMOTE", "MONGOOSE_API_TOKEN", "MONGOOSE_INDEX_ROOTS")}
    try:
        yield
    finally:
        SW.ENV_PATH = saved_path
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


# ---- enable-phone writes the settings that make a phone able to connect ----

def test_enable_phone_writes_lan_settings_and_token(monkeypatch):
    d = _fresh()
    import mongoose.setup_wizard as SW
    from pathlib import Path
    envf = Path(d) / ".env"
    SW.ENV_PATH = envf
    envf.write_text("")
    import mongoose.enable_phone as EP
    monkeypatch.setattr(EP.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(EP.subprocess, "Popen", lambda *a, **k: None)
    rc = EP.enable_phone(yes=True)
    assert rc == 0
    t = envf.read_text()
    assert "MONGOOSE_HOST=0.0.0.0" in t
    assert "MONGOOSE_ALLOW_REMOTE=true" in t
    assert "MONGOOSE_API_TOKEN=" in t


def test_start_scripts_do_not_hardcode_host():
    """The installer/bootstrap must NOT pass --host (it would override MONGOOSE_HOST and make phone
    access impossible). They should start `mongoose serve` letting the setting decide."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("Install Mongoose.command", "scripts/bootstrap_macos.sh"):
        text = (root / name).read_text()
        # find the serve start line(s)
        for line in text.splitlines():
            if "mongoose serve" in line and "nohup" in line or ("mongoose serve" in line and "&" in line):
                assert "--host" not in line, f"{name} hardcodes --host on the serve line: {line}"


# ---- pairing note is actionable (points to enable-phone) ----

def test_pairing_note_points_to_enable_phone(monkeypatch):
    _fresh()
    import mongoose.app as APP

    class _S:
        host = "127.0.0.1"   # loopback-only => phone can't reach => note must fire
        allow_remote = False
        port = 8765
    monkeypatch.setattr(APP, "get_settings", lambda: _S())
    note = APP._pairing_reachability_note()
    assert note is not None
    # Note is now a sentinel string; the JS cockpit renders the "Enable phone access" button.
    assert note == "not_lan_reachable"


# ---- core/liveness coherence: liveness knows about the brain ----

def test_liveness_headline_reflects_core_down(monkeypatch):
    _fresh()
    os.environ.pop("MONGOOSE_INDEX_ROOTS", None)
    import mongoose.wizard as W
    # force core "not ready"
    monkeypatch.setattr(W, "_core_status", lambda: {"ready": False,
                        "items": [{"label": "Local brain (Ollama)", "ok": False,
                                   "detail": "Ollama not reachable"}]})
    from mongoose.liveness import liveness
    rep = liveness()
    # headline must not claim everything is fresh/running when the brain is down
    assert "fresh and running" not in rep["headline"]
    assert "brain" in rep["headline"].lower()
    core = [c for c in rep["checks"] if c["subsystem"].startswith("core")]
    assert core and core[0]["state"] != "ok"


def test_liveness_core_ok_allows_fresh_headline(monkeypatch):
    _fresh()
    os.environ.pop("MONGOOSE_INDEX_ROOTS", None)
    import mongoose.wizard as W
    monkeypatch.setattr(W, "_core_status", lambda: {"ready": True,
                        "items": [{"label": "Local brain (Ollama)", "ok": True, "detail": "ok"}]})
    from mongoose.liveness import liveness
    rep = liveness()
    core = [c for c in rep["checks"] if c["subsystem"].startswith("core")][0]
    assert core["state"] == "ok"


# ---- ntfy refused message is actionable ----

def test_ntfy_refused_points_to_setup(monkeypatch):
    _fresh()
    import urllib.request
    from mongoose import setup_wizard as SW

    def boom(*a, **k):
        raise OSError("[Errno 61] Connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ok, msg = SW.verify_ntfy({"NTFY_URL": "http://127.0.0.1:8090", "NTFY_TOPIC": "t"})
    assert ok is False
    assert "setup-ntfy" in msg


# ---- Microsoft help has concrete steps + link ----

def test_microsoft_help_has_steps_and_link():
    _fresh()
    from mongoose.setup_wizard import INTEGRATIONS
    ms = [i for i in INTEGRATIONS if i.slug == "microsoft"][0]
    # The no-registration To Do path leads, but the advanced app-registration route still references
    # entra and the Graph permissions for users who want calendar/mail too.
    assert "entra.microsoft.com" in ms.help_text
    assert "Calendars.Read" in ms.help_text and "Tasks.Read" in ms.help_text
    # and it must make the easy path obvious: no registration needed for To Do
    assert "Sign in with Microsoft" in ms.help_text and "do NOT need to register" in ms.help_text


# ---- reindex + selfcheck endpoints exist ----

def test_reindex_and_selfcheck_endpoints_exist():
    _fresh()
    from mongoose.app import app
    paths = {r.path for r in app.routes}
    assert "/cockpit/reindex-files" in paths
    assert "/cockpit/selfcheck" in paths


def test_selfcheck_reports_chat_ui_present():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    d = TestClient(app).get("/cockpit/selfcheck").json()
    assert d["chat_lime_css_present"] is True
    assert d["chat_css_trapped_in_reduced_motion"] is False
    assert d["chat_unified_conversation"] is True


# ---- conversations count replaced by meaningful chat-history message count ----

def test_transparency_uses_chat_history_not_conversation_count():
    _fresh()
    from mongoose.transparency import _data_categories
    cats = {c["category"] for c in _data_categories()}
    assert "Chat history" in cats
    assert "Conversations" not in cats
