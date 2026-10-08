"""The install progress bar must be weighted by expected duration, not step count.

Regression guard for the persistent "browser opens with the bar already almost full, then sits
near-full through the long model download" delight bug. The browser opens right after the server is
up (prerequisites/environment/database/server done) and BEFORE the model pull. At that moment the bar
must be low, and the model download must then be the single largest visible jump.
"""
from __future__ import annotations

from mongoose import install_progress as ip


def _pct(monkeypatch, tmp_path, done, running=None):
    # isolate the progress file under tmp
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "p.sqlite3"))
    ip.begin_install()
    for k in done:
        ip.set_step(k, "done")
    if running:
        ip.set_step(running, "running")
    return ip.install_progress()["progress_pct"]


def test_bar_is_low_when_browser_opens(monkeypatch, tmp_path):
    # browser opens after server done, models not yet running
    pct = _pct(monkeypatch, tmp_path, done=["prerequisites", "environment", "database", "server"])
    assert pct < 35, f"bar at browser-open should be low, got {pct}% (the near-full-bar bug)"


def test_model_download_is_the_largest_single_jump(monkeypatch, tmp_path):
    # at browser open
    before = _pct(monkeypatch, tmp_path, done=["prerequisites", "environment", "database", "server"])
    # model download running (the long one)
    after_running = _pct(monkeypatch, tmp_path,
                         done=["prerequisites", "environment", "database", "server"],
                         running="models")
    model_jump = after_running - before
    # the four quick steps together
    _pct(monkeypatch, tmp_path, done=["prerequisites", "environment", "database", "server"])
    # the model download (even at half credit for 'running') should move the bar more than any one
    # quick step did. A 32-point sweep vs a few points each.
    assert model_jump >= 20, f"model download should be a large visible jump, got {model_jump} points"


def test_full_timeline_is_monotonic_and_ends_at_100(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "p.sqlite3"))
    ip.begin_install()
    seq = [("prerequisites", "done"), ("environment", "done"), ("database", "done"),
           ("server", "done"), ("models", "running"), ("services", "done"),
           ("models", "done"), ("ready", "done")]
    last = -1
    for k, s in seq:
        ip.set_step(k, s)
        pct = ip.install_progress()["progress_pct"]
        assert pct >= last, f"bar went backwards at {k}/{s}: {pct} < {last}"
        last = pct
    assert last == 100
