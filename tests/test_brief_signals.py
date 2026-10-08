"""Tests for the expanded brief deterministic signal layer (v0.12.5):
email, notifications, memory, and selected home-state, plus the cross-domain
links and the privacy guarantees that make those domains safe to include."""
from datetime import datetime, timezone, timedelta

import pytest

from mongoose.db import init_db
from mongoose.services import brief


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def _now():
    return datetime.now(timezone.utc)


def _base(**over):
    pic = {
        "generated_at": _now().isoformat(),
        "events": [], "tasks": [], "weather": None, "bus_unread": [],
        "email_recent": [], "notifications_recent": [], "facts": [], "home_state": [],
    }
    pic.update(over)
    return pic


# --- no spurious signals on empty input --------------------------------------
def test_empty_picture_yields_no_signals(db):
    assert brief.cross_signals(_base()) == []


# --- email signals -----------------------------------------------------------
def test_ordinary_email_counted_and_named(db):
    pic = _base(email_recent=[
        {"sender": "Lawfirm LLP <a@k.com>", "subject": "Re", "privacy": "medium",
         "received_at": _now().isoformat()},
    ])
    sigs = brief.cross_signals(pic)
    assert any(s.startswith("EMAIL:") and "Lawfirm" in s for s in sigs)


def test_private_email_counted_but_never_detailed(db):
    pic = _base(email_recent=[
        {"sender": "secret@x.com", "subject": "DO NOT LEAK", "privacy": "high",
         "received_at": _now().isoformat()},
    ])
    sigs = brief.cross_signals(pic)
    joined = " ".join(sigs)
    assert "DO NOT LEAK" not in joined and "secret@x.com" not in joined
    assert any("sensitive" in s for s in sigs)


# --- notification signals ----------------------------------------------------
def test_urgent_notification_surfaced(db):
    pic = _base(notifications_recent=[
        {"app": "Calendar", "title": "Meeting in 15m", "privacy": "low",
         "urgency": "high", "received_at": _now().isoformat()},
    ])
    sigs = brief.cross_signals(pic)
    assert any("Calendar" in s and "Meeting" in s for s in sigs)


def test_private_notification_masked(db):
    pic = _base(notifications_recent=[
        {"app": "Health", "title": "SENSITIVE", "privacy": "high",
         "urgency": "critical", "received_at": _now().isoformat()},
    ])
    sigs = brief.cross_signals(pic)
    assert "SENSITIVE" not in " ".join(sigs)
    assert any("private alert" in s for s in sigs)


def test_routine_notifications_counted_not_listed(db):
    pic = _base(notifications_recent=[
        {"app": "News", "title": "h1", "privacy": "low", "urgency": "normal",
         "received_at": _now().isoformat()},
        {"app": "News", "title": "h2", "privacy": "low", "urgency": "normal",
         "received_at": _now().isoformat()},
    ])
    sigs = brief.cross_signals(pic)
    assert any("2 other notification" in s for s in sigs)


# --- memory signals (counts only, never the facts) ---------------------------
def test_memory_surfaces_only_facts_relevant_to_today(db):
    now = _now()
    # a fact that matches today's event is surfaced; an unrelated fact is not
    pic = _base(
        events=[{"title": "Dentist appointment", "start_at": now.isoformat(),
                 "end_at": (now + timedelta(hours=1)).isoformat()}],
        facts=[
            {"fact": "my dentist is Dr Smith", "source": "chat", "updated_at": now.isoformat()},
            {"fact": "drives a Honda", "source": "chat", "updated_at": now.isoformat()},
        ],
    )
    sigs = brief.cross_signals(pic)
    joined = " ".join(sigs)
    assert any(s.startswith("MEMORY:") for s in sigs)
    assert "dentist" in joined.lower()      # the relevant fact surfaced
    assert "Honda" not in joined            # the irrelevant fact did not


def test_memory_not_surfaced_without_today_context(db):
    now = _now()
    # facts present, but nothing on today's agenda to match them
    pic = _base(facts=[{"fact": "drives a Honda", "source": "chat", "updated_at": now.isoformat()}])
    sigs = brief.cross_signals(pic)
    assert not any(s.startswith("MEMORY:") for s in sigs)


def test_private_email_term_cannot_pull_a_sensitive_fact(db):
    now = _now()
    # a high-privacy email's subject is excluded from today-terms, so a fact that
    # would only match that private term must NOT be surfaced
    pic = _base(
        email_recent=[{"sender": "clinic", "subject": "dermatology results",
                       "privacy": "high", "received_at": now.isoformat()}],
        facts=[{"fact": "has a dermatology condition", "source": "chat", "updated_at": now.isoformat()}],
    )
    sigs = brief.cross_signals(pic)
    assert "dermatology" not in " ".join(sigs)


