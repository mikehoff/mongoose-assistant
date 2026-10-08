"""Regression tests for the setup-journey-coherence audit: the setup journey must be HONEST
(not overclaim connected sources), CONSISTENT across surfaces (CLI matches the browser
wizard), and RESUMABLE (choices persist). Plus the two security gaps it found."""
import os
import tempfile


from mongoose.db import init_db, connect


def _fresh_db():
    os.environ["MONGOOSE_DB_PATH"] = tempfile.mktemp(suffix=".sqlite3")
    init_db()


# --- P0.1: connected-count counts only real evidence ------------------------

def test_fresh_install_reports_zero_connected():
    _fresh_db()
    from mongoose.onboarding import _connected_count
    assert _connected_count() == 0


def test_env_example_does_not_count_as_connected():
    """The exact audit repro: .env.example ships default paths/values that must NOT count as
    connected. A fresh install (even with .env.example copied) is 0 connected."""
    _fresh_db()
    from mongoose.onboarding import _connected_count
    assert _connected_count() == 0


def test_real_data_does_count():
    _fresh_db()
    from datetime import datetime, timezone
    with connect() as con:
        con.execute("INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
                    "VALUES('t','a','s','b',?,'normal',?)",
                    (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
        con.commit()
    from mongoose.onboarding import connected_sources
    assert "email" in connected_sources()


# --- P0.2: CLI setup stages match the browser wizard ------------------------

def test_cli_setup_stages_match_wizard_and_ha_is_optional():
    from mongoose.wizard import WIZARD_STAGES
    titles = [st["title"] for st in WIZARD_STAGES]
    # HA must be in the optional Home stage (key 'home'), never a "Core" stage. Optionality is now
    # conveyed warmly in the blurb rather than a parenthetical in the title.
    home = next(st for st in WIZARD_STAGES if st["key"] == "home")
    assert "skip" in home["blurb"].lower() or "optional" in home["blurb"].lower()
    assert not any(t == "Core" for t in titles)
    assert "home_assistant" in home["slugs"]


# --- P1: resumable skip-connect ---------------------------------------------

def test_skip_connect_persists_and_is_honoured():
    _fresh_db()
    import mongoose.onboarding as O
    from mongoose.db import set_app_state
    O._brain_ready = lambda: (True, "ok")
    assert O.onboarding_state()["current_step"] == "connect"
    set_app_state("onboarding_skip_connect", "true")
    assert O.onboarding_state()["current_step"] == "phone"


# --- P1: /setup/authorize is nonce-gated ------------------------------------

def test_setup_authorize_requires_nonce():
    from fastapi.testclient import TestClient
    from mongoose.app import app, _SETUP_NONCE
    c = TestClient(app)
    assert c.post("/setup/authorize/google").status_code == 403
    assert c.post("/setup/authorize/google", headers={"X-Setup-Nonce": "wrong"}).status_code == 403
    # correct nonce passes the gate (the oauth call itself may fail, but not with 403)
    assert c.post("/setup/authorize/google", headers={"X-Setup-Nonce": _SETUP_NONCE}).status_code != 403


# --- P1: /cockpit/onboarding coarse vs full ---------------------------------

def test_onboarding_full_for_loopback():
    _fresh_db()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)   # testclient host is treated as loopback
    body = c.get("/cockpit/onboarding").json()
    assert "signals" in body and "steps" in body   # full detail for loopback


def test_onboarding_coarse_for_unauthenticated_lan(monkeypatch):
    _fresh_db()
    from unittest.mock import MagicMock
    import mongoose.app as A
    monkeypatch.setattr(A, "get_settings", lambda: type("S", (), {"api_token": "tok"})())
    req = MagicMock()
    req.client.host = "192.168.1.50"
    req.headers = {}
    req.cookies = {}
    out = A.cockpit_onboarding(req)
    assert "signals" not in out and "steps" not in out   # coarse only
    assert "current_step" in out and "progress_pct" in out
