"""Tests for the install-progress experience: the live screen that replaces the log-wall +
health-URL finish with a clean per-step progress view that ends at the phone QR."""
import os
import tempfile


def _fresh():
    os.environ["MONGOOSE_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = tempfile.mktemp(suffix=".sqlite3")
    from mongoose.db import init_db
    init_db()


def test_progress_starts_empty_then_advances():
    _fresh()
    from mongoose.install_progress import begin_install, set_step, install_progress
    begin_install()
    p = install_progress()
    assert p["progress_pct"] == 0
    assert all(s["status"] == "pending" for s in p["steps"])
    set_step("prerequisites", "done")
    set_step("environment", "done")
    p = install_progress()
    assert p["progress_pct"] > 0
    assert not p["ready"]


def test_progress_reaches_ready():
    _fresh()
    from mongoose.install_progress import begin_install, set_step, install_progress
    begin_install()
    for k in ("prerequisites", "environment", "database", "models", "services", "server"):
        set_step(k, "done")
    set_step("ready", "done")
    p = install_progress()
    assert p["ready"] is True
    assert p["progress_pct"] == 100


def test_skipped_step_counts_as_progress_not_failure():
    _fresh()
    from mongoose.install_progress import begin_install, set_step, install_progress
    begin_install()
    set_step("services", "skipped")
    p = install_progress()
    assert p["failed"] is False
    # skipped contributes to progress (it's a legitimate optional skip)
    assert p["progress_pct"] > 0


def test_failed_step_is_surfaced():
    _fresh()
    from mongoose.install_progress import begin_install, set_step, install_progress
    begin_install()
    set_step("models", "failed", "download failed")
    p = install_progress()
    assert p["failed"] is True


def test_install_page_and_endpoints_served():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    page = c.get("/install").text
    assert "Mongoose" in page and "/install/progress" in page
    prog = c.get("/install/progress").json()
    assert "steps" in prog and "progress_pct" in prog


def test_pair_code_available_during_first_run_window():
    _fresh()
    from mongoose.install_progress import set_step
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    set_step("ready", "done")
    # loopback (testclient) + nothing paired -> pairing code is offered to close the flow
    r = c.post("/install/pair-code")
    assert r.status_code == 200
    body = r.json()
    assert "code" in body


def test_pair_code_blocked_once_a_device_is_paired(monkeypatch):
    _fresh()
    from mongoose.install_progress import set_step
    set_step("ready", "done")
    # once any device is paired, pairing moves to the authenticated cockpit Devices tab
    import mongoose.app as A
    monkeypatch.setattr("mongoose.onboarding._paired_count", lambda: 1)
    from fastapi.testclient import TestClient
    c = TestClient(A.app)
    r = c.post("/install/pair-code")
    assert r.status_code == 403


def test_browser_open_at_server_done_is_not_almost_finished():
    """Regression for the human install journey: the browser opens as soon as the web server is ready,
    before the long model pull. At that point the progress bar must not imply 'nearly done'."""
    _fresh()
    from mongoose.install_progress import begin_install, set_step, install_progress
    begin_install()
    for k in ("prerequisites", "environment", "database", "services", "server"):
        set_step(k, "done")
    p = install_progress()
    assert 30 <= p["progress_pct"] <= 50
    assert p["ready"] is False