# --- home-state signals ------------------------------------------------------
def test_home_state_flags_unlocked_open_and_off(db):
    pic = _base(home_state=[
        {"entity_id": "lock.back", "state": "unlocked", "name": "Back door", "domain": "lock"},
        {"entity_id": "cover.garage", "state": "open", "name": "Garage", "domain": "cover"},
        {"entity_id": "climate.hall", "state": "off", "name": "Hall", "domain": "climate"},
        {"entity_id": "lock.front", "state": "locked", "name": "Front", "domain": "lock"},
    ])
    sigs = brief.cross_signals(pic)
    joined = " ".join(sigs)
    assert "Back door is unlocked" in joined
    assert "Garage is open" in joined
    assert "Hall is off" in joined
    assert "Front" not in joined  # a locked door is not noteworthy


# --- cross-domain links (the USP) --------------------------------------------
def test_cross_email_from_meeting_attendee(db):
    now = _now()
    pic = _base(
        events=[{"title": "Call with Lawfirm", "start_at": now.isoformat(),
                 "end_at": (now + timedelta(hours=1)).isoformat()}],
        email_recent=[{"sender": "legal@lawfirm.example", "subject": "Re", "privacy": "medium",
                       "received_at": now.isoformat()}],
    )
    sigs = brief.cross_signals(pic)
    assert any(s.startswith("CROSS:") and "meeting" in s for s in sigs)


def test_cross_heating_off_and_bad_weather(db):
    pic = _base(
        events=[{"title": "x", "start_at": _now().isoformat(),
                 "end_at": (_now() + timedelta(hours=1)).isoformat()}],
        weather={"rain_window": {"summary": "all afternoon"}},
        home_state=[{"entity_id": "climate.hall", "state": "off", "name": "Hall",
                     "domain": "climate"}],
    )
    sigs = brief.cross_signals(pic)
    assert any(s.startswith("CROSS:") and "heating is off" in s for s in sigs)


# --- query helpers -----------------------------------------------------------
def test_recent_email_window(db):
    from mongoose.services import email_index
    now = _now()
    with __import__("mongoose.db", fromlist=["connect"]).connect() as con:
        con.execute("INSERT INTO email_messages(source,source_message_id,subject,body,received_at,indexed_at,privacy) "
                    "VALUES('imap','a','Recent','x',?,?,'medium')",
                    (now.isoformat(), now.isoformat()))
        con.execute("INSERT INTO email_messages(source,source_message_id,subject,body,received_at,indexed_at,privacy) "
                    "VALUES('imap','b','Old','x',?,?,'medium')",
                    ((now - timedelta(days=3)).isoformat(), (now - timedelta(days=3)).isoformat()))
    rows = email_index.recent_email(hours=24)
    subjects = {r["subject"] for r in rows}
    assert "Recent" in subjects and "Old" not in subjects


def test_recent_notifications_window(db):
    from mongoose.services import notifications
    now = _now()
    with __import__("mongoose.db", fromlist=["connect"]).connect() as con:
        con.execute("INSERT INTO notifications(source,app,title,body,privacy,urgency,dedupe_key,received_at) "
                    "VALUES('push','App','New','x','low','high','k1',?)", (now.isoformat(),))
        con.execute("INSERT INTO notifications(source,app,title,body,privacy,urgency,dedupe_key,received_at) "
                    "VALUES('push','App','Stale','x','low','high','k2',?)",
                    ((now - timedelta(days=3)).isoformat(),))
    rows = notifications.recent_notifications(hours=24)
    titles = {r["title"] for r in rows}
    assert "New" in titles and "Stale" not in titles


# --- home_state reader degrades safely ---------------------------------------
@pytest.mark.asyncio
async def test_home_state_empty_when_not_configured(db, monkeypatch):
    monkeypatch.delenv("MONGOOSE_BRIEF_HOME_ENTITIES", raising=False)
    from mongoose.services.home_state import read_selected_home_state
    assert await read_selected_home_state() == []


# --- gather wires the new domains without raising ----------------------------
@pytest.mark.asyncio
async def test_gather_includes_new_domains(db):
    pic = await brief.gather()
    for key in ("email_recent", "notifications_recent", "facts", "home_state", "files_recent"):
        assert key in pic


# --- actionable email detection (adopted from the parallel patch) ------------
def test_actionable_email_flagged_over_routine(db):
    now = _now()
    pic = _base(email_recent=[
        {"sender": "billing@x.com", "subject": "Invoice overdue, payment required",
         "privacy": "medium", "received_at": now.isoformat()},
        {"sender": "news@y.com", "subject": "Weekly digest", "privacy": "low",
         "received_at": now.isoformat()},
    ])
    sigs = brief.cross_signals(pic)
    email_sig = next(s for s in sigs if s.startswith("EMAIL:"))
    assert "may need a reply" in email_sig  # the invoice is surfaced as needing a reply
    assert "billing@x.com" in email_sig
    assert "news@y.com" not in email_sig  # routine mail not named as actionable


