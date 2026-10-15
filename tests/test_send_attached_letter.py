"""Letters emailed as PDF attachments through the portal (send_attached_letter).

A letter reaches a leaseholder only after a human has read a test: a real send needs confirm=True, a test
goes only to the RTM's own address and copies nobody, and a portal that sends the cover note without the
attachment (a plugin older than 1.1.14) is reported as a failure, never as a letter sent.
"""
import pytest

from mongoose.services import rtm_portal as P


@pytest.fixture()
def wp(monkeypatch, tmp_path):
    calls = []
    replies = {"/wp-json/example-court/v1/sync/demand-pdf": {"ok": True},
               "/wp-json/example-court/v1/portal/send-letter": {"ok": True, "data": {"ok": True, "sent": True,
                                                                "email": "lh@example.com", "attached": 1}}}

    def fake(method, path, json_body=None, timeout=0):
        calls.append((path, json_body))
        return dict(replies[path])
    monkeypatch.setattr(P, "_wp_call", fake)
    monkeypatch.setattr(P, "_record_sync_run", lambda *a, **k: None)
    import mongoose.services.rtm_management as M
    monkeypatch.setattr(M, "correspondence_cc", lambda fid: ["agent@example.com"])
    pdf = tmp_path / "Notice.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    return calls, replies, pdf


def test_a_real_send_needs_confirmation(wp):
    calls, _, pdf = wp
    r = P.send_attached_letter("1ECF3", str(pdf), subject="Notice", document_number="LETTER/X")
    assert r["reason"] == "confirm_required" and not calls


def test_test_send_goes_nowhere_but_the_rtm_and_copies_nobody(wp):
    calls, _, pdf = wp
    r = P.send_attached_letter("1ECF3", str(pdf), subject="Notice", document_number="LETTER/X", test=True)
    assert r["ok"] and r["test"] and r["attached"] == 1 and r["email"] == "lh@example.com"
    push, send = calls
    assert push[1]["flat_id"] == "1ECF3" and push[1]["pdf_key"] == send[1]["pdf_key"]
    assert send[1]["test"] is True and send[1]["cc"] == "" and send[1]["signoff"] is False
    assert "Please find important communication attached from your management company." in send[1]["body_html"]
    assert "Director, Example Court RTM" in send[1]["body_html"]


def test_confirmed_send_copies_the_usual_parties(wp):
    calls, _, pdf = wp
    r = P.send_attached_letter("1ECF3", str(pdf), subject="Notice", document_number="LETTER/X", confirm=True,
                               attachment_name="Notice_of_subletting_1ECF3.pdf")
    assert r["ok"] and r["sent"] and r["cc"] == ["agent@example.com"]
    assert calls[1][1]["attachment_name"] == "Notice_of_subletting_1ECF3.pdf"


def test_test_send_without_attachment_is_also_a_failure(wp):
    calls, replies, pdf = wp
    replies["/wp-json/example-court/v1/portal/send-letter"] = {"ok": True, "data": {"ok": True, "sent": True}}
    assert not P.send_attached_letter("1ECF3", str(pdf), subject="N", document_number="L", test=True)["ok"]


def test_old_plugin_sending_without_the_attachment_is_a_failure(wp):
    calls, replies, pdf = wp
    replies["/wp-json/example-court/v1/portal/send-letter"] = {"ok": True, "data": {"ok": True, "sent": True,
                                                                                "email": "lh@example.com"}}
    r = P.send_attached_letter("1ECF3", str(pdf), subject="Notice", document_number="LETTER/X", confirm=True)
    assert not r["ok"] and "WITHOUT the attachment" in r["error"]


def test_store_failure_stops_before_any_email(wp):
    calls, replies, pdf = wp
    replies["/wp-json/example-court/v1/sync/demand-pdf"] = {"ok": False, "error": "nope"}
    r = P.send_attached_letter("1ECF3", str(pdf), subject="Notice", document_number="LETTER/X", test=True)
    assert not r["ok"] and len(calls) == 1


def test_only_pdfs_are_sent(wp, tmp_path):
    calls, _, _ = wp
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"not a pdf")
    assert not P.send_attached_letter("1ECF3", str(bad), subject="N", document_number="L", test=True)["ok"]
    assert not calls
