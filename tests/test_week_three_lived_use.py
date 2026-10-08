from __future__ import annotations

import asyncio
import sys
import types


def test_resumable_file_crawl_wraps_and_prunes_deleted_files(tmp_path, monkeypatch):
    from mongoose.db import init_db
    from mongoose.services.crawler import crawl_files, get_source
    from mongoose.services.file_index import search_files
    # This test exercises crawl/prune logic, not the low-disk skip path. Mock the disk guard so it is
    # not flaky on a low-disk CI/sandbox (the crawler short-circuits to skipped_low_disk under ~5GB free).
    from mongoose.services import crawler as _cr
    monkeypatch.setattr(_cr, "_disk_ok", lambda: True)

    init_db()
    root = tmp_path / "docs"
    root.mkdir()
    a = root / "a.txt"
    z = root / "z.txt"
    a.write_text("alpha original", encoding="utf-8")
    z.write_text("zulu keeper", encoding="utf-8")
    monkeypatch.setenv("MONGOOSE_CRAWLER_MAX_FILES_PER_RUN", "1")

    first = crawl_files(str(root), mode="fresh")
    assert first["more_remaining"] is True
    assert get_source(f"files:{root.resolve()}")["fresh_cursor"].endswith("a.txt")

    second = crawl_files(str(root), mode="fresh")
    assert second["more_remaining"] is False
    assert get_source(f"files:{root.resolve()}")["fresh_cursor"] in ("", None)

    # A later edit to an earlier alphabetic path must be seen on the next scheduled run.
    a.write_text("alpha updated week three", encoding="utf-8")
    third = crawl_files(str(root), mode="fresh")
    assert third["scanned"] >= 1
    assert search_files("updated", limit=5)

    # Deletions should also disappear once the crawler reaches the end of the tree.
    z.unlink()
    crawl_files(str(root), mode="fresh")
    crawl_files(str(root), mode="fresh")
    assert not search_files("zulu", limit=5)


def test_imap_crawler_failure_records_error_not_done(monkeypatch):
    from mongoose.db import init_db
    from mongoose.services import crawler

    init_db()
    # Mock the disk guard: this test asserts the IMAP-failure path records 'error', but the crawler
    # skips to 'skipped_low_disk' before reaching IMAP under low free disk (default ~5GB). Mocking keeps
    # the test about IMAP-failure handling regardless of the host's disk.
    monkeypatch.setattr(crawler, "_disk_ok", lambda: True)

    def fake_sync(mailbox, *, last_uid=0, max_messages=250):
        return {"ok": False, "error": "token expired"}

    monkeypatch.setattr("mongoose.services.email_index.sync_imap_incremental", fake_sync)
    result = crawler.crawl_imap_fresh("INBOX")
    assert result["status"] == "error"
    assert "token expired" in result["error"]
    source = crawler.get_source("imap:INBOX")
    assert source["last_status"] == "error"
    assert "token expired" in (source["last_error"] or "")


def test_email_sync_overlap_and_top_level_failure(monkeypatch):
    from mongoose.services import scheduler

    calls = {}
    monkeypatch.setattr(scheduler, "_provider_ready", lambda provider: provider == "google")

    def fake_sync_gmail(*, query, max_results):
        calls["query"] = query
        calls["max_results"] = max_results
        return {"ok": False, "error": "network down"}

    fake_mod = types.SimpleNamespace(sync_gmail=fake_sync_gmail)
    monkeypatch.setitem(sys.modules, "mongoose.connectors.gmail", fake_mod)
    result = asyncio.run(scheduler._job_sync_email())
    assert calls == {"query": "newer_than:30d", "max_results": 100}
    assert result["ok"] is False
    assert result["error"] == "all configured providers failed"


def test_liveness_uses_successful_sync_attempt_not_only_new_email_rows(monkeypatch, tmp_path):
    from mongoose.db import connect, init_db, utc_now_iso, set_app_state
    from mongoose.services import scheduler
    from mongoose import liveness as live
    import mongoose.settings as settings

    init_db()
    scheduler._ensure_table()
    token = tmp_path / "msal-token-cache.bin"
    token.write_text('{"AccessToken": {"x": "' + 'a' * 200 + '"}}', encoding="utf-8")  # realistic size: the plausibility guard rejects tiny placeholders
    monkeypatch.setenv("MS_TOKEN_CACHE", str(token))
    settings._SETTINGS = None
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO scheduler_jobs(name,last_run,last_status,last_detail) VALUES (?,?,?,?)",
            ("sync_email", utc_now_iso(), "ok", "quiet mailbox"),
        )
    set_app_state("scheduler_service_installed", "1")
    set_app_state("scheduler_heartbeat", utc_now_iso())

    rep = live.liveness()
    email = next(c for c in rep["checks"] if c["subsystem"] == "email")
    scheduler = next(c for c in rep["checks"] if c["subsystem"] == "scheduler")
    assert email["state"] == live.OK
    assert scheduler["state"] == live.OK


def test_web_push_exception_failures_eventually_prune(monkeypatch):
    from mongoose.services import web_push

    web_push.save_subscription({"endpoint": "https://push.example/1", "keys": {"p256dh": "x", "auth": "y"}})
    for _ in range(4):
        assert web_push._mark_subscription_failure("https://push.example/1") is False
    assert web_push._mark_subscription_failure("https://push.example/1") is True
    assert web_push.subscription_count() == 0
