from pathlib import Path

import pytest

from mongoose.setup_wizard import write_env, _read_env, integration_status, INTEGRATIONS


@pytest.fixture(autouse=True)
def _reset_env_path():
    """These tests rely on write_env/_read_env using ./.env in the (monkeypatched) cwd. Another test
    file may have left setup_wizard.ENV_PATH pointing elsewhere; snapshot and restore it so this file
    is order-independent."""
    import mongoose.setup_wizard as SW
    saved = SW.ENV_PATH
    SW.ENV_PATH = Path(".env")
    try:
        yield
    finally:
        SW.ENV_PATH = saved


def test_write_env_preserves_and_updates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = Path(".env")
    env.write_text("OLLAMA_KEEP_ALIVE=0\n# a comment\nHA_URL=old\n")
    write_env({"HA_URL": "newurl", "HA_TOKEN": "tok"})
    text = env.read_text()
    assert "OLLAMA_KEEP_ALIVE=0" in text   # preserved
    assert "# a comment" in text            # comment preserved
    assert "HA_URL=newurl" in text          # updated, not duplicated
    assert text.count("HA_URL=") == 1
    assert "HA_TOKEN=tok" in text           # appended


def test_read_env_ignores_comments_and_blanks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("# c\n\nA=1\nB=two words\n")
    parsed = _read_env()
    assert parsed == {"A": "1", "B": "two words"}


def test_every_integration_has_fields_and_help():
    for integ in INTEGRATIONS:
        assert integ.name and integ.help_text and integ.fields


def test_integration_status_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("HA_URL=http://x\nHA_TOKEN=t\n")
    # clear any real env that might interfere
    for k in ("HA_URL", "HA_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    status = integration_status()
    slugs = {s["slug"] for s in status}
    assert {"home_assistant", "google_calendar", "gemini", "microsoft", "imap", "anthropic", "openai", "alexa"} <= slugs
    ha = next(s for s in status if s["slug"] == "home_assistant")
    assert ha["configured"] is True
