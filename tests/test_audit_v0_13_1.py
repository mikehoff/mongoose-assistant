"""Tests for the v0.13.1 QA audit remediation: cross-device collision, binary
files indexed by name, unified search in the chat tool loop, Gmail/Microsoft
attachment indexing (mocked), scheduler multi-provider, ntfy local-first, Google
verifier split, and loud schema verification."""
import asyncio

import pytest

from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- #2 cross-device collision ------------------------------------------------
def test_same_path_two_devices_no_collision(db):
    from mongoose.services.file_index import ingest_device_files, search_files
    ingest_device_files([{"path": "/sdcard/Download/report.pdf", "filename": "report.pdf",
                          "text": "alpha", "mtime": 1.0}], device="alpha_phone")
    ingest_device_files([{"path": "/sdcard/Download/report.pdf", "filename": "report.pdf",
                          "text": "beta", "mtime": 2.0}], device="beta_tablet")
    devices = {h["device"] for h in search_files("report")}
    assert devices == {"alpha_phone", "beta_tablet"}   # both kept


def test_same_device_same_path_updates_not_duplicates(db):
    from mongoose.services.file_index import ingest_device_files
    from mongoose.db import connect
    for txt in ("v1", "v2", "v3"):
        ingest_device_files([{"path": "/sdcard/a.md", "filename": "a.md", "text": txt, "mtime": 1.0}],
                            device="phone")
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM files WHERE device='phone' AND path='/sdcard/a.md'").fetchone()[0]
    assert n == 1   # same (device, path) updates in place


# --- #3 binary files indexed by name -----------------------------------------
def test_binary_files_indexed_by_filename(db, tmp_path):
    from mongoose.services.file_index import index_files, search_files
    d = tmp_path / "files"
    d.mkdir()
    (d / "holiday_photo.jpg").write_bytes(b"\xff\xd8\xff")
    (d / "data_export.zip").write_bytes(b"PK\x03\x04")
    (d / "budget.xlsx").write_bytes(b"PK\x03\x04xlsx")
    r = index_files(str(d), device="mac")
    assert r["indexed"] == 3
    assert search_files("holiday_photo")
    assert search_files("data_export")
    assert search_files("budget")


# --- #4 chat tool loop uses unified find -------------------------------------
def test_chat_tool_search_uses_unified_find(db, tmp_path):
    from mongoose.services.email_index import ingest_email, EmailIn
    from mongoose.tools import _tool_search_files
    ingest_email(EmailIn(source="imap", source_message_id="<e@x>", subject="Invoice 0012",
                         body="please pay the invoice", privacy="medium"))
    res = asyncio.run(_tool_search_files({"query": "invoice"}))
    # unified result shape (has 'results' with kind labels), and email is found
    assert "results" in res
    assert any(h.get("kind") == "email" for h in res["results"])


# --- #1 Gmail attachments (fully mocked) -------------------------------------
@pytest.mark.connectors
def test_gmail_attachments_indexed(db, monkeypatch):
    pytest.importorskip("googleapiclient")
    import base64
    from mongoose.connectors import gmail

    # a fake Gmail service with one message carrying a PDF attachment
    msg = {
        "id": "m1", "threadId": "t1", "labelIds": ["INBOX"], "snippet": "see attached",
        "payload": {
            "headers": [{"name": "From", "value": "agent@block.com"},
                        {"name": "Subject", "value": "Service charge"},
                        {"name": "Date", "value": "Mon, 01 Jun 2026 10:00:00 +0000"}],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"hello").decode()}},
                {"filename": "certificate.pdf", "mimeType": "application/pdf",
                 "body": {"attachmentId": "att1", "size": 1234}},
            ],
        },
    }

    class FakeAttachments:
        def get(self, userId, messageId, id):
            return self
        def execute(self):
            return {"data": base64.urlsafe_b64encode(b"%PDF-1.4 service charge certificate text").decode()}

    class FakeMessages:
        def list(self, userId, q, maxResults):
            return self
        def get(self, userId, id, format):
            self._id = id
            return self
        def attachments(self):
            return FakeAttachments()
        def execute(self):
            # list() execute returns the listing; get() execute returns the message
            return {"messages": [{"id": "m1"}]} if not hasattr(self, "_id") else msg

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeService:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(gmail, "build", lambda *a, **k: FakeService())
    monkeypatch.setattr(gmail, "_credentials", lambda: None)
    # extraction of the fake PDF bytes: monkeypatch the content extractor to a stub
    import mongoose.services.attachments as att
    monkeypatch.setattr(att, "_extract_attachment_content",
                        lambda name, raw: "service charge certificate text" if name.endswith(".pdf") else None)

    result = gmail.sync_gmail(include_attachments=True)
    assert result["attachments"] == 1
    from mongoose.services.attachments import search_attachments
    hits = search_attachments("certificate")
    assert any(h["filename"] == "certificate.pdf" for h in hits)
    # content searchable
    assert search_attachments("service charge certificate")


