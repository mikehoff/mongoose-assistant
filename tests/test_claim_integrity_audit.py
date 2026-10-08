"""Claim-integrity audit regressions: each test forces the hidden state that would make a
user-visible claim false, and asserts the claim is now honest.

Finding 1: 'nothing leaves the machine' , sync checkpoints must not transmit data off-machine
without an explicit outbound-sync opt-in, even when a notification channel (ntfy) exists.
Finding 2: 'configured' / 'ready' , setup, doctor and the running server must agree on the same
condition; an empty value in os.environ must not make them diverge.
"""
import os
import tempfile
from pathlib import Path

import pytest

_KEYS = ("HA_URL", "HA_TOKEN", "MONGOOSE_SYNC_DELIVERY")


@pytest.fixture(autouse=True)
def _isolate_env():
    import mongoose.setup_wizard as SW
    saved = {k: os.environ.get(k) for k in _KEYS}
    saved_envpath = SW.ENV_PATH
    for k in _KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        SW.ENV_PATH = saved_envpath  # don't leak a temp .env path into other test files
        import mongoose.settings as _st
        _st._SETTINGS = None
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fresh_db():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    from mongoose.db import init_db
    init_db()
    return d


# ---- Finding 1: local-first / nothing leaves without opt-in -------------------

def test_sync_checkpoint_does_not_transmit_without_optin(monkeypatch):
    import asyncio
    _fresh_db()
    import mongoose.services.notifications as N
    pushed = []

    async def spy(title, body, **kw):
        pushed.append((title, body))
        return {"sent": True}

    monkeypatch.setattr(N, "send_to_phone", spy)
    from mongoose.services import sync_checkpoint as SC
    # delivery NOT opted in (default)
    result = asyncio.run(SC.run_checkpoint())
    assert result["checkpoint"]["id"]  # assembled locally
    assert pushed == [], "checkpoint data was pushed off-machine without opt-in"
    assert result["delivery"]["delivered"] is False


def test_sync_checkpoint_channels_not_deliverable_without_optin():
    _fresh_db()
    from mongoose.services import sync_checkpoint as SC
    st = SC.channel_status()
    assert st["_delivery_enabled"] is False
    # no channel should claim deliverable when outbound sync is off
    assert not any(v.get("deliverable") for k, v in st.items() if k != "_delivery_enabled")


def test_sync_checkpoint_delivers_when_opted_in(monkeypatch):
    import asyncio
    _fresh_db()
    monkeypatch.setenv("MONGOOSE_SYNC_DELIVERY", "on")
    import mongoose.settings as st
    st._SETTINGS = None
    import mongoose.services.notifications as N
    pushed = []

    async def spy(title, body, **kw):
        pushed.append((title, body))
        return {"sent": True}

    monkeypatch.setattr(N, "send_to_phone", spy)
    from mongoose.services import sync_checkpoint as SC
    asyncio.run(SC.run_checkpoint())
    assert pushed, "opted-in sync did not deliver"


# ---- Finding 2: configured/ready consistency across surfaces ------------------

def test_empty_env_does_not_split_setup_and_doctor():
    """The classic three-truths drift: HA creds in .env, but an empty HA_URL in os.environ made the
    setup badge say configured while doctor/the running server said not configured."""
    envf = Path(tempfile.mktemp())
    envf.write_text("HA_URL=http://homeassistant.local:8123\nHA_TOKEN=tok123\n")
    _fresh_db()
    import mongoose.setup_wizard as SW
    SW.ENV_PATH = envf
    os.environ["HA_URL"] = ""  # the clobber

    setup_ha = {s["slug"]: s for s in SW.integration_status()}["home_assistant"]["configured"]

    import mongoose.settings as st
    st._SETTINGS = None
    from mongoose.settings import get_settings
    s = get_settings()
    doctor_ha = bool(s.ha_url and s.ha_token)

    assert setup_ha == doctor_ha, "setup and doctor disagree on HA configured-ness"
    assert setup_ha is True and doctor_ha is True


def test_legit_nonempty_override_still_wins():
    envf = Path(tempfile.mktemp())
    envf.write_text("HA_URL=http://from-file:8123\nHA_TOKEN=tok\n")
    _fresh_db()
    import mongoose.setup_wizard as SW
    SW.ENV_PATH = envf
    os.environ["HA_URL"] = "http://from-environ:8123"
    import mongoose.settings as st
    st._SETTINGS = None
    from mongoose.settings import get_settings
    assert get_settings().ha_url == "http://from-environ:8123"


def test_no_env_file_does_not_crash():
    envf = Path(tempfile.mktemp())  # does not exist
    _fresh_db()
    import mongoose.setup_wizard as SW
    SW.ENV_PATH = envf
    os.environ["HA_URL"] = ""
    import mongoose.settings as st
    st._SETTINGS = None
    from mongoose.settings import get_settings
    assert get_settings().ha_url == ""


# ---- Follow-up: ntfy public-server privacy is surfaced at config time ----------

def test_ntfy_verifier_warns_on_public_server(monkeypatch):
    """Configuring a public ntfy server (content transits a third party) must not return a bare
    'success' , the privacy consequence has to be visible at the moment of configuration."""
    _fresh_db()
    import urllib.request
    from mongoose import setup_wizard as SW

    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp())

    ok_pub, msg_pub = SW.verify_ntfy({"NTFY_URL": "https://ntfy.sh", "NTFY_TOPIC": "t"})
    assert ok_pub is True  # it does send
    assert "third party" in msg_pub  # but the privacy cost is surfaced

    ok_loc, msg_loc = SW.verify_ntfy({"NTFY_URL": "http://127.0.0.1:8090", "NTFY_TOPIC": "t"})
    assert ok_loc is True
    assert "third party" not in msg_loc  # local => no warning
    assert "network" in msg_loc.lower()  # affirms content stays local
