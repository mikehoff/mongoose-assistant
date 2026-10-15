"""Scheduled letters: nothing goes unarmed or early, each letter goes once, a missed window is held, not sent late."""
from datetime import datetime

import pytest

from mongoose.services import rtm_scheduled_letters as SL

L = SL.LONDON


@pytest.fixture()
def batch(tmp_path, monkeypatch):
    store = {}
    monkeypatch.setattr(SL, "get_app_state", lambda k, d="": store.get(k, d))
    monkeypatch.setattr(SL, "set_app_state", lambda k, v: store.__setitem__(k, v))
    letters = []
    for fid in ("1ECF3", "3ECF8"):
        p = tmp_path / f"{fid}.pdf"
        p.write_bytes(b"%PDF-1.4")
        letters.append({"flat_id": fid, "pdf_path": str(p), "subject": f"Notice required for {fid}",
                        "document_number": f"LETTER/{fid}", "attachment_name": f"{fid}.pdf"})
    return letters


def sender_ok(calls):
    def s(flat_id, pdf_path, **kw):
        calls.append((flat_id, kw))
        return {"ok": True, "email": "x@example.com", "attached": 1}
    return s


def test_unarmed_batch_never_sends(batch):
    calls = []
    SL.schedule(batch, "2026-10-15T09:00:00")
    SL.run_due(datetime(2026, 10, 15, 9, 1, tzinfo=L), sender=sender_ok(calls))
    assert calls == []


def test_armed_batch_sends_at_nine_london_time_and_only_once(batch):
    calls, notes = [], []
    r = SL.schedule(batch, "2026-10-15T09:00:00", armed=True, label="clause 10.7 notices")
    SL.run_due(datetime(2026, 10, 15, 8, 59, tzinfo=L), sender=sender_ok(calls))
    assert calls == []
    SL.run_due(datetime(2026, 10, 15, 9, 0, tzinfo=L), sender=sender_ok(calls), notify=lambda t, b: notes.append(t))
    assert [c[0] for c in calls] == ["1ECF3", "3ECF8"] and all(c[1]["confirm"] is True for c in calls)
    SL.run_due(datetime(2026, 10, 15, 9, 5, tzinfo=L), sender=sender_ok(calls))
    assert len(calls) == 2
    assert SL.status()[0]["state"] == "sent" and notes == ["Letters sent: clause 10.7 notices"]
    assert r["send_at"].endswith("+01:00")   # 9am BST


def test_a_failed_letter_is_reported_and_not_retried_blindly(batch):
    calls = []

    def flaky(flat_id, pdf_path, **kw):
        calls.append(flat_id)
        return {"ok": flat_id != "3ECF8", "error": None if flat_id != "3ECF8" else "smtp"}
    SL.schedule(batch, "2026-10-15T09:00:00", armed=True)
    out = SL.run_due(datetime(2026, 10, 15, 9, 0, tzinfo=L), sender=flaky)
    assert out == {"ok": False, "sent": 1, "failed": 1, "held": 0}
    SL.run_due(datetime(2026, 10, 15, 9, 2, tzinfo=L), sender=flaky)
    assert calls == ["1ECF3", "3ECF8"] and SL.status()[0]["state"] == "partly_sent"


def test_missed_window_is_held_not_sent_late(batch):
    calls, notes = [], []
    SL.schedule(batch, "2026-10-15T09:00:00", armed=True)
    SL.run_due(datetime(2026, 10, 15, 16, 0, tzinfo=L), sender=sender_ok(calls), notify=lambda t, b: notes.append(t))
    assert calls == [] and SL.status()[0]["state"] == "held_late" and notes and "not sent" in notes[0]


def test_cancel_stops_it(batch):
    calls = []
    b = SL.schedule(batch, "2026-10-15T09:00:00", armed=True)["batch"]
    assert SL.cancel(b)["ok"]
    SL.run_due(datetime(2026, 10, 15, 9, 0, tzinfo=L), sender=sender_ok(calls))
    assert calls == []


def test_missing_pdf_is_refused_up_front(batch):
    batch[0]["pdf_path"] = "/nope.pdf"
    assert not SL.schedule(batch, "2026-10-15T09:00:00")["ok"]
