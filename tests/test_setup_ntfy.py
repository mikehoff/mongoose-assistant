"""Native ntfy setup: the local-first, Docker-free path to reliable phone push. The local server
must bind to loopback (content stays on the machine), and .env must get the local URL + a topic."""
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate():
    import mongoose.setup_wizard as SW
    saved = {k: os.environ.get(k) for k in ("NTFY_URL", "NTFY_TOPIC")}
    saved_path = SW.ENV_PATH
    for k in ("NTFY_URL", "NTFY_TOPIC"):
        os.environ.pop(k, None)
    try:
        yield
    finally:
        SW.ENV_PATH = saved_path
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    from mongoose.db import init_db
    init_db()
    return d


def test_local_server_config_binds_loopback_only():
    _fresh()
    import mongoose.setup_ntfy as S
    cfg = S._write_server_config()
    text = cfg.read_text()
    # local-first: the server must listen only on 127.0.0.1, never 0.0.0.0 or a public bind
    assert "127.0.0.1:8090" in text
    assert "0.0.0.0" not in text


def test_env_wired_to_local_url_and_topic():
    d = _fresh()
    import mongoose.setup_wizard as SW
    envf = Path(d) / ".env"
    SW.ENV_PATH = envf
    envf.write_text("")
    import mongoose.setup_ntfy as S
    S._update_env("mongoose-abc123")
    text = envf.read_text()
    assert "NTFY_URL=http://127.0.0.1:8090" in text
    assert "NTFY_TOPIC=mongoose-abc123" in text
    # immediately usable by the running process
    assert os.environ.get("NTFY_URL") == "http://127.0.0.1:8090"


def test_existing_topic_is_preserved():
    d = _fresh()
    import mongoose.setup_wizard as SW
    envf = Path(d) / ".env"
    SW.ENV_PATH = envf
    envf.write_text("NTFY_TOPIC=already-subscribed\n")
    import mongoose.setup_ntfy as S
    # re-running setup must keep an existing topic so the phone app stays subscribed
    assert S._existing_topic() == "already-subscribed"


def test_setup_ntfy_command_is_wired():
    d = _fresh()
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "mongoose.cli", "setup-ntfy", "--yes"],
                       env=dict(os.environ, PYTHONPATH="src", MONGOOSE_DATA_DIR=d),
                       capture_output=True, text=True)
    # recognised command (not an argparse error); on Linux it prints the macOS guidance
    assert "invalid choice" not in r.stderr
    assert r.returncode in (0, 1)
