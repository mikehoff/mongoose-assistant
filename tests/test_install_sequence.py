"""The install screen must NOT open with everything already ticked. The fix: the server (and browser)
come up BEFORE the long model download, so 'Downloading the local brain' is the live step when the page
opens. These tests lock in the step ordering + the bootstrap sequence."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    from mongoose.db import init_db
    init_db()


def test_models_is_the_last_real_step_after_server():
    from mongoose.install_progress import STEPS
    keys = [k for k, _ in STEPS]
    # server comes up before optional services/model pull , that's the whole fix: the browser
    # opens on a coherent, still-in-progress journey rather than a stale log wall.
    assert keys.index("server") < keys.index("services") < keys.index("models")
    # models is the last HEAVY step. After it come the quick readiness/protection steps (smoke test,
    # then the best-effort watchdog), then the terminal 'ready' marker.
    assert keys.index("models") < keys.index("smoke") < keys.index("watchdog") < keys.index("ready")
    assert keys[-1] == "ready"
    # nothing heavy after models: smoke and watchdog are fast, models is the last long pull
    assert keys.index("models") == keys.index("smoke") - 1


def test_browser_open_state_shows_brain_downloading_not_all_done():
    _fresh()
    from mongoose import install_progress as ip
    ip.begin_install()
    # the state the browser sees the moment it opens (server just came up):
    for step in ["prerequisites", "environment", "database", "services", "server"]:
        ip.set_step(step, "done")
    ip.set_step("models", "running")
    prog = ip.install_progress()
    by_key = {s["key"]: s["status"] for s in prog["steps"]}
    # the long step is actively running, NOT done , so the user sees real progress
    assert by_key["models"] == "running"
    assert by_key["server"] == "done"
    # not everything is ticked
    assert any(s["status"] == "running" for s in prog["steps"])


def test_bootstrap_pulls_models_after_server_is_up():
    """Guard the bootstrap ordering: the model pull must come after 'server done', not before."""
    import pathlib
    bootstrap = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_macos.sh"
    text = bootstrap.read_text()
    server_done = text.index('_install-step server done')
    models_running = text.index('_install-step models running')
    assert server_done < models_running, "models must be pulled AFTER the server starts (so the browser opens first)"
    # and there's no EARLIER models pull left behind before the server
    pre_server = text[:server_done]
    assert "_install-step models running" not in pre_server
