"""Tests for the Daily Operating Picture, Mongoose's cross-domain synthesis flagship.

The point of these tests is the SYNTHESIS, not just that an endpoint returns 200: the DOP
must make connections BETWEEN domains (email from someone in today's calendar), organise
signals into the seven-part shape, and compute each section from real data (facts before
fluency) rather than inventing it."""
import asyncio
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

from mongoose.app import app
from mongoose.db import connect, init_db
import mongoose.services.daily_picture as DP

client = TestClient(app)


def _seed_event_and_email_same_person():
    init_db()
    now = datetime.now(timezone.utc)
    # Span the current moment (start before now, end after) so the event unambiguously
    # overlaps "today" in local time regardless of the hour the suite runs , avoids a
    # timezone day-boundary flake where 'now + 3h' could land outside today.
    start = now - timedelta(hours=2)
    end = now + timedelta(hours=2)
    with connect() as con:
        con.execute(
            "INSERT INTO events(source,title,start_at,end_at,busy_status,sensitivity,last_seen) "
            "VALUES('test','Meeting with Chris Hughes',?,?,'busy','normal',?)",
            (start.isoformat(), end.isoformat(), now.isoformat()))
        con.execute(
            "INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
            "VALUES('test','Chris Hughes <chris@example.com>','Re: agenda','x',?,'normal',?)",
            (now.isoformat(), now.isoformat()))
        con.commit()


def test_dop_makes_cross_domain_link():
    """The core value: an email from someone in today's calendar is surfaced as a
    connection that no single-domain tool could make."""
    _seed_event_and_email_same_person()
    dop = asyncio.run(DP.daily_operating_picture())
    kinds = [l["kind"] for l in dop["cross_domain_links"]]
    assert "email_meets_calendar" in kinds
    txt = " ".join(l["text"] for l in dop["cross_domain_links"])
    assert "Chris Hughes" in txt


def test_dop_has_all_seven_sections():
    dop = asyncio.run(DP.daily_operating_picture())
    for key in ["todays_shape", "cross_domain_links", "what_changed", "what_needs_you",
                "what_can_wait", "whats_broken", "what_mongoose_did", "next_smallest_step"]:
        assert key in dop


def test_dop_todays_shape_has_date():
    dop = asyncio.run(DP.daily_operating_picture())
    assert dop["todays_shape"]["date"]


def test_dop_weather_threatens_outdoor_event():
    """Cross-domain: bad weather flagged against an outdoor-looking event."""
    init_db()
    now = datetime.now(timezone.utc)
    with connect() as con:
        con.execute(
            "INSERT INTO events(source,title,start_at,end_at,busy_status,sensitivity,last_seen) "
            "VALUES('test','Morning walk in the park',?,?,'busy','normal',?)",
            ((now - timedelta(hours=2)).isoformat(), (now + timedelta(hours=2)).isoformat(), now.isoformat()))
        con.commit()
    # inject weather via the picture path
    import mongoose.services.brief as brief
    orig = brief.gather

    async def fake_gather():
        p = await orig()
        p["weather"] = {"summary": "Heavy rain showers"}
        return p
    brief.gather = fake_gather
    try:
        dop = asyncio.run(DP.daily_operating_picture())
    finally:
        brief.gather = orig
    kinds = [l["kind"] for l in dop["cross_domain_links"]]
    assert "weather_threatens_event" in kinds


def test_dop_needs_you_ranks_now_first():
    """What-needs-you is a real triage: urgent items (now) come before soon."""
    needs = DP._what_needs_you(
        {"whatsapp_waiting": [{"contact": "Sam"}]},
        {"checks": {"ollama": {"ok": False, "detail": "down"}}})
    # ollama down is a 'now' repair; reply to Sam is 'soon' -> now first
    assert needs[0]["urgency"] == "now"


def test_dop_next_step_prefers_urgent():
    step = DP._next_smallest_step(
        [{"urgency": "soon", "text": "reply to X"}, {"urgency": "now", "text": "approve Y"}],
        [])
    assert step == "approve Y"


def test_dop_next_step_calm_when_nothing():
    step = DP._next_smallest_step([], [])
    assert "Nothing needs you" in step


def test_dop_endpoint():
    r = client.get("/cockpit/daily")
    assert r.status_code == 200
    assert "todays_shape" in r.json() and "next_smallest_step" in r.json()


def test_dop_leads_overview():
    body = client.get("/ui").text
    assert "cockpit/daily" in body
    # The Overview now leads with the cross-domain brief as a HERO (UX refresh 0002): the connections
    # still render (from cross_domain_links), just framed as the lead synthesis rather than a sub-heading.
    assert "cross-domain brief" in body
    assert "cross_domain_links" in body


def test_dop_does_not_consume_what_changed(monkeypatch):
    """The DOP must PEEK at what-changed (acknowledge=False), not consume it, so the
    user's actual 'what changed' isn't reset by viewing the overview."""
    import mongoose.services.journal_layer as JL
    seen = {}

    def fake_wc(*, acknowledge=True):
        seen["acknowledge"] = acknowledge
        return {"changes": [], "detail": {}}
    monkeypatch.setattr(JL, "what_changed", fake_wc)
    asyncio.run(DP.daily_operating_picture())
    assert seen.get("acknowledge") is False


def test_dop_privacy_high_email_not_linked():
    """A high-privacy email must not leak its subject into cross-domain links."""
    init_db()
    now = datetime.now(timezone.utc)
    with connect() as con:
        con.execute(
            "INSERT INTO events(source,title,start_at,end_at,busy_status,sensitivity,last_seen) "
            "VALUES('test','Meeting with Alex',?,?,'busy','normal',?)",
            ((now - timedelta(hours=2)).isoformat(), (now + timedelta(hours=2)).isoformat(), now.isoformat()))
        con.execute(
            "INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
            "VALUES('test','Alex <alex@example.com>','SECRET medical result','x',?,'high',?)",
            (now.isoformat(), now.isoformat()))
        con.commit()
    dop = asyncio.run(DP.daily_operating_picture())
    txt = " ".join(l["text"] for l in dop["cross_domain_links"])
    assert "medical" not in txt.lower()   # high-privacy subject never surfaced
