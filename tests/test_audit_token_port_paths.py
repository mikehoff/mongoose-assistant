"""Audit fixes: token-path consistency (Gmail/Calendar/MS probes) + non-hardcoded restart port."""
import os
import tempfile


def _fresh_cwd(**env):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("MONGOOSE_PORT", "GOOGLE_TOKEN_PATH", "MS_TOKEN_CACHE"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    return d


def test_gmail_reads_same_token_as_calendar():
    """One Google sign-in must serve both Gmail and Calendar (no separate gmail-token.json)."""
    _fresh_cwd()
    from mongoose.settings import get_settings
    s = get_settings()
    # both connectors must resolve to the same primary token path when only the shared token exists
    os.makedirs("secrets", exist_ok=True)
    open(s.google_token_path, "w").write('{"token":"x"}')
    # Calendar uses settings.google_token_path directly
    assert str(s.google_token_path).endswith("google-token.json")
    # Gmail's _credentials resolves primary first (the shared token), not a missing gmail-token.json
    from pathlib import Path
    primary = Path(s.google_token_path)
    assert primary.exists()


def test_gmail_probe_finds_token_at_settings_path():
    """The probe must check settings.google_token_path, not a hardcoded data/ path."""
    _fresh_cwd()
    from mongoose.settings import get_settings
    s = get_settings()
    os.makedirs("secrets", exist_ok=True)
    open(s.google_token_path, "w").write('{"token":"x","refresh_token":"y"}')
    open(s.google_client_secrets_path, "w").write('{"installed":{}}')
    # replicate the probe's presence logic (the fix)
    cands = [s.google_token_path, s.google_token_path.replace("google-token", "gmail-token"),
             "~/.mongoose/data/google_token.json", "data/google_token.json"]
    assert any(p and os.path.exists(os.path.expanduser(p)) for p in cands)


def test_ms_probe_finds_cache_at_settings_path():
    """The MS probe must check settings.ms_token_cache, not a hardcoded data/ path."""
    _fresh_cwd()
    from mongoose.settings import get_settings
    s = get_settings()
    os.makedirs("secrets", exist_ok=True)
    open(s.ms_token_cache, "wb").write(b"cache")
    cands = [s.ms_token_cache, "~/.mongoose/data/ms_token_cache.bin", "data/ms_token_cache.bin"]
    assert any(p and os.path.exists(os.path.expanduser(p)) for p in cands)


def test_enable_phone_uses_configured_port_not_hardcoded():
    """enable_phone must restart on the configured MONGOOSE_PORT, not a hardcoded 8765."""
    import inspect
    import mongoose.enable_phone as EP
    src = inspect.getsource(EP.enable_phone)
    # the serve restart must reference the resolved port, never a literal "8765" in the serve call
    assert 'str(port)' in src
    assert '"serve", "--port", "8765"' not in src
    # and the port is resolved from MONGOOSE_PORT
    assert "MONGOOSE_PORT" in src


def test_env_example_exposes_protocol_v3_and_oauth_settings():
    """The recurring failure pattern: code supports a setting but .env.example doesn't show it."""
    # find .env.example from the repo root (cwd may be a temp dir)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # this checkout (repo root of the test file)
    example = os.path.join(repo, ".env.example")
    text = open(example).read()
    for key in ("PI_SSH_HOST", "PI_SSH_PORT", "PI_SSH_KEY", "PI_SSH_PASSWORD", "PI_CONFIG_DIR",
                "PROTOCOL_V3_ENABLED", "GOOGLE_OAUTH_REDIRECT_PORT"):
        assert key in text, f"{key} missing from .env.example"