# --- #1 Microsoft attachments (fully mocked) ---------------------------------
@pytest.mark.connectors
def test_microsoft_attachments_indexed(db, monkeypatch):
    pytest.importorskip("msal")
    import base64
    from mongoose.connectors import microsoft_graph as mg

    listing = {"value": [{
        "id": "msg1", "subject": "Demand", "hasAttachments": True,
        "from": {"emailAddress": {"address": "agent@block.com"}},
        "toRecipients": [], "receivedDateTime": "2026-06-01T10:00:00Z",
        "sentDateTime": "2026-06-01T10:00:00Z", "bodyPreview": "see attached",
        "body": {"content": "see attached"},
    }]}
    att_list = {"value": [{"id": "a1", "name": "demand.pdf", "contentType": "application/pdf",
                           "size": 999, "@odata.type": "#microsoft.graph.fileAttachment"}]}
    att_full = {"contentBytes": base64.b64encode(b"%PDF demand text").decode()}

    class FakeResp:
        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            if "/attachments/a1" in url:
                return FakeResp(att_full)
            if "/attachments" in url:
                return FakeResp(att_list)
            return FakeResp(listing)

    monkeypatch.setattr(mg.httpx, "Client", FakeClient)
    monkeypatch.setattr(mg, "_headers", lambda: {"Authorization": "Bearer x"})
    import mongoose.services.attachments as att
    monkeypatch.setattr(att, "_extract_attachment_content",
                        lambda name, raw: "demand text" if name.endswith(".pdf") else None)

    result = mg.sync_microsoft_mail(include_attachments=True)
    assert result["attachments"] == 1
    from mongoose.services.attachments import search_attachments
    assert any(h["filename"] == "demand.pdf" for h in search_attachments("demand"))


# --- #5 scheduler multi-provider ---------------------------------------------
def test_scheduler_email_job_skips_when_none_configured(db, monkeypatch):
    from mongoose.services import scheduler as S
    monkeypatch.delenv("GOOGLE_TOKEN_PATH", raising=False)
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)
    # no imap host either
    result = asyncio.run(S._job_sync_email())
    assert result.get("skipped") == "no_mail_providers_configured"


# --- #8 ntfy local-first ------------------------------------------------------
def test_wizard_ntfy_defaults_local(db):
    from mongoose.setup_wizard import INTEGRATIONS
    push = next(i for i in INTEGRATIONS if i.slug == "push")
    url_field = next(f for f in push.fields if f.key == "NTFY_URL")
    assert "127.0.0.1" in url_field.default   # local, not ntfy.sh


def test_send_to_phone_warns_on_public_ntfy(db, monkeypatch):
    import mongoose.services.notifications as N
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh")
    monkeypatch.setenv("NTFY_TOPIC", "mongoose-test")

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, content=None): return FakeResp()

    monkeypatch.setattr(N.httpx, "AsyncClient", FakeClient)
    r = asyncio.run(N.send_to_phone("hi", "body"))
    assert r["sent"] is True and "warning" in r   # public server flagged


# --- #7 Google verifier split ------------------------------------------------
def test_google_verifier_reports_oauth_and_gemini_separately(db):
    from mongoose.setup_wizard import verify_google_api
    ok, msg = verify_google_api({})   # nothing configured
    assert "OAuth" in msg and "Gemini" in msg
    assert ok is False   # no oauth token -> not ready


# --- #9 loud schema verification ---------------------------------------------
def test_schema_verification_passes_on_fresh_db(db):
    # init_db already ran _verify_schema in the fixture; reaching here means it passed
    from mongoose.db import connect
    with connect() as con:
        idx = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_files_device_path'").fetchone()
    assert idx is not None


def test_schema_verification_raises_on_broken_schema(tmp_path, monkeypatch):
    import sqlite3
    from mongoose import db as dbmod
    p = tmp_path / "broken.sqlite3"
    con = sqlite3.connect(str(p))
    # a files table WITHOUT device and WITH the old unique -> should fail verify
    con.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, "
                "title TEXT, body TEXT, mtime REAL, indexed_at TEXT)")
    con.row_factory = sqlite3.Row
    with pytest.raises(RuntimeError, match="Schema verification FAILED"):
        dbmod._verify_schema(con)
    con.close()


# --- #10 role classification of natural file-finding phrases ------------------
def test_natural_file_finding_routes_to_tool():
    from mongoose.role_router import classify_role
    for phrase in [
        "find my insurance certificate",
        "where's the tribunal bundle",
        "do I have a receipt for the yoga",
        "where did I save that contract",
        "pull up my bank statement",
        "search for invoices from last month",
        "where are my tax documents",
    ]:
        assert classify_role(phrase) == "tool", phrase


def test_file_finding_no_false_positives():
    from mongoose.role_router import classify_role
    # find/where verbs that are NOT about files must stay chat
    for phrase in [
        "find peace in difficult times",
        "where is France on the map",
        "how do I find the area of a circle",
        "I need to find myself",
        "the report says inflation is rising",
        "what is a certificate of deposit",
        "where is the nearest station",
    ]:
        assert classify_role(phrase) == "chat", phrase
