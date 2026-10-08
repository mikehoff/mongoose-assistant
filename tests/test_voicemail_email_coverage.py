"""Voicemails that arrive as forwarded IMAP emails must be findable even when the person asks
generically ("any voicemails?") and even when the email itself never contains the literal word
"voicemail". These tests pin: category qualifiers ("rtm") do not become dead search terms, a generic
ask lists recent voicemail-like emails, the recogniser covers common provider phrasings and is
owner-extensible, and a specific caller query does not drag in unrelated voicemail emails. DB isolation
is automatic via conftest."""
from __future__ import annotations


def _seed():
    from mongoose.services.email_index import ingest_email, EmailIn
    # Two voicemail notices that do NOT contain the literal word "voicemail":
    ingest_email(EmailIn(source="imap", mailbox="INBOX",
        sender="VoIP provider <noreply@voip.example>", subject="New message in mailbox 601",
        body="You have a new message from 07700 900123. Duration 00:42.",
        received_at="2026-06-30T09:15:00Z"))
    ingest_email(EmailIn(source="imap", mailbox="INBOX",
        sender="messages@carrier.example", subject="You have a new voice message",
        body="Caller: 020 7946 0000. Sign in to hear it.",
        received_at="2026-06-29T18:02:00Z"))
    # A plain non-voicemail email that must never be surfaced:
    ingest_email(EmailIn(source="imap", mailbox="INBOX",
        sender="newsletter@shop.example", subject="Summer sale inside",
        body="Big discounts this week.", received_at="2026-06-30T10:00:00Z"))


def test_category_and_noise_asks_extract_generic():
    from mongoose import tools
    for p in ["are there any rtm voicemails", "any voicemails", "are there any voicemails",
              "whats the most recent voicemail thats not new but already read"]:
        assert tools._extract_voicemail_query(p) is None, p


def test_generic_search_lists_recent_voicemail_emails():
    _seed()
    from mongoose.services.voicemail import search_voicemails
    res = search_voicemails("")
    subjects = {r.get("subject") for r in res}
    assert "New message in mailbox 601" in subjects
    assert "You have a new voice message" in subjects
    assert "Summer sale inside" not in subjects
    assert all(r.get("origin") == "email" for r in res)


def test_recogniser_covers_provider_phrasings():
    from mongoose.services.voicemail import _email_looks_like_voicemail
    assert _email_looks_like_voicemail("VoIP provider", "New message in mailbox 601", "you have a new message")
    assert _email_looks_like_voicemail("x@y", "You have a new voice message", "caller 020")
    assert _email_looks_like_voicemail("x@y", "Missed call from 07000", "someone called")
    assert not _email_looks_like_voicemail("newsletter@shop", "Summer sale", "discounts on shoes")


def test_recogniser_is_owner_extensible(monkeypatch):
    from mongoose.services.voicemail import _email_looks_like_voicemail
    monkeypatch.setenv("MONGOOSE_VOICEMAIL_EMAIL_MARKERS", "phonecall alert, reception buzzed")
    assert _email_looks_like_voicemail("x@y", "PhoneCall Alert", "front desk")
    monkeypatch.setenv("MONGOOSE_VOICEMAIL_EMAIL_SENDERS", "pbx@office.example")
    assert _email_looks_like_voicemail("pbx@office.example", "message waiting", "line 3")


def test_specific_caller_query_does_not_drag_in_unrelated():
    _seed()
    from mongoose.services.voicemail import search_voicemails
    # A specific caller who left nothing: must not list the other unrelated voicemail emails.
    res = search_voicemails("07999123456")
    assert res == [] or all("07999123456" in (str(r.get("caller_number") or "") + str(r.get("notice_text") or "")) for r in res)
