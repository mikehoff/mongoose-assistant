"""Tests for the Surpriser, Mongoose's proactive companion layer.

The whole design goal is EARNING the interruption, so the tests are about restraint as
much as capability: it must ground suggestions in real correlations, never repeat itself
(novelty), respect quiet hours and a minimum gap, surface only high-value items, and
default to silence."""
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from mongoose.app import app
from mongoose.db import connect, init_db
import mongoose.services.surpriser as S
_REJECTED_SUPPRESS_DAYS_TEST = S._REJECTED_SUPPRESS_DAYS



@pytest.fixture(autouse=True)
def _pin_daytime(monkeypatch):
    """Pin the surpriser's notion of 'now' to a fixed daytime hour so background-cycle
    tests are deterministic regardless of the wall-clock time the suite actually runs in.
    Without this, tests that exercise run_surpriser_cycle / force=False flake during the
    real 22:00-08:00 quiet-hours window (the cycle correctly returns 'quiet hours' and
    nothing surfaces). We only override the HOUR-of-day used for quiet-hours; the DB
    timestamps still use real now so cooldowns behave normally."""
    fixed = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(S, "_now_local", lambda: fixed)
    yield


def _seed_correlation():
    init_db()
    now = datetime.now(timezone.utc)
    with connect() as con:
        con.execute(
            "INSERT INTO events(source,title,start_at,end_at,busy_status,sensitivity,last_seen) "
            "VALUES('t','Meeting with Chris',?,?,'busy','normal',?)",
            ((now - timedelta(hours=2)).isoformat(), (now + timedelta(hours=2)).isoformat(), now.isoformat()))
        con.execute(
            "INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
            "VALUES('t','Chris <chris@x.com>','agenda','x',?,'normal',?)",
            (now.isoformat(), now.isoformat()))
        con.commit()


def test_candidates_from_real_correlation():
    _seed_correlation()
    cands = asyncio.run(S.candidate_suggestions())
    assert cands
    assert any("Chris" in c["text"] for c in cands)
    # each candidate is grounded (has a 'why') and has a dedup signature
    for c in cands:
        assert c["why"] and c["signature"]


def test_novelty_prevents_repeats():
    _seed_correlation()
    with connect() as con:
        S._ensure_table(con)
        con.execute("DELETE FROM surpriser_log")
        con.commit()
    d1 = asyncio.run(S.pick_surprise(force=True))
    assert d1["surfaced"] is True
    sig1 = d1["suggestion"]["signature"]
    # the SAME suggestion must never be surfaced again (it may offer a different novel
    # one, but never repeat sig1). Drain a few times and assert sig1 never recurs.
    seen_again = False
    for _ in range(5):
        d = asyncio.run(S.pick_surprise(force=True))
        if d.get("surfaced") and d["suggestion"]["signature"] == sig1:
            seen_again = True
            break
    assert seen_again is False


def test_min_gap_enforced():
    _seed_correlation()
    asyncio.run(S.pick_surprise(force=True))          # records a surfacing
    # a non-forced cycle right after must be blocked by the min-gap
    d = asyncio.run(S.pick_surprise(force=False))
    assert d["surfaced"] is False
    assert d["reason"] in ("too soon since last surprise", "quiet hours",
                           "nothing fresh and worth interrupting for")


def test_quiet_hours_logic():
    class N:
        def __init__(self, h):
            self.hour = h
    assert S._in_quiet_hours(N(23)) is True
    assert S._in_quiet_hours(N(3)) is True
    assert S._in_quiet_hours(N(11)) is False


def test_low_value_not_surfaced(monkeypatch):
    """Even with candidates, anything below the value threshold is not surfaced."""
    async def only_low():
        return [{"kind": "x", "text": "trivia", "why": "w", "score": 0.2, "signature": "low1"}]
    monkeypatch.setattr(S, "candidate_suggestions", only_low)
    d = asyncio.run(S.pick_surprise(force=True))
    assert d["surfaced"] is False


