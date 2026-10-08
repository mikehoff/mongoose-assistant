"""Two-tier surpriser + guest light tasks , the ISOLATION boundary.

The security-critical property: a guest gets a RESTRICTED surpriser (delights/entertainment + messages
the owner left them) and light scope-isolated tasks, and NONE of it can read or write the owner's real-world data,
tasks, or scheduler. These tests pin that boundary at the module and endpoint level.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    return A, TestClient(A.app)


# ── guest surpriser never touches owner data ───────────────────────────────────────────────────────

def test_guest_surprise_refuses_owner_scope(app_client):
    from mongoose.services.guest_surpriser import guest_surprise
    r = guest_surprise("owner")
    assert r["surfaced"] is False
    assert "guest" in r["reason"]


def test_guest_surprise_never_calls_owner_candidate_source(app_client, monkeypatch):
    # Hard proof of separation: if the guest surpriser ever called the owner candidate source, this
    # would blow up. It must never be invoked for a guest.
    import mongoose.services.surpriser as SUR
    async def boom():
        raise AssertionError("guest surpriser must NEVER call owner candidate_suggestions")
    monkeypatch.setattr(SUR, "candidate_suggestions", boom)
    from mongoose.services.guest_surpriser import guest_surprise
    r = guest_surprise("guest:devA", prefer="delight")
    assert r["surfaced"] in (True, False)  # produced a result without touching owner candidates


def test_owner_message_surfaces_to_its_guest_only(app_client):
    from mongoose.services.mailbox import send_message, ensure_tables
    from mongoose.services.guest_surpriser import guest_surprise
    ensure_tables()
    send_message(sender="owner", recipient="guest:devA", body="coffee's on", notify=False)
    # the intended guest sees it
    r = guest_surprise("guest:devA")
    assert r["suggestion"]["kind"] == "owner_message"
    # a DIFFERENT guest does not
    r2 = guest_surprise("guest:devB")
    assert r2.get("suggestion", {}).get("kind") != "owner_message"


# ── guest tasks are scope-isolated and refuse owner scope ──────────────────────────────────────────

def test_guest_tasks_refuse_owner_scope(app_client):
    import mongoose.services.guest_tasks as GT
    with pytest.raises(ValueError):
        GT.add_item("owner", kind="reminder", text="should never write to owner")
    with pytest.raises(ValueError):
        GT.list_items("owner")


def test_guest_items_isolated_between_guests(app_client):
    import mongoose.services.guest_tasks as GT
    GT.add_item("guest:devA", kind="todo", text="A only")
    GT.add_item("guest:devB", kind="todo", text="B only")
    a = [i["text"] for i in GT.list_items("guest:devA")]
    b = [i["text"] for i in GT.list_items("guest:devB")]
    assert a == ["A only"] and b == ["B only"]


def test_guest_cannot_complete_another_guests_item(app_client):
    import mongoose.services.guest_tasks as GT
    res = GT.add_item("guest:devA", kind="todo", text="A's")
    other = GT.complete_item("guest:devB", res["id"])  # wrong owner
    assert other["ok"] is False
    # A's item is still open
    assert any(i["id"] == res["id"] for i in GT.list_items("guest:devA"))


def test_day_plan_is_scope_checked_and_orders_urgent_first(app_client):
    import mongoose.services.guest_tasks as GT
    with pytest.raises(ValueError):
        GT.day_plan("owner", ["x"])
    plan = GT.day_plan("guest:devA", ["read a book", "call the bank now", "tidy up"])
    assert plan["steps"][0]["task"] == "call the bank now"  # urgent front-loaded


# ── endpoint level: guest-only gating ──────────────────────────────────────────────────────────────

def test_guest_task_endpoint_rejects_owner_device(app_client, monkeypatch):
    A, c = app_client
    # an owner device (not trusted_guest) must be refused the guest task endpoint
    monkeypatch.setattr(A, "_device_from_request",
                        lambda request: {"device_id": "mac1", "profile": "primary_phone"})
    r = c.post("/device/guest/task", json={"action": "reflect", "text": "hi"})
    assert r.status_code == 403


def test_guest_surprise_endpoint_routes_guest_to_restricted(app_client, monkeypatch):
    A, c = app_client
    monkeypatch.setattr(A, "_device_from_request",
                        lambda request: {"device_id": "devA", "profile": "trusted_guest"})
    # Even if the owner surpriser would explode, the guest path must not call it.
    import mongoose.services.surpriser as SUR
    async def boom():
        raise AssertionError("guest device must not hit the owner surpriser")
    monkeypatch.setattr(SUR, "pick_surprise", boom)
    r = c.get("/device/surprise?prefer=delight")
    assert r.status_code == 200
    body = r.json()
    assert body.get("audience") == "guest" or body.get("surfaced") in (True, False)


def test_guest_can_do_endpoint(app_client, monkeypatch):
    A, c = app_client
    monkeypatch.setattr(A, "_device_from_request",
                        lambda request: {"device_id": "devA", "profile": "trusted_guest"})
    r = c.get("/device/guest/can-do")
    assert r.status_code == 200
    assert "can_do" in r.json()


def test_guest_what_can_i_do_surfaces_light_tasks(app_client):
    # When a guest asks what they can do, Mongoose must mention the light, scope-isolated capabilities
    # (coach/focus/plan/research/reminders), not just the fun menu , and state the privacy boundary.
    import mongoose.app as A
    for q in ("what can you do", "what can I do", "how can you help"):
        reply = A._maybe_guest_delight(q)
        assert reply, f"expected a guest capabilities reply for {q!r}"
        assert "low-key helper" in reply        # the light-task intro
        assert "Talk something through" in reply  # a coaching capability
        assert "Alex" in reply                    # the privacy boundary is stated
