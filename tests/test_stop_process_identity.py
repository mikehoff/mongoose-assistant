"""A runtime ownership claim must prove ownership before acting. `mongoose stop` and the installers
must only terminate actual Mongoose servers , never a random process that happens to hold the port
or be pointed at by a stale pidfile. (Concurrent claim-integrity audit finding, ported.)"""
import os
import subprocess
import sys
import time
from pathlib import Path


def test_classifier_accepts_real_mongoose_commands():
    from mongoose.cli import _command_looks_like_mongoose_server as ok
    assert ok("mongoose serve --host 127.0.0.1 --port 8765")
    assert ok("/usr/bin/python -m mongoose.cli serve --port 8765")
    assert ok(".venv/bin/uvicorn mongoose.app:app --host 127.0.0.1")
    assert ok("/Users/x/app/.venv/bin/mongoose serve")


def test_classifier_rejects_unrelated_commands():
    from mongoose.cli import _command_looks_like_mongoose_server as ok
    assert not ok("python -m http.server 8765")
    assert not ok("sleep 300")
    assert not ok("/usr/bin/python some_other_script.py")
    assert not ok("nginx: worker process")
    assert not ok("")  # unknown command
    # a python process that merely mentions mongoose but isn't serving
    assert not ok("python -m mongoose.cli doctor")


def test_stop_refuses_to_kill_non_mongoose_pid(tmp_path):
    """A stale pidfile pointing at an innocent process must NOT cause that process to be killed."""
    d = tmp_path
    os.environ_backup = dict(os.environ)
    env = dict(os.environ, PYTHONPATH="src", MONGOOSE_DATA_DIR=str(d),
               MONGOOSE_DB_PATH=str(d / "m.sqlite3"))
    # init db
    subprocess.run([sys.executable, "-m", "mongoose.cli", "init-db"], env=env,
                   capture_output=True)
    victim = subprocess.Popen(["sleep", "30"])
    try:
        (d / "mongoose-server.pid").write_text(f"{victim.pid}\n8765\n")
        r = subprocess.run([sys.executable, "-m", "mongoose.cli", "stop"], env=env,
                           capture_output=True, text=True)
        time.sleep(0.4)
        assert victim.poll() is None, "stop killed an innocent (non-Mongoose) process"
        assert "refusing to kill" in r.stdout.lower()
    finally:
        if victim.poll() is None:
            victim.kill()


def test_install_scripts_have_no_raw_kill_loops():
    """The installer and bootstrap must not raw-kill arbitrary port holders. Killing a listener is
    allowed ONLY when the process is first confirmed to be a Mongoose server (an ownership claim),
    never merely because it occupies the port. Two ownership proofs are accepted:
      (a) ps -o command shows a mongoose serve / uvicorn-mongoose.app command, OR
      (b) /health on the port returns "name":"mongoose" (a STRONGER proof , a non-mongoose
          process cannot answer it). The kill loops are guarded by (b) and re-check it before each
          kill, which is why the narrow ps-pattern gate (which let launch-agent/path-variant old
          servers survive) was removed."""
    root = Path(__file__).resolve().parent.parent
    for name in ("Install Mongoose.command", "scripts/bootstrap_macos.sh"):
        text = (root / name).read_text()
        if "lsof -ti" not in text:
            continue
        lines = text.splitlines()
        for li, line in enumerate(lines):
            if "lsof -ti" not in line:
                continue
            # Ignore lsof inside an ECHOED manual instruction (help text telling the user how to kill
            # it themselves) , that's not an executed kill.
            stripped = line.lstrip()
            if stripped.startswith("echo") or stripped.startswith("printf"):
                continue
            # The vicinity = this line plus the next ~20 lines (the kill loop + its guards).
            window = "\n".join(lines[li: li + 22])
            kills_nearby = "kill" in window
            ps_guard = ("ps -p" in window or "ps -o command" in window) and "mongoose" in window
            # /health returning the exact mongoose JSON is a STRONGER ownership proof than a ps
            # pattern (a non-mongoose process can't answer it); accept it, or the helper that wraps it.
            health_guard = ('"name":"mongoose"' in window or "mongoose_is_on_port" in window
                            or "force_free_port_of_mongoose" in window)
            # Identity proof by pidfile/cmdline/cwd: kills only a listener we can PROVE is THIS install's
            # Mongoose (matches data/mongoose-server.pid, or ps shows `mongoose serve` under this repo
            # path/cwd), even when /health is dead. This is what rescues a wedged server that no longer
            # answers /health, and is at least as strong as the ps-pattern. Accept it as a guard.
            identity_guard = ("pid_is_our_mongoose" in window or "_pid_is_our_mongoose" in window
                              or "_kill_owned_port_holders" in window
                              or "force_free_port_by_identity" in window)
            assert (not kills_nearby) or ps_guard or health_guard or identity_guard, \
                f"{name} appears to raw-kill port holders without confirming they are Mongoose"


def test_bootstrap_starts_via_mongoose_serve():
    """Starting via `mongoose serve` (not bare uvicorn) is what writes a pidfile and gives future
    restarts a proper ownership handle."""
    root = Path(__file__).resolve().parent.parent
    boot = (root / "scripts/bootstrap_macos.sh").read_text()
    assert "mongoose serve" in boot
    # the server start line should not be bare uvicorn
    start_lines = [ln for ln in boot.splitlines() if "nohup" in ln and "serve" in ln or ("uvicorn" in ln and "nohup" in ln)]
    for ln in start_lines:
        assert "mongoose serve" in ln, f"bootstrap starts via bare uvicorn: {ln}"
