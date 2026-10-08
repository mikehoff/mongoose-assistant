from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from email.message import EmailMessage


def test_imap_crawler_resets_when_uidvalidity_changes(monkeypatch):
    from mongoose.db import init_db
    from mongoose.services import crawler

    init_db()
    # Mock the disk guard so this test exercises UIDVALIDITY-reset logic rather than the low-disk skip
    # path (the crawler short-circuits to skipped_low_disk under ~5GB free, which is common on CI).
    monkeypatch.setattr(crawler, "_disk_ok", lambda: True)
    calls: list[int] = []

    def fake_sync(mailbox, *, last_uid=0, max_messages=250):
        calls.append(last_uid)
        if len(calls) == 1:
            return {"ok": True, "count": 2, "highest_uid": 100, "uidvalidity": "A", "more": False}
        if len(calls) == 2:
            # Server/mailbox was rebuilt: same mailbox, new UIDVALIDITY. A stale cursor of 100
            # would otherwise make Mongoose believe there is no new mail forever.
            return {"ok": True, "count": 0, "highest_uid": 100, "uidvalidity": "B", "more": False}
        return {"ok": True, "count": 3, "highest_uid": 3, "uidvalidity": "B", "more": False}

    monkeypatch.setattr("mongoose.services.email_index.sync_imap_incremental", fake_sync)
    first = crawler.crawl_imap_fresh("INBOX")
    assert first["status"] == "done"
    cursor1 = json.loads(crawler.get_source("imap:INBOX")["fresh_cursor"])
    assert cursor1 == {"uid": 100, "uidvalidity": "A"}

    second = crawler.crawl_imap_fresh("INBOX")
    assert second["status"] == "done"
    assert calls == [0, 100, 0]
    cursor2 = json.loads(crawler.get_source("imap:INBOX")["fresh_cursor"])
    assert cursor2 == {"uid": 3, "uidvalidity": "B"}


def test_voicemail_reingest_does_not_retranscribe_same_imap_audio(monkeypatch):
    from mongoose.db import init_db
    from mongoose.services import voicemail as VM

    init_db()
    msg = EmailMessage()
    msg["From"] = "VoIP Voicemail <voicemail@voip.example>"
    msg["Subject"] = "New message in mailbox for master group of 30000000 from 07700900123"
    msg["Date"] = "Mon, 22 Jun 2026 03:54:44 +0000"
    body = (
        "You have a new voice mail message in the mailbox for master group of account "
        "30000000 from 07700900123 on Monday, June 22, 2026 at 3:54:44 AM."
    )
    attachments = [{
        "id": 10,
        "filename": "message-30000000g0_2fb7242e082749b9a5107dd849ccd04d.wav",
        "mime_type": "audio/x-wav",
        "size_bytes": 123,
        "payload_b64": "UklGRg==",
    }]
    calls = {"n": 0}

    def fake_transcribe(payload_b64, filename):
        calls["n"] += 1
        return "please call me back", "done", "local_command", None

    monkeypatch.setattr(VM, "_transcribe_audio", fake_transcribe)
    r1 = VM.index_voicemail_from_email(
        msg=msg, body=body, source="imap", source_message_id="<vm-1@example>",
        email_id=1, mailbox="INBOX", attachments=attachments,
    )
    r2 = VM.index_voicemail_from_email(
        msg=msg, body=body, source="imap", source_message_id="<vm-1@example>",
        email_id=1, mailbox="INBOX", attachments=attachments,
    )
    assert r1["ok"] and r2["ok"]
    assert r2["action"] == "merge"
    assert calls["n"] == 1


def test_file_index_truncates_huge_plain_text(monkeypatch, tmp_path):
    from mongoose.services.file_index import index_files, search_files

    monkeypatch.setenv("MONGOOSE_FILE_INDEX_MAX_TEXT_BYTES", "65536")
    root = tmp_path / "docs"
    root.mkdir()
    big = root / "large-log.txt"
    big.write_text("early searchable phrase\n" + ("x" * 70000) + "\nlate secret phrase", encoding="utf-8")

    res = index_files(str(root), limit=10)
    assert res["indexed"] == 1
    assert search_files("early searchable")
    assert search_files("late secret") == []


def test_surpriser_pushes_only_to_owner_profiles(monkeypatch):
    from mongoose.db import init_db
    from mongoose.services import surpriser as S
    from mongoose.services import notifications as N

    init_db()
    monkeypatch.setattr(S, "_now_local", lambda: datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc))

    async def fake_candidates():
        return [{
            "kind": "email_meets_calendar",
            "text": "Email from Chris relates to your 2pm meeting. Want me to pull it up?",
            "why": "calendar/email correlation",
            "score": 0.9,
            "signature": "sig-owner-only",
            "family": "email_meets_calendar",
            "imminent": False,
        }]

    seen = {}

    async def fake_send_to_phone(*args, **kwargs):
        seen["kwargs"] = kwargs
        return {"sent": True, "channel": "web_push"}

    monkeypatch.setattr(S, "candidate_suggestions", fake_candidates)
    monkeypatch.setattr(N, "send_to_phone", fake_send_to_phone)
    res = asyncio.run(S.run_surpriser_cycle())
    assert res["pushed"] is True
    assert seen["kwargs"]["target_device_profiles"] == ("primary_phone", "home_tablet")


def test_surpriser_schema_migrates_feedback_column():
    from mongoose.db import connect, init_db
    from mongoose.services import surpriser as S

    init_db()
    with connect() as con:
        con.execute("DROP TABLE IF EXISTS surpriser_log")
        con.execute(
            "CREATE TABLE surpriser_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, signature TEXT NOT NULL, kind TEXT, text TEXT, "
            "score REAL, created_at TEXT NOT NULL, surfaced INTEGER NOT NULL DEFAULT 1)"
        )
        S._ensure_table(con)
        cols = {r[1] for r in con.execute("PRAGMA table_info(surpriser_log)")}
    assert {"source", "family", "feedback"}.issubset(cols)
