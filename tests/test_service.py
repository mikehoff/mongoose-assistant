"""Tests for the launchd service helper that runs the scheduler unattended. The
launchctl calls are macOS-only and not exercised here; these lock in the plist
generation (real absolute paths, KeepAlive, durable log location) and the status
shape, which is what makes the feature trustworthy rather than another hand-edit plist."""
import sys

import mongoose.service as svc


def test_plist_has_keepalive_and_runs_scheduler():
    p = svc.render_plist()
    assert "<key>KeepAlive</key>" in p
    assert "scheduler" in p
    assert svc.LABEL in p
    # RunAtLoad so it starts on login, ProcessType Background
    assert "<key>RunAtLoad</key>" in p
    assert "Background" in p


def test_plist_paths_are_absolute():
    import re
    p = svc.render_plist()
    # every StandardOut/Err path and the WorkingDirectory must be absolute (launchd
    # rejects relative paths)
    logs = re.findall(r"<string>([^<]*scheduler\.\w+\.log)</string>", p)
    assert logs and all(x.startswith("/") for x in logs)
    wd = re.search(r"<key>WorkingDirectory</key>\s*<string>([^<]+)</string>", p)
    assert wd and wd.group(1).startswith("/")


def test_plist_uses_real_interpreter_not_placeholder():
    p = svc.render_plist()
    # no stale hardcoded user path; uses this interpreter or the mongoose console script
    assert "/Users/alex/Downloads/mongoose-newer" not in p
    assert (sys.executable in p) or ("mongoose</string>" in p)


def test_logs_go_to_durable_dir_not_tmp():
    p = svc.render_plist()
    # durable: logs live under data/logs (next to the DB), not in a throwaway /tmp file
    # like the old brief plist (/tmp/mongoose_brief.log). We assert the data/logs
    # location rather than a bare "/tmp" substring, since a whole install can legally
    # sit under /tmp in CI.
    assert "logs/scheduler.out.log" in p and "logs/scheduler.err.log" in p
    assert "/tmp/mongoose_" not in p  # not the old ephemeral brief-style path


def test_write_plist_roundtrip(tmp_path, monkeypatch):
    # redirect the agents dir to a temp location
    monkeypatch.setattr(svc, "_agents_dir", lambda: tmp_path)
    p = svc.write_plist()
    assert p.exists()
    assert svc.LABEL in p.read_text()
    assert p.name == f"{svc.LABEL}.plist"


def test_status_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "_agents_dir", lambda: tmp_path)
    # no plist yet -> installed False
    st = svc.status()
    assert st["label"] == svc.LABEL
    assert st["installed"] is False
    assert "out_log" in st and "err_log" in st
    # write it -> installed True
    svc.write_plist()
    st2 = svc.status()
    assert st2["installed"] is True


def test_alpha_check_reports_unattended_state():
    """alpha-check's Scheduler line should mention the unattended/launchd state."""
    import asyncio
    from mongoose.release_check import release_check
    r = asyncio.run(release_check())
    sched = [ln for _, lines in r["sections"] for ln in lines if ln["label"] == "Scheduler"]
    assert sched
    assert "unattended" in sched[0]["detail"].lower() or "launchd" in sched[0]["detail"].lower()
