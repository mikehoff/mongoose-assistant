"""Field-proven bug: on some Python builds (observed: Homebrew framework python@3.12), `ps -o
command=` for a running Mongoose server does not contain "mongoose" or "serve" at all, so the
old string-match ownership check silently returned False forever. That meant `mongoose stop`,
the watchdog, and every installer's version-mismatch teardown could never identify or kill the
process, so no update could ever actually take effect no matter how many times it was applied.
The fix asks the PID's own bound port for /health identity instead of trusting ps rendering."""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
import warnings

import pytest

warnings.filterwarnings("ignore")

pytestmark = pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not available")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def mongoose_process_with_opaque_argv(tmp_path, monkeypatch):
    """A real Mongoose server whose ps command line reveals nothing "mongoose"/"serve" related,
    reproducing the exact field failure. Yields (pid, port); cleans up after."""
    port = _free_port()
    db = tmp_path / "identity.sqlite3"
    launcher = tmp_path / "generic_launcher.py"
    launcher.write_text(
        "import sys, os\n"
        f'sys.argv = ["totally-generic-launcher", "serve", "--no-tls", "--port", "{port}"]\n'
        f"os.environ['MONGOOSE_DB_PATH'] = {str(db)!r}\n"
        "sys.path.insert(0, 'src')\n"
        "from mongoose.cli import main\n"
        "main()\n"
    )
    proc = subprocess.Popen([sys.executable, str(launcher)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=".")
    import urllib.request
    up = False
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if b'"name":"mongoose"' in r.read():
                    up = True
                    break
        except Exception:
            time.sleep(0.25)
    assert up, "test server did not come up"
    try:
        yield proc.pid, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_ps_string_match_alone_misses_the_opaque_process(mongoose_process_with_opaque_argv):
    from mongoose.cli import _command_looks_like_mongoose_server, _process_command_for_pid
    pid, _port = mongoose_process_with_opaque_argv
    cmd = _process_command_for_pid(pid)
    assert not _command_looks_like_mongoose_server(cmd), (
        "test setup no longer reproduces the field bug; ps command now contains a matching "
        f"substring ({cmd!r}), so this fixture must be adjusted to still be opaque"
    )


def test_port_based_identity_catches_it(mongoose_process_with_opaque_argv):
    from mongoose.cli import _pid_answers_as_mongoose, _pid_looks_like_mongoose_server
    pid, _port = mongoose_process_with_opaque_argv
    assert _pid_answers_as_mongoose(pid) is True
    assert _pid_looks_like_mongoose_server(pid) is True


def test_unrelated_process_is_not_falsely_identified():
    from mongoose.cli import _pid_looks_like_mongoose_server
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.3)
    try:
        assert _pid_looks_like_mongoose_server(proc.pid) is False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_bootstrap_kill_loop_uses_port_identity_not_only_ps_string():
    """The bash teardown's own comment already promised /health-based identity proof; this
    asserts the kill decision itself actually consults it, not just the header comment."""
    import pathlib
    t = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_macos.sh").read_text()
    seg = t[t.index("force_free_port_of_mongoose"):t.index("# Clear the port")]
    assert "mongoose_is_on_port" in seg
    assert "port_says_mongoose" in seg
    assert '_pid_is_mongoose_serve "$pid" || [ "$port_says_mongoose" = "1" ]' in seg
