"""Tests for the unified find() search (files + attachments + email), and the
expanded setup wizard / guided update."""
import pytest

from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def _seed_all(tmp_path):
    """A file, a phone file, an attachment, and an email, all sharing a term."""
    import tempfile
    from mongoose.services.file_index import index_files, ingest_device_files
    from mongoose.services.attachments import index_attachment
    from mongoose.services.email_index import ingest_email, EmailIn
    d = tempfile.mkdtemp()
    (tmp_path / "x").mkdir(exist_ok=True)
    import os
    open(os.path.join(d, "sc_notes.md"), "w").write("service charge breakdown")
    index_files(d, device="macbook")
    ingest_device_files([{"path": "/sdcard/sc.md", "filename": "sc.md",
                          "text": "service charge from phone", "mtime": 1.0}], device="phone")
    index_attachment(source="imap", source_message_id="<a@x>", filename="service_charge.pdf",
                     sender="agent@block.com", subject="Cert", email_date="2026-06-01",
                     content_text="service charge certificate")
    ingest_email(EmailIn(source="imap", source_message_id="<e@x>", mailbox="INBOX",
                         sender="agent@block.com", subject="Service charge demand",
                         body="the service charge demand is attached", privacy="medium"))


# --- unified find ------------------------------------------------------------
def test_find_spans_all_sources(db, tmp_path):
    from mongoose.services.find import find
    _seed_all(tmp_path)
    res = find("service charge")
    kinds = {h["kind"] for h in res["results"]}
    assert {"file", "attachment", "email"} <= kinds
    # the two files (mac + phone) both present
    file_devices = {h["device"] for h in res["results"] if h["kind"] == "file"}
    assert {"macbook", "phone"} <= file_devices


def test_find_email_appears_as_searchable_doc(db, tmp_path):
    from mongoose.services.find import find
    _seed_all(tmp_path)
    res = find("demand")  # only in the email body/subject
    assert any(h["kind"] == "email" and "demand" in h["title"].lower() for h in res["results"])


def test_find_kinds_filter(db, tmp_path):
    from mongoose.services.find import find
    _seed_all(tmp_path)
    res = find("service charge", kinds=("email",))
    assert res["results"] and all(h["kind"] == "email" for h in res["results"])


def test_find_masks_private_email(db):
    from mongoose.services.email_index import ingest_email, EmailIn
    from mongoose.services.find import find
    ingest_email(EmailIn(source="imap", source_message_id="<p@x>", mailbox="INBOX",
                         sender="clinic@h.com", subject="dermatology results",
                         body="your dermatology results", privacy="high"))
    res = find("dermatology")
    assert res["count"] >= 1  # still findable
    assert all("dermatology" not in str(h["snippet"]).lower() for h in res["results"])
    assert all("[private" in h["title"] for h in res["results"] if h["kind"] == "email")


def test_find_endpoint(db, tmp_path):
    _seed_all(tmp_path)
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.get("/find", params={"q": "service charge"})
    assert r.status_code == 200
    assert r.json()["count"] >= 3


def test_mcp_search_files_now_unified(db, tmp_path):
    _seed_all(tmp_path)
    from mongoose.mcp_server import _tools
    t = _tools()
    assert "find" in t and "search_files" in t


# --- wizard / updater additions ----------------------------------------------
def test_wizard_has_new_integrations():
    from mongoose.setup_wizard import INTEGRATIONS
    slugs = {i.slug for i in INTEGRATIONS}
    for expected in ("push", "scheduler", "remote"):
        assert expected in slugs


def test_ntfy_verifier_reports_unconfigured():
    from mongoose.setup_wizard import verify_ntfy
    ok, msg = verify_ntfy({})
    assert ok is False and "NTFY" in msg


def test_guided_update_exists():
    # the comprehensive update flow is wired
    from mongoose import updater
    assert hasattr(updater, "run_update_guided")


def test_doctor_summary_helper_runs(db):
    from mongoose.setup_wizard import _print_doctor_summary
    # should not raise
    _print_doctor_summary()


def test_search_ui_surfaces_voicemail_and_notifications_as_first_class_sources():
    import mongoose.cockpit as ck
    html = ck.COCKPIT_HTML
    assert 'data-filter="voicemail"' in html
    assert 'data-filter="notification"' in html
    assert 'Search files, email, voicemails and notifications' in html
    assert 'kind==="voicemail"' in html
    assert 'kind==="notification"' in html
