"""Tests for the named HA assistant prompts and device-tagged notification
routing (phone/tablet/HA notifications into Mongoose)."""
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


# --- HA prompts (the screenshot list) ----------------------------------------
def test_prompt_menu_matches_screenshot(db):
    from mongoose.services.ha_prompts import list_prompts
    titles = {p["title"] for p in list_prompts()}
    # the items shown in the HA "Add from Home Assistant" screenshot
    for expected in ["Automation review", "Automation builder", "Automation debugger",
                     "Automation audit", "Troubleshoot device", "Setup guide",
                     "Schedule optimizer", "Naming conventions", "Daily summary",
                     "Energy report", "Dashboard builder", "Change validator",
                     "Security review"]:
        assert expected in titles


def test_list_prompts_is_model_free(db):
    # listing must not need the model (instant menu)
    from mongoose.services.ha_prompts import list_prompts
    items = list_prompts()
    assert all({"key", "title", "description"} <= set(p) for p in items)


def test_run_prompt_routes_through_brain(db, monkeypatch):
    import mongoose.llm as llm

    async def fake(prompt, system=None, role="chat"):
        assert "automation" in prompt.lower()
        return "Mocked review."
    monkeypatch.setattr(llm, "ask_ollama", fake)
    from mongoose.services.ha_prompts import run_prompt
    r = asyncio.run(run_prompt("automation_review"))
    assert r["ok"] and r["reply"] == "Mocked review."


def test_run_prompt_appends_extra(db, monkeypatch):
    import mongoose.llm as llm
    seen = {}

    async def fake(prompt, system=None, role="chat"):
        seen["prompt"] = prompt
        return "ok"
    monkeypatch.setattr(llm, "ask_ollama", fake)
    from mongoose.services.ha_prompts import run_prompt
    asyncio.run(run_prompt("troubleshoot_device", extra="the hall motion sensor"))
    assert "hall motion sensor" in seen["prompt"]


def test_daily_summary_prompt_uses_brief(db, monkeypatch):
    import mongoose.services.brief as brief

    async def fake_brief(*, post_to_bus=False):
        return {"brief": "Today is clear.", "signals": []}
    monkeypatch.setattr(brief, "generate_brief", fake_brief)
    from mongoose.services.ha_prompts import run_prompt
    r = asyncio.run(run_prompt("daily_summary"))
    assert r["ok"] and r["reply"] == "Today is clear." and r["kind"] == "brief"


def test_unknown_prompt_fails_cleanly(db):
    from mongoose.services.ha_prompts import run_prompt
    r = asyncio.run(run_prompt("does_not_exist"))
    assert r["ok"] is False and "available" in r


# --- device-tagged notifications (phone / tablet / HA into Mongoose) ----------
def test_notification_carries_device(db):
    from mongoose.services.notifications import (
        NotificationIn, ingest_notification, recent_notifications)
    asyncio.run(ingest_notification(NotificationIn(
        source="android", app="WhatsApp", title="Sam: dinner?", body="7pm",
        privacy="medium", urgency="normal", device="pixel_phone")))
    rows = recent_notifications()
    assert any(r["device"] == "pixel_phone" for r in rows)


def test_ha_notification_routes_in(db):
    from mongoose.services.notifications import (
        NotificationIn, ingest_notification, recent_notifications)
    asyncio.run(ingest_notification(NotificationIn(
        source="home_assistant", app="HA", title="Back door unlocked", body="10 min",
        privacy="medium", urgency="high", device="home_assistant")))
    rows = recent_notifications()
    ha = [r for r in rows if r["device"] == "home_assistant"]
    assert ha and ha[0]["urgency"] == "high"


def test_same_notification_from_two_devices_not_deduped(db):
    # device is part of the dedupe key, so the SAME alert on phone and tablet is
    # two distinct rows (you genuinely got it twice).
    from mongoose.services.notifications import (
        NotificationIn, ingest_notification, recent_notifications)
    for dev in ("pixel_phone", "ipad_mini"):
        asyncio.run(ingest_notification(NotificationIn(
            source="android", app="Calendar", title="Tribunal 9am", body="tomorrow",
            privacy="low", urgency="high", device=dev)))
    rows = [r for r in recent_notifications() if r["title"] == "Tribunal 9am"]
    assert {r["device"] for r in rows} == {"pixel_phone", "ipad_mini"}


def test_ingest_endpoint_accepts_device(db):
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.post("/notifications/ingest", json={
        "source": "android", "app": "Signal", "title": "msg", "body": "hi",
        "device": "pixel_phone"})
    assert r.status_code == 200
    assert r.json()["inserted"] is True


def test_ha_prompts_endpoint_lists(db):
    from fastapi.testclient import TestClient
    import mongoose.app as app_mod
    c = TestClient(app_mod.app)
    r = c.get("/ha-prompts")
    assert r.status_code == 200
    titles = {p["title"] for p in r.json()["prompts"]}
    assert "Energy report" in titles
