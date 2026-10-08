import base64


def test_google_oauth_redirect_port_is_stable_but_overrideable(monkeypatch):
    from mongoose.oauth import google_oauth_redirect_port, google_oauth_redirect_uri

    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_PORT", raising=False)
    assert google_oauth_redirect_port() == 8766
    assert google_oauth_redirect_uri() == "http://localhost:8766/"

    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "8877")
    assert google_oauth_redirect_port() == 8877
    assert google_oauth_redirect_uri() == "http://localhost:8877/"

    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "not-a-port")
    assert google_oauth_redirect_uri() == "http://localhost:8766/"


def test_task_tool_aliases_filter_and_recount_correctly():
    import asyncio
    from mongoose.services.calendar_tasks import TaskIn, add_task
    from mongoose.tools import TOOLS, READ_TOOLS

    add_task(TaskIn(source="test", source_task_id="1", list_name="Work", title="write report"))
    add_task(TaskIn(source="test", source_task_id="2", list_name="Home", title="buy milk"))
    add_task(TaskIn(source="test", source_task_id="3", list_name="Work", title="send update", status="completed"))

    assert "get_tasks" in TOOLS
    assert "get_calendar" in TOOLS
    assert "get_tasks" in READ_TOOLS
    res = asyncio.run(TOOLS["get_tasks"]["executor"]({"list_name": "Work"}))
    assert res["ok"] is True
    assert res["count"] == 1
    assert [t["title"] for t in res["tasks"]] == ["write report"]
    assert [l["name"] for l in res["lists"]] == ["Work"]


def test_role_router_routes_todo_and_integration_status_to_tools():
    from mongoose.role_router import classify_role

    assert classify_role("what folders are there in TODO") == "tool"
    assert classify_role("can you see my to do list") == "tool"
    assert classify_role("what integrations are working") == "tool"


def test_email_detail_and_download_endpoints_expose_real_body_and_exports():
    from fastapi.testclient import TestClient
    from mongoose.app import app
    from mongoose.services.email_index import EmailIn, ingest_email
    from mongoose.services.attachments import index_attachment

    raw = b"Subject: RTM update\nFrom: sender@example.com\nTo: me@example.com\n\nHello RTM body"
    created = ingest_email(EmailIn(
        source="imap",
        source_message_id="<rtm-1>",
        mailbox="INBOX",
        sender="sender@example.com",
        recipients="me@example.com",
        subject="RTM update",
        body="Hello RTM body",
        raw={"raw_rfc822_b64": base64.b64encode(raw).decode("ascii")},
    ))
    index_attachment(
        source="imap",
        source_message_id="<rtm-1>",
        email_id=created["id"],
        filename="minutes.txt",
        mime_type="text/plain",
        size_bytes=7,
        payload_b64=base64.b64encode(b"minutes").decode("ascii"),
    )

    with TestClient(app) as client:
        nonce = client.get("/cockpit/nonce").json()["cockpit_nonce"]
        detail = client.get(f"/email/message/{created['id']}", headers={"X-Cockpit-Nonce": nonce})
        assert detail.status_code == 200
        msg = detail.json()["message"]
        assert msg["body"] == "Hello RTM body"
        assert msg["attachments"][0]["filename"] == "minutes.txt"
        assert msg["attachments"][0]["downloadable"] in (1, True)

        txt = client.get(f"/email/message/{created['id']}/download?format=txt", headers={"X-Cockpit-Nonce": nonce})
        assert txt.status_code == 200
        assert b"Hello RTM body" in txt.content

        eml = client.get(f"/email/message/{created['id']}/download?format=eml", headers={"X-Cockpit-Nonce": nonce})
        assert eml.status_code == 200
        assert eml.content == raw

        pdf = client.get(f"/email/message/{created['id']}/download?format=pdf", headers={"X-Cockpit-Nonce": nonce})
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")

        att_id = msg["attachments"][0]["id"]
        att = client.get(f"/email/attachment/{att_id}/download", headers={"X-Cockpit-Nonce": nonce})
        assert att.status_code == 200
        assert att.content == b"minutes"

        zip_resp = client.get(f"/email/message/{created['id']}/attachments.zip", headers={"X-Cockpit-Nonce": nonce})
        assert zip_resp.status_code == 200
        assert zip_resp.content.startswith(b"PK")
