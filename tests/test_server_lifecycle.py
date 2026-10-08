"""The installer must restart into the new version: a pidfile is written on serve, `mongoose stop`
uses it, and the cockpit is served no-cache so a browser can't show a stale page after an update."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()
    return d


def test_stop_command_exists():
    """The stop subcommand must be wired (the installer relies on it)."""
    d = _fresh()
    import subprocess
    import sys
    # 'mongoose stop' must be a recognised command (not an argparse error)
    r = subprocess.run([sys.executable, "-m", "mongoose.cli", "stop"],
                       env=dict(os.environ, PYTHONPATH="src", MONGOOSE_DATA_DIR=d),
                       capture_output=True, text=True)
    assert r.returncode == 0  # known command, runs cleanly
    assert "invalid choice" not in r.stderr


def test_cockpit_served_no_cache():
    _fresh()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    for path in ("/ui", "/welcome"):
        r = c.get(path)
        assert r.status_code == 200
        cc = r.headers.get("cache-control", "")
        assert "no-cache" in cc and "no-store" in cc, f"{path} is cacheable: {cc}"


def test_health_reports_version():
    """The installer reads /health to detect a version mismatch; it must carry the version."""
    _fresh()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    from mongoose import __version__
    c = TestClient(A.app)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json().get("version") == __version__


def test_stop_is_graceful_with_no_server():
    d = _fresh()
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "mongoose.cli", "stop"],
                       env=dict(os.environ, PYTHONPATH="src", MONGOOSE_DATA_DIR=d),
                       capture_output=True, text=True)
    # exits cleanly and says nothing's running (no traceback)
    assert r.returncode == 0
    assert "Traceback" not in r.stderr
