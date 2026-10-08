"""Regression: a saved credential must not read as 'not set' because an empty/stale value sits in
os.environ, and after saving it must be immediately usable by the running server."""
import os
import tempfile
from pathlib import Path

import pytest

_ENV_KEYS = ("HA_URL", "HA_TOKEN", "GOOGLE_CLIENT_SECRETS_PATH", "GOOGLE_API_KEY",
             "MS_CLIENT_ID", "MS_TENANT_ID")


@pytest.fixture(autouse=True)
def _isolate_env():
    """Snapshot and restore the env keys these tests touch, so pollution from other test files (which
    may leave HA_URL etc. set in os.environ) can't change the precondition , and so we don't leak."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    try:
        import mongoose.setup_wizard as _SW
        saved_path = _SW.ENV_PATH
    except Exception:
        _SW = None
        saved_path = None
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        if _SW is not None:
            _SW.ENV_PATH = saved_path
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _envfile(contents=""):
    p = Path(tempfile.mktemp())
    p.write_text(contents)
    return p


def test_empty_os_environ_does_not_clobber_saved_env():
    import mongoose.setup_wizard as SW
    envf = _envfile("HA_URL=http://homeassistant.local:8123\nHA_TOKEN=tok\n")
    SW.ENV_PATH = envf
    # the bug precondition: empty-but-present value in the process env
    os.environ["HA_URL"] = ""
    try:
        eff = SW.effective_env(envf)
        # the real .env value must win over the empty process value
        assert eff.get("HA_URL") == "http://homeassistant.local:8123"
        assert eff.get("HA_TOKEN") == "tok"
    finally:
        os.environ.pop("HA_URL", None)


def test_nonempty_os_environ_still_overrides():
    import mongoose.setup_wizard as SW
    envf = _envfile("HA_URL=http://from-file:8123\n")
    SW.ENV_PATH = envf
    os.environ["HA_URL"] = "http://from-environ:8123"
    try:
        # a genuine non-empty override is still respected
        assert SW.effective_env(envf).get("HA_URL") == "http://from-environ:8123"
    finally:
        os.environ.pop("HA_URL", None)


def test_badge_and_verifier_use_same_env():
    """integration_status (the 'configured' badge) and verify_integration (the test button) must
    agree on whether HA is configured , they previously used different env-merge logic."""
    import mongoose.setup_wizard as SW
    import mongoose.wizard as W
    envf = _envfile("HA_URL=http://homeassistant.local:8123\nHA_TOKEN=tok\n")
    SW.ENV_PATH = envf
    os.environ["HA_URL"] = ""  # the clobber precondition
    try:
        status = {s["slug"]: s for s in SW.integration_status()}
        ha = status.get("home_assistant", {})
        # badge says configured (creds are in .env)
        assert ha.get("configured") is True
        # verifier sees the same creds (won't say 'not set'); it may fail to CONNECT in sandbox,
        # but the detail must not be the "not set" message
        v = W.verify_integration("home_assistant")
        assert "not set" not in (v.get("detail") or "")
    finally:
        os.environ.pop("HA_URL", None)


def test_save_makes_creds_immediately_usable_by_running_server():
    import mongoose.setup_wizard as SW
    import mongoose.wizard as W
    from mongoose.settings import get_settings
    envf = _envfile("")
    SW.ENV_PATH = envf
    os.environ["HA_URL"] = ""  # stale empty that would otherwise block load_dotenv(override=False)
    os.environ.pop("HA_TOKEN", None)
    try:
        W.save_integration("home_assistant",
                           {"HA_URL": "http://homeassistant.local:8123", "HA_TOKEN": "tok123"})
        # the live process env is updated, so the running server uses it without restart
        assert os.environ.get("HA_URL") == "http://homeassistant.local:8123"
        s = get_settings()
        assert s.ha_url == "http://homeassistant.local:8123"
        assert s.ha_token == "tok123"
    finally:
        os.environ.pop("HA_URL", None)
        os.environ.pop("HA_TOKEN", None)


def test_optional_only_integration_not_configured_by_default():
    """Google/Microsoft have all-optional fields with default values. A default string alone
    (e.g. the OAuth path that doesn't point at a real file, or the 'common' tenant) must NOT make
    the integration read as configured , the old all([])=True bug."""
    import mongoose.setup_wizard as SW
    envf = _envfile(
        "GOOGLE_CLIENT_SECRETS_PATH=./secrets/google-client-secrets.json\nMS_TENANT_ID=common\n")
    SW.ENV_PATH = envf
    for k in ("GOOGLE_CLIENT_SECRETS_PATH", "GOOGLE_API_KEY", "MS_CLIENT_ID", "MS_TENANT_ID"):
        os.environ.pop(k, None)
    status = {s["slug"]: s for s in SW.integration_status()}
    # a default OAuth path that doesn't point at a real file is NOT configured
    assert status["google_calendar"]["configured"] is False
    assert status["microsoft"]["configured"] is False


def test_gemini_configured_only_with_real_key():
    import mongoose.setup_wizard as SW
    # a real Gemini key configures GEMINI only, not calendar
    envf = _envfile("GOOGLE_API_KEY=AIza-real\n")
    SW.ENV_PATH = envf
    for k in ("GOOGLE_CLIENT_SECRETS_PATH", "GOOGLE_API_KEY"):
        os.environ.pop(k, None)
    status = {s["slug"]: s for s in SW.integration_status()}
    assert status["gemini"]["configured"] is True
    assert status["google_calendar"]["configured"] is False


def test_calendar_configured_when_oauth_file_actually_exists(tmp_path):
    """CORRECTED (v0.52.26): an OAuth client file existing is SCAFFOLDING, not sign-in. Google must
    read configured=False / auth_state='needs_signin' until an actual token exists. (Previously this
    asserted configured=True, which was the false-'configured' bug.)"""
    import mongoose.setup_wizard as SW
    real = tmp_path / "client.json"
    real.write_text('{"installed":{"client_id":"x"}}')
    envf = _envfile(f"GOOGLE_CLIENT_SECRETS_PATH={real}\n")
    SW.ENV_PATH = envf
    for k in ("GOOGLE_CLIENT_SECRETS_PATH", "GOOGLE_API_KEY", "GOOGLE_TOKEN_PATH"):
        os.environ.pop(k, None)
    # point the token path somewhere that does NOT exist, so we test the scaffolding-only state
    os.environ["GOOGLE_TOKEN_PATH"] = str(tmp_path / "no-token.json")
    status = {s["slug"]: s for s in SW.integration_status()}
    assert status["google_calendar"]["configured"] is False
    assert status["google_calendar"]["auth_state"] == "needs_signin"
    # and once a token exists, it flips to signed in / configured
    (tmp_path / "no-token.json").write_text('{"token": "ya29.' + 'a' * 200 + '"}')  # realistic size: the plausibility guard rejects a bare {}
    status2 = {s["slug"]: s for s in SW.integration_status()}
    assert status2["google_calendar"]["configured"] is True
    assert status2["google_calendar"]["auth_state"] == "signed_in"
    os.environ.pop("GOOGLE_TOKEN_PATH", None)
    # gemini stays independent (no key set)
    assert status["gemini"]["configured"] is False


def test_microsoft_configured_only_with_client_id():
    """CORRECTED (v0.52.26): a client ID alone is scaffolding, not sign-in. Microsoft must read
    configured=False / auth_state='needs_signin' until a token cache exists. (Previously asserted
    configured=True , the false-'configured' bug.)"""
    import mongoose.setup_wizard as SW
    envf = _envfile("MS_CLIENT_ID=real-client-id\n")
    SW.ENV_PATH = envf
    for k in ("MS_CLIENT_ID", "MS_TENANT_ID", "MS_TOKEN_CACHE"):
        os.environ.pop(k, None)
    import tempfile as _tf
    os.environ["MS_TOKEN_CACHE"] = _tf.mktemp(suffix=".bin")  # nonexistent
    status = {s["slug"]: s for s in SW.integration_status()}
    assert status["microsoft"]["configured"] is False
    assert status["microsoft"]["auth_state"] == "needs_signin"
    os.environ.pop("MS_TOKEN_CACHE", None)