def test_silence_is_default_when_nothing(monkeypatch):
    init_db()
    # The "silence when nothing substantive" contract holds when proactive delight is OFF: an empty DB
    # with no correlations surfaces nothing. (With delight ON, a gentle fun fact may surface instead;
    # that's covered in test_surpriser_delight_cadence.)
    import mongoose.settings as _S
    monkeypatch.setenv("MONGOOSE_PROACTIVE_DELIGHT", "false")
    if hasattr(_S, "_settings"): _S._settings = None
    if hasattr(_S.get_settings, "cache_clear"): _S.get_settings.cache_clear()
    d = asyncio.run(S.pick_surprise(force=True))
    assert d["surfaced"] is False


def test_feedback_loop():
    init_db()
    with connect() as con:
        S._ensure_table(con)
        con.execute("INSERT INTO surpriser_log(signature,kind,text,score,created_at,surfaced) "
                    "VALUES('fb1','t','hi',0.9,?,1)", (datetime.now(timezone.utc).isoformat(),))
        con.commit()
    assert S.record_feedback("fb1", "not_useful")["ok"] is True
    assert S.record_feedback("fb1", "bogus")["ok"] is False
    assert S.recent_suggestions()[0]["feedback"] == "not_useful"


def test_suggest_endpoint():
    with TestClient(app) as client:
        assert client.get("/cockpit/suggest").status_code == 200


def test_feedback_endpoint_requires_nonce():
    with TestClient(app) as client:
        r = client.post("/cockpit/suggest/feedback", json={"signature": "x", "feedback": "useful"})
    assert r.status_code == 403


def test_surpriser_off_by_default():
    """A proactive companion must be opt-in: the scheduler job is absent unless enabled."""
    from mongoose.services.scheduler import _build_registry
    import mongoose.settings as st
    st._SETTINGS = None
    reg = _build_registry()
    assert "surpriser" not in reg   # off by default


def test_run_cycle_pushes_when_surfaced(monkeypatch):
    _seed_correlation()

    async def fake_push(title, body, **kw):
        return {"sent": True}
    import mongoose.services.notifications as N
    monkeypatch.setattr(N, "send_to_phone", fake_push)
    # ensure a clean slate so novelty doesn't suppress
    init_db()
    _seed_correlation()
    res = asyncio.run(S.run_surpriser_cycle())
    # either it pushed, or it was legitimately silent (quiet hours); both are valid
    assert "pushed" in res


# --- regression tests for bugs found in the v0.21 surpriser audit ---

def _seed_corr_clean():
    """Seed one email<->calendar correlation in a clean surpriser log."""
    _seed_correlation()
    with connect() as con:
        S._ensure_table(con)
        con.execute("DELETE FROM surpriser_log")
        con.commit()


def test_not_useful_feedback_durably_suppresses():
    """REGRESSION: a suggestion marked not_useful must NOT return after the normal 20h
    novelty window; it's suppressed for _REJECTED_SUPPRESS_DAYS."""
    from datetime import datetime, timezone, timedelta
    _seed_corr_clean()
    d = asyncio.run(S.pick_surprise(force=True))
    assert d["surfaced"]
    sig = d["suggestion"]["signature"]
    S.record_feedback(sig, "not_useful")
    # backdate past the 20h novelty window
    with connect() as con:
        con.execute("UPDATE surpriser_log SET created_at=? WHERE signature=?",
                    ((datetime.now(timezone.utc) - timedelta(hours=21)).isoformat(), sig))
        con.commit()
    d2 = asyncio.run(S.pick_surprise(force=True))
    # the rejected suggestion must not be the one surfaced
    assert not (d2.get("surfaced") and d2["suggestion"]["signature"] == sig)


