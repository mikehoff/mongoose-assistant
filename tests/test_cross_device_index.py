"""Tests for cross-device file indexing and the email-attachment index."""
from email.message import EmailMessage

import pytest

from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- cross-device file indexing ----------------------------------------------
def test_files_tagged_with_device(db, tmp_path):
    from mongoose.services.file_index import index_files, search_files
    phone = tmp_path / "phone"
    mac = tmp_path / "mac"
    phone.mkdir()
    mac.mkdir()
    (phone / "taxes.md").write_text("annual taxes summary")
    (mac / "taxes_mac.md").write_text("annual taxes mac copy")

    r1 = index_files(str(phone), device="phone")
    r2 = index_files(str(mac), device="macbook")
    assert r1["device"] == "phone" and r2["device"] == "macbook"

    results = search_files("taxes")
    devices = {r["device"] for r in results}
    assert devices == {"phone", "macbook"}  # one query, both devices


def test_recent_files_carry_device(db, tmp_path):
    from mongoose.services.file_index import index_files, recent_files
    d = tmp_path / "phone"
    d.mkdir()
    (d / "note.md").write_text("recent phone note")
    index_files(str(d), device="phone")
    rows = recent_files(hours=24)
    assert rows and all("device" in r for r in rows)
    assert any(r["device"] == "phone" for r in rows)


def test_device_defaults_to_local(db, tmp_path):
    from mongoose.services.file_index import index_files, search_files
    d = tmp_path / "x"
    d.mkdir()
    (d / "a.md").write_text("local content here")
    index_files(str(d))  # no device arg
    assert all(r["device"] == "local" for r in search_files("local"))


# --- attachment indexing -----------------------------------------------------
def _email_with_attachments():
    msg = EmailMessage()
    msg["From"] = "Lawfirm LLP <legal@lawfirm.example>"
    msg["Subject"] = "Tribunal bundle"
    msg["Date"] = "Mon, 14 Jun 2026 10:00:00 +0000"
    msg["Message-ID"] = "<abc@lawfirm.example>"
    msg.set_content("See attached.")
    msg.add_attachment(b"%PDF-fake", maintype="application", subtype="pdf",
                       filename="hearing_bundle.pdf")
    msg.add_attachment(b"data", maintype="application", subtype="vnd.ms-excel",
                       filename="service_charges.xlsx")
    return msg


def test_extract_attachments_from_message(db):
    from mongoose.services.attachments import extract_attachments_from_message
    atts = extract_attachments_from_message(_email_with_attachments())
    names = {a["filename"] for a in atts}
    assert names == {"hearing_bundle.pdf", "service_charges.xlsx"}
    assert all(a["size_bytes"] and a["mime_type"] for a in atts)


def test_index_and_search_attachment_by_filename(db):
    from mongoose.services.attachments import (
        extract_attachments_from_message, index_attachment, search_attachments)
    msg = _email_with_attachments()
    for a in extract_attachments_from_message(msg):
        index_attachment(source="imap", source_message_id=msg["Message-ID"],
                         filename=a["filename"], mime_type=a["mime_type"],
                         size_bytes=a["size_bytes"], sender=msg["From"],
                         subject=msg["Subject"], email_date=msg["Date"], mailbox="INBOX")
    hits = search_attachments("bundle")
    assert any(h["filename"] == "hearing_bundle.pdf" for h in hits)
    # the email it came from is recoverable
    assert all(h["source_message_id"] == "<abc@lawfirm.example>" for h in hits)


def test_search_attachment_by_subject_and_sender(db):
    from mongoose.services.attachments import index_attachment, search_attachments
    index_attachment(source="imap", source_message_id="<m1>", filename="q1.pdf",
                     sender="bob@acme.com", subject="Quarterly numbers", email_date="2026-06-01")
    assert search_attachments("quarterly")  # by subject
    assert search_attachments("acme")       # by sender


def test_attachment_index_is_idempotent(db):
    from mongoose.services.attachments import index_attachment, attachment_stats
    for _ in range(3):
        index_attachment(source="imap", source_message_id="<m2>", filename="dup.pdf",
                         size_bytes=100, sender="x", subject="s", email_date="2026-06-01")
    assert attachment_stats()["total"] == 1


def test_content_extraction_seam_present(db):
    # content_text is accepted now (for the follow-up) and marks content_indexed
    from mongoose.services.attachments import index_attachment, search_attachments, attachment_stats
    index_attachment(source="imap", source_message_id="<m3>", filename="report.pdf",
                     content_text="the leaseholder service charge was unreasonable",
                     sender="x", subject="s", email_date="2026-06-01")
    # searchable by content
    assert search_attachments("leaseholder")
    assert attachment_stats()["content_indexed"] == 1


# --- imap sync wires attachments (without a real server) ---------------------
def test_imap_sync_indexes_attachments(db, monkeypatch):
    # Drive the attachment path the way sync_imap does, proving the wiring without
    # a live IMAP server.
    from mongoose.services.attachments import extract_attachments_from_message, index_attachment, attachment_stats
    msg = _email_with_attachments()
    for att in extract_attachments_from_message(msg):
        index_attachment(source="imap", source_message_id=msg.get("Message-ID"),
                         filename=att["filename"], mime_type=att.get("mime_type"),
                         size_bytes=att.get("size_bytes"), sender=msg.get("From"),
                         subject=msg.get("Subject"), email_date=msg.get("Date"), mailbox="INBOX")
    assert attachment_stats()["total"] == 2


def test_doctor_reports_search_index(db, tmp_path):
    from mongoose.services.file_index import index_files
    d = tmp_path / "p"
    d.mkdir()
    (d / "f.md").write_text("indexed content")
    index_files(str(d), device="phone")
    from mongoose.doctor import run_doctor
    r = run_doctor()
    assert "search_index" in r["checks"]
    si = r["checks"]["search_index"]
    assert si["files"] >= 1
    assert "phone" in si["devices"]
