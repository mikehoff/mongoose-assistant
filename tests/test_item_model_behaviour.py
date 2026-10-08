from __future__ import annotations

import asyncio
import os
from pathlib import Path


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db(Path(os.environ["MONGOOSE_DB_PATH"]))


def _seed_weak_file_strong_email():
    from mongoose.services.file_index import ingest_device_files
    from mongoose.services.email_index import ingest_email, EmailIn
    ingest_device_files([
        {"path": "/phone/weak.txt", "filename": "weak.txt",
         "text": "service charge mentioned once", "mtime": 1.0}
    ], device="phone")
    ingest_email(EmailIn(
        source="imap", source_message_id="<strong@x>", mailbox="INBOX",
        sender="agent@example.com", subject="Service charge tribunal evidence",
        body="service charge service charge tribunal evidence service charge",
        privacy="medium", received_at="2026-06-01T12:00:00+00:00",
    ))


def test_unified_find_ranks_relevance_across_connectors(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _seed_weak_file_strong_email()
    from mongoose.services.find import find

    res = find("service charge tribunal", limit=10)
    assert res["result_set_id"]
    assert res["results"][0]["kind"] == "email"
    assert "Service charge tribunal" in res["results"][0]["title"]
    assert res["results"][0]["score"] > res["results"][1]["score"]
    assert res["results"][0]["item_ref"]
    assert any(a["action"] == "open_email" for a in res["results"][0]["actions"])
    assert res["results"][0]["correlation_keys"]


def test_search_result_sets_do_not_get_cleared_by_next_search(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _seed_weak_file_strong_email()
    from mongoose.services.find import find
    from mongoose.services.item_model import result_set

    first = find("service charge tribunal", limit=10)
    first_id = first["result_set_id"]
    second = find("weak", limit=10)
    assert second["result_set_id"] != first_id

    old = result_set(first_id)
    assert old is not None
    assert old["id"] == first_id
    assert old["items"]
    assert old["items"][0]["snapshot"]["title"] == first["results"][0]["title"]


def test_item_actions_resolve_latest_first_result(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _seed_weak_file_strong_email()
    from mongoose.services.find import find
    from mongoose.services.item_model import describe_actions

    find("service charge tribunal", limit=10)
    desc = describe_actions(ordinal=1)
    assert desc["ok"] is True
    assert desc["kind"] == "email"
    assert any(a["action"] == "open_email" for a in desc["actions"])
    assert desc["correlation_keys"]


def test_chat_routes_natural_language_item_search_without_model(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _seed_weak_file_strong_email()
    import mongoose.tools as tools

    async def no_planner(*args, **kwargs):  # should not be called for this deterministic route
        raise AssertionError("planner should not be needed for natural-language item search")

    async def phrase(prompt, role="chat"):
        return "phrased"

    monkeypatch.setattr(tools, "ask_ollama", no_planner)
    monkeypatch.setattr(tools, "ask_persona", phrase)
    out = asyncio.run(tools.run_tool_turn("find my service charge tribunal email"))
    assert out["executed"] is True
    assert out["tool"] == "search_files"
    assert out["result"]["count"] >= 1
    assert out["result"]["results"][0]["kind"] == "email"
    assert out["result"]["result_set_id"]


def test_notification_search_uses_relevance_before_recency(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.services.notifications import NotificationIn, store_notification, search_notifications
    from mongoose.db import connect

    strong = store_notification(NotificationIn(source="phone", app="Mail", title="Lift invoice",
        body="lift invoice lift invoice service charge", privacy="normal", device="pixel"))["rowid"]
    weak = store_notification(NotificationIn(source="phone", app="Mail", title="Building note",
        body="lift invoice mentioned once", privacy="normal", device="pixel"))["rowid"]
    with connect() as con:
        con.execute("UPDATE notifications SET received_at='2026-01-01T00:00:00+00:00' WHERE id=?", (strong,))
        con.execute("UPDATE notifications SET received_at='2026-06-01T00:00:00+00:00' WHERE id=?", (weak,))
    hits = search_notifications("lift invoice", limit=10)
    assert hits[0]["id"] == strong


def test_item_model_endpoints_expose_stored_result_actions(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _seed_weak_file_strong_email()
    from mongoose.services.find import find
    sid = find("service charge tribunal", limit=10)["result_set_id"]
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/item/results", params={"result_set_id": sid})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result_set"]["items"]
    a = c.get("/item/actions", params={"result_set_id": sid, "ordinal": 1})
    assert a.status_code == 200
    assert a.json()["ok"] is True
    assert a.json()["actions"]
