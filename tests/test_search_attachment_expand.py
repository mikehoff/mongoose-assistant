"""Search result display: attachments belong to emails, so an attachment result must carry the
integer email_id of its parent (so clicking it opens the full email), and the email detail endpoint
must return the body + downloadable attachments for that id. This was the 'results not usefully
clickable / didn't unroll' bug — attachment results had only a string message-id, not the email row id."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _seed():
    from mongoose.services.email_index import ingest_email, EmailIn
    from mongoose.services.attachments import index_attachment
    r = ingest_email(EmailIn(source="imap", source_message_id="<msg123>", mailbox="INBOX",
        sender="M. Morgan", recipients="me", subject="Memorandum",
        body="Full body about Rahimi.", sent_at="2025-01-01", received_at="2025-01-01"))
    eid = r["id"]
    index_attachment(source="imap", filename="Memorandum.pdf", source_message_id="<msg123>",
        email_id=eid, mime_type="application/pdf", size_bytes=61909, sender="M.H",
        subject="Memo", content_text="Rahimi", payload_b64="QkFTRTY0")
    return eid


def test_attachment_result_carries_parent_email_id():
    _fresh()
    eid = _seed()
    from mongoose.services.find import find
    res = find("Rahimi")["results"]
    att = [r for r in res if r["kind"] == "attachment"]
    assert att, "expected an attachment result"
    # the attachment must carry the INTEGER email_id of its parent so the UI can open the email
    assert att[0]["email_id"] == eid
    assert att[0]["attachment_id"]
    assert att[0]["downloadable"] is True


def test_email_result_open_is_integer_row_id():
    _fresh()
    eid = _seed()
    from mongoose.services.find import find
    res = find("Rahimi")["results"]
    em = [r for r in res if r["kind"] == "email"]
    assert em
    assert em[0]["open"] == eid  # integer row id the detail endpoint accepts


def test_email_detail_endpoint_returns_body_and_attachments():
    _fresh()
    eid = _seed()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get(f"/email/message/{eid}", headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    m = r.json()["message"]
    assert m.get("body")
    assert len(m.get("attachments", [])) == 1
    assert m["attachments"][0].get("downloadable")