def test_payment_receipt_not_flagged_as_actionable(db):
    """A payment/order confirmation is a receipt, never a reply-needed request (the Docmail
    'Payment received for order 70000001' false positive)."""
    now = _now()
    pic = _base(email_recent=[
        {"sender": "\"Docmail Support\" <docmailsupport@cfh.com>",
         "subject": "Docmail - Payment received for order 70000001",
         "privacy": "medium", "received_at": now.isoformat()},
    ])
    assert brief._is_automated_email(pic["email_recent"][0]) is True
    email_sig = next(s for s in brief.cross_signals(pic) if s.startswith("EMAIL:"))
    assert "may need a reply" not in email_sig
    assert "Docmail" not in email_sig  # the receipt sender is not named as needing attention


def test_forwarded_lone_urgent_not_flagged(db):
    """A lone 'urgent' inside a forwarded/replied subject is the quoted original's word, not the
    sender's, so it must not be promoted (the Harbour 'RE: FW: ... Urgent ...' insurance thread). A
    forwarded subject carrying a CONCRETE cue (invoice/due) still is actionable."""
    now = _now()
    inherited = {"sender": "Dan Broker <dan@broker.example>",
                 "subject": "RE: FW: Harbour Urgent request for verification of insurance",
                 "privacy": "medium", "received_at": now.isoformat()}
    concrete = {"sender": "Dan Broker <dan@broker.example>",
                "subject": "RE: FW: Invoice 88 overdue, payment required",
                "privacy": "medium", "received_at": now.isoformat()}
    assert brief._is_actionable_email(inherited) is False
    assert brief._is_actionable_email(concrete) is True


# --- grounding guard: the brief cannot ship urgency the signals do not support -----
def test_grounding_guard_rejects_inflated_urgency():
    """The resilient backstop: with no actionable signal, prose that invents urgency is rejected."""
    no_signal = []
    # The two historical lie-shapes.
    assert brief.brief_is_grounded(
        "You have an urgent request from Harbour that needs your attention today.", no_signal) is False
    assert brief.brief_is_grounded(
        "Some new threads to untangle , please reply to Jordan and Heather.", no_signal) is False


def test_grounding_guard_allows_honest_and_negated_and_grounded():
    no_signal = []
    actionable = ["EMAIL: 1 of 3 recent email(s) may need a reply: billing: Invoice overdue."]
    # Honest quiet day, and negated urgency, must pass with no signal.
    assert brief.brief_is_grounded(
        "Your calendar is clear; new mail from David to glance at, nothing needs a reply.", no_signal) is True
    assert brief.brief_is_grounded("Nothing urgent today, your day is clear.", no_signal) is True
    # When a signal genuinely flags action, urgency language is grounded.
    assert brief.brief_is_grounded("That invoice is urgent and needs a reply.", actionable) is True


# --- richer home-state classification ----------------------------------------
def test_home_state_flags_battery_and_lights(db):
    pic = _base(home_state=[
        {"entity_id": "sensor.remote_battery", "state": "12", "name": "Remote battery",
         "domain": "sensor", "device_class": "battery"},
        {"entity_id": "light.kitchen", "state": "on", "name": "Kitchen light",
         "domain": "light", "device_class": ""},
        {"entity_id": "light.hall", "state": "off", "name": "Hall light",
         "domain": "light", "device_class": ""},
    ])
    sigs = brief.cross_signals(pic)
    joined = " ".join(sigs)
    assert "battery is low" in joined
    assert "Kitchen light is on" in joined
    assert "Hall light" not in joined  # an off light is not notable


# --- files signal (indexed files as a brief signal) --------------------------
def test_files_relevant_to_today_named(db):
    now = _now()
    pic = _base(
        events=[{"title": "Review Teal poetry", "start_at": now.isoformat(),
                 "end_at": (now + timedelta(hours=1)).isoformat()}],
        files_recent=[
            {"path": "/d/teal.md", "title": "Teal poetry draft", "mtime": now.timestamp()},
            {"path": "/d/tax.xlsx", "title": "Tax return", "mtime": now.timestamp()},
        ],
    )
    sigs = brief.cross_signals(pic)
    fsig = next(s for s in sigs if s.startswith("FILES:"))
    assert "Teal" in fsig and "Tax" not in fsig


def test_files_count_when_none_relevant(db):
    now = _now()
    pic = _base(files_recent=[
        {"path": "/d/x.md", "title": "Unrelated notes", "mtime": now.timestamp()},
    ])
    sigs = brief.cross_signals(pic)
    assert any(s.startswith("FILES:") and "edited in the last day" in s for s in sigs)


def test_recent_files_window(db):
    from mongoose.services import file_index
    import time
    now = time.time()
    with __import__("mongoose.db", fromlist=["connect"]).connect() as con:
        con.execute("INSERT INTO files(path,title,body,mtime,indexed_at) VALUES('/a','Fresh','x',?,?)",
                    (now, "2026-06-14T00:00"))
        con.execute("INSERT INTO files(path,title,body,mtime,indexed_at) VALUES('/b','Old','x',?,?)",
                    (now - 3 * 86400, "2026-06-11T00:00"))
    rows = file_index.recent_files(hours=24)
    titles = {r["title"] for r in rows}
    assert "Fresh" in titles and "Old" not in titles