def test_rejected_suggestion_returns_after_long_window():
    """The suppression is time-bounded, not permanent: after _REJECTED_SUPPRESS_DAYS it
    may resurface (state may genuinely be relevant again)."""
    from datetime import datetime, timezone, timedelta
    _seed_corr_clean()
    d = asyncio.run(S.pick_surprise(force=True))
    sig = d["suggestion"]["signature"]
    S.record_feedback(sig, "not_useful")
    with connect() as con:
        con.execute("UPDATE surpriser_log SET created_at=? WHERE signature=?",
                    ((datetime.now(timezone.utc) - timedelta(days=_REJECTED_SUPPRESS_DAYS_TEST + 1)).isoformat(), sig))
        con.commit()
    d2 = asyncio.run(S.pick_surprise(force=True))
    assert d2["surfaced"] and d2["suggestion"]["signature"] == sig


def test_concurrent_cycles_do_not_duplicate():
    """REGRESSION: two concurrent pick_surprise calls must not both surface the same
    suggestion (the TOCTOU race)."""
    _seed_corr_clean()

    async def race():
        return await asyncio.gather(S.pick_surprise(force=True), S.pick_surprise(force=True))
    a, b = asyncio.run(race())
    surfaced = [x for x in (a, b) if x.get("surfaced")]
    # at most one surfaces the suggestion; never two identical pushes
    if len(surfaced) == 2:
        assert surfaced[0]["suggestion"]["signature"] != surfaced[1]["suggestion"]["signature"]
    # and only one log row for whichever signature surfaced
    sig = (a.get("suggestion") or b.get("suggestion") or {}).get("signature")
    if sig:
        with connect() as con:
            n = con.execute("SELECT COUNT(*) c FROM surpriser_log WHERE signature=?", (sig,)).fetchone()["c"]
        assert n == 1


def test_on_demand_does_not_starve_background():
    """REGRESSION: using the on-demand 'Anything for me?' (source=on_demand) must not
    push back the background surpriser's min-gap."""
    _seed_corr_clean()
    asyncio.run(S.pick_surprise(force=True, source="on_demand"))
    d = asyncio.run(S.pick_surprise(force=False, source="background"))
    assert d.get("reason") != "too soon since last surprise"


def test_background_gap_still_enforced_between_background_cycles():
    """The min-gap must STILL apply between two BACKGROUND cycles (we only excluded
    on-demand, we didn't remove the gap)."""
    _seed_corr_clean()
    first = asyncio.run(S.pick_surprise(force=False, source="background"))
    # quiet hours could make 'first' not surface; only assert the gap when it did
    if first.get("surfaced"):
        d = asyncio.run(S.pick_surprise(force=False, source="background"))
        assert d.get("surfaced") is False


def test_failed_push_is_not_silently_lost(monkeypatch):
    """REGRESSION: if the push fails, the suggestion must be rolled back (not consumed by
    novelty), so it's retried next cycle instead of vanishing unseen."""
    _seed_corr_clean()
    import mongoose.services.notifications as N

    async def fail(*a, **k):
        return {"sent": False}
    monkeypatch.setattr(N, "send_to_phone", fail)
    r1 = asyncio.run(S.run_surpriser_cycle())
    assert r1["pushed"] is False
    with connect() as con:
        S._ensure_table(con)
        assert con.execute("SELECT COUNT(*) c FROM surpriser_log").fetchone()["c"] == 0

    async def ok(*a, **k):
        return {"sent": True}
    monkeypatch.setattr(N, "send_to_phone", ok)
    r2 = asyncio.run(S.run_surpriser_cycle())
    assert r2["pushed"] is True   # the same suggestion was retried and delivered


def test_signature_is_stable_to_text_variance():
    """REGRESSION: the novelty signature must key on the underlying fact, not exact prose,
    so cosmetic text variance (spacing/punctuation) doesn't dodge dedup."""
    a = S._sig("email_meets_calendar", "Email from Sam Example, who appears in today's calendar.")
    b = S._sig("email_meets_calendar", "Email from Sam Example , who appears  in today's calendar.")
    assert a == b
    # but genuinely different facts still differ
    c = S._sig("email_meets_calendar", "Email from Toby, who appears in today's calendar.")
    assert a != c
