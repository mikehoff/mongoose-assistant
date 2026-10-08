"""The update pipeline's live-handover contract, from the field failure where updates were
"applied" for weeks while the port kept serving 1.21.0: update.sh swapped files but never
stopped/started/verified a server, and the guided updater verified coherence on DISK only.
Contract now: every update path ends with the live port serving the new version, verified,
or fails loudly."""
from __future__ import annotations

import json
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import warnings

import pytest

warnings.filterwarnings("ignore")

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_health(port: int, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.25)
    return None


def test_update_sh_wires_handover_into_update_and_rollback():
    t = (ROOT / "scripts" / "update.sh").read_text()
    for fn in ("mongoose_teardown_port()", "mongoose_start_and_verify()",
               "_update_port_is_mongoose()", "_update_live_version()"):
        assert fn in t, fn
    # update flow calls both, with the NEW version as the expectation
    assert 'mongoose_start_and_verify "$UPD_PORT" "$NEW_VER"' in t
    assert 'mongoose_teardown_port "$UPD_PORT"' in t
    # rollback flow re-verifies the restored version too
    assert 'mongoose_start_and_verify "$RB_PORT" "$RB_VER"' in t
    # the success message states the verified live version, not just disk state
    assert "has been verified as" in t


def test_guided_updater_fails_without_live_match():
    """verify_live_version must refuse a port serving the wrong (or no) version."""
    from mongoose.updater import verify_live_version
    dead = _free_port()
    r = verify_live_version(expected="1.0.0", port=dead, timeout=2.0)
    assert r["ok"] is False and r["live_version"] is None


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not available")
def test_full_handover_replaces_opaque_old_server(tmp_path):
    """End to end: an old-version server whose ps command line cannot be string-matched holds
    the port; teardown removes it by port identity; start_and_verify brings up THIS tree's
    version and verifies it live; verify_live_version then agrees."""
    port = _free_port()
    db = tmp_path / "old.sqlite3"
    launcher = tmp_path / "opaque_old.py"
    launcher.write_text(
        "import sys, os\n"
        f'sys.argv=["totally-generic-launcher","serve","--no-tls","--port","{port}"]\n'
        f"os.environ['MONGOOSE_DB_PATH']={str(db)!r}\n"
        "sys.path.insert(0,'src')\n"
        "import mongoose; mongoose.__version__='1.21.0'\n"
        "import mongoose.app as _a; _a.__version__='1.21.0'\n"
        "from mongoose.cli import main; main()\n"
    )
    old = subprocess.Popen([sys.executable, str(launcher)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           cwd=str(ROOT), start_new_session=True)
    try:
        up = _wait_health(port)
        assert up and up.get("version") == "1.21.0", up

        expected = None
        for line in (ROOT / "pyproject.toml").read_text().splitlines():
            if line.startswith("version"):
                expected = line.split('"')[1]
                break
        assert expected

        import os as _os
        env = dict(_os.environ, UPDATE_SH_LIB_ONLY="1", MONGOOSE_TLS="off",
                   MONGOOSE_DB_PATH=str(tmp_path / "new.sqlite3"), PYTHONPATH="src")
        r = subprocess.run(
            ["bash", "-c",
             "set -euo pipefail; source scripts/update.sh; "
             f"mongoose_teardown_port {port} {sys.executable} && "
             f"mongoose_start_and_verify {port} {expected} {sys.executable}"],
            capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"Live server verified: version {expected}" in r.stdout

        time.sleep(0.3)
        assert old.poll() is not None, "opaque old server survived teardown"

        from mongoose.updater import verify_live_version
        v = verify_live_version(expected=expected, port=port, timeout=10.0)
        assert v["ok"] is True and v["live_version"] == expected
    finally:
        try:
            old.terminate()
        except Exception:
            pass
        subprocess.run(["bash", "-c",
                        f"export UPDATE_SH_LIB_ONLY=1; source scripts/update.sh; "
                        f"mongoose_teardown_port {port} {sys.executable}"],
                       capture_output=True, cwd=str(ROOT), timeout=60)


def test_wrong_version_on_port_is_refused(tmp_path):
    """start_and_verify must not bless a port serving a different version than expected."""
    import os as _os
    port = _free_port()
    env = dict(_os.environ, UPDATE_SH_LIB_ONLY="1", MONGOOSE_TLS="off",
               MONGOOSE_DB_PATH=str(tmp_path / "wrong.sqlite3"), PYTHONPATH="src")
    r = subprocess.run(
        ["bash", "-c",
         "set -euo pipefail; source scripts/update.sh; "
         f"mongoose_start_and_verify {port} 9.9.9 {sys.executable}"],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=150)
    assert r.returncode != 0
    assert "FAILED VERIFICATION" in r.stdout
    subprocess.run(["bash", "-c",
                    f"export UPDATE_SH_LIB_ONLY=1; source scripts/update.sh; "
                    f"mongoose_teardown_port {port} {sys.executable}"],
                   capture_output=True, cwd=str(ROOT), timeout=60)
