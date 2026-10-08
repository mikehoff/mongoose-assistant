"""Mongoose's music-taste streak: profile, escalating onboarding schedule, quirky recs, surpriser hook."""
from __future__ import annotations

import sqlite3
import time

from fastapi.testclient import TestClient


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def test_record_and_profile(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    assert MT.record_answer("fave_singers", "Kate Bush")["ok"] is True
    assert MT.record_answer("badfacet", "x")["ok"] is False
    assert MT.record_answer("fave_singers", "")["ok"] is False
    prof = MT.profile()
    assert prof["facets"]["fave_singers"] == ["Kate Bush"]
    assert "Kate Bush" in MT.profile_summary()


def test_escalating_schedule(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    MT.begin_if_new()  # proactive music is on by default; this starts the interview clock
    db = str(tmp_path / "m.sqlite3")

    def backdate(secs_ago):
        con = sqlite3.connect(db)
        con.execute("UPDATE music_taste SET started_at=?, last_asked_at=0 WHERE scope='owner'",
                    (time.time() - secs_ago,))
        con.commit(); con.close()

    # Decay 1,2,4,8,10,10: 1min for first 2min, 2min for next 4 (cum 6), 4min for next 8 (cum 14),
    # 8min for next 16 (cum 30), 10min for next 20 (cum 50), 10min for next 20 (cum 70), then normal.
    checks = [(0.5, 60), (1.9, 60), (2.5, 120), (5.9, 120), (6.5, 240), (13.9, 240),
              (14.5, 480), (29.9, 480), (30.5, 600), (49.9, 600), (50.5, 600), (69.9, 600),
              (70.5, None)]
    for ago_min, expect in checks:
        backdate(ago_min * 60)
        assert MT.onboarding_interval_seconds() == expect, f"at {ago_min}min expected {expect}"


def test_onboarding_nudges_advance_through_facets(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    MT.opt_in()  # opting in starts the interview clock
    # force "due" by zeroing last_asked and putting us in the 1-min band
    db = str(tmp_path / "m.sqlite3")
    seen = set()
    for _ in range(len(MT.FACETS) + 2):
        con = sqlite3.connect(db)
        # 10s in -> 1-min band, last_asked=0 -> due; last_chat_at=now -> the owner is chatting, which
        # is required for a nudge to fire (the schedule is chat-gated: it freezes during silence).
        con.execute("UPDATE music_taste SET started_at=?, last_asked_at=0, last_chat_at=? WHERE scope='owner'",
                    (time.time() - 10, time.time()))
        con.commit(); con.close()
        n = MT.onboarding_nudge()
        if n is None:
            break
        assert n["facet"] in MT.FACETS
        seen.add(n["facet"])
        MT.mark_asked(n["facet"])  # the delivering caller marks it asked; that is what advances facets
    assert len(seen) >= 5  # marched through several distinct facets


def test_onboarded_after_enough_answers(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    for f in MT.FACETS[:7]:
        MT.record_answer(f, "something")
    assert MT.profile()["onboarded"] is True
    assert MT.is_onboarding() is False


def test_recommendation_is_quirky_and_fresh(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    with_song = MT.recommendation_text("Running Up That Hill by Kate Bush")
    assert "Running Up That Hill by Kate Bush" in with_song
    # no song -> an open question, never a fabricated title
    open_q = MT.recommendation_text("")
    assert open_q and "by" not in open_q.split("?")[0].lower() or "?" in open_q
    # freshness: several draws yield multiple phrasings
    draws = {MT.recommendation_text("X by Y") for _ in range(12)}
    assert len(draws) >= 3


def test_surpriser_music_is_on_by_default_and_silenced_only_by_opt_out(monkeypatch, tmp_path):
    """Current contract (reverses the earlier opt-in gate): proactive music curiosity is part of who
    Mongoose is, so it surfaces BY DEFAULT for a fresh scope. The one and only thing that silences it
    is an explicit opt-out."""
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("MONGOOSE_PROACTIVE_DELIGHT_ENABLED", "true")
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import asyncio
    import mongoose.services.surpriser as SP
    import mongoose.services.music_taste as MT
    # fresh, un-opted-in scope -> music surfaces anyway (default on)
    cands0 = asyncio.new_event_loop().run_until_complete(SP.candidate_suggestions())
    assert any(c.get("kind") in ("music_interview", "music_rec") for c in cands0)
    # explicit opt-out -> music goes silent
    MT.opt_out()
    cands1 = asyncio.new_event_loop().run_until_complete(SP.candidate_suggestions())
    assert not any(c.get("kind") in ("music_interview", "music_rec") for c in cands1)


def test_blank_env_starts_music_by_default_and_opt_out_suppresses(monkeypatch, tmp_path):
    """Current contract: proactive music is on by default. begin_if_new starts the interview clock for
    a fresh scope (it is NOT gated on an opt-in), so onboarding runs. An explicit opt-out is what stops
    it: after opting out, begin_if_new is a no-op and no question is ever pushed."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    assert MT.has_opted_in() is False
    # begin_if_new starts the clock by default (proactive music is part of the persona)
    assert MT.begin_if_new().get("started_at") is not None
    assert MT.is_onboarding() is True
    # answering a music question is itself an opt-in
    MT.record_answer("fave_genres", "shoegaze")
    assert MT.has_opted_in() is True

    # a scope that has explicitly opted out gets nothing (distinct scope to keep the DB clean)
    sc = "opted_out_scope"
    MT.opt_out(sc)
    assert MT.begin_if_new(sc).get("started_at") is None   # no-op once opted out
    assert MT.is_onboarding(sc) is False
    assert MT.onboarding_nudge(sc) is None


def test_persona_carries_music_quirk():
    from mongoose.persona import persona_card
    assert "Music streak" in persona_card()


def test_endpoints(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    assert c.get("/cockpit/music").json()["ok"] is True
    assert c.post("/cockpit/music/answer", json={"facet": "fave_genres", "value": "trip-hop"}).json()["ok"] is True
    assert "trip-hop" in str(c.get("/cockpit/music").json()["profile"])
    rec = c.get("/cockpit/music/recommend", params={"song": "Teardrop by Massive Attack"}).json()
    assert "Teardrop by Massive Attack" in rec["text"]


def test_two_unanswered_questions_slow_the_next_by_an_hour(monkeypatch, tmp_path):
    """After two music questions go unanswered in a row, the next one is slowed by an hour; answering
    clears the penalty and restores the fast cadence."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    clock = {"t": time.time()}
    monkeypatch.setattr(MT, "_now", lambda: clock["t"])
    MT.begin_if_new(); MT.note_chat_activity()
    _n1 = MT.onboarding_nudge(name="Alex"); assert _n1 is not None  # ask 1
    MT.mark_asked(_n1["facet"])                                     # delivering marks it -> streak 1
    clock["t"] += MT.onboarding_interval_seconds() + 1
    MT.note_chat_activity()
    _n2 = MT.onboarding_nudge(name="Alex"); assert _n2 is not None  # ask 2
    MT.mark_asked(_n2["facet"])                                     # streak -> 2
    assert MT._load(MT.OWNER_SCOPE)["unanswered_streak"] == 2
    assert MT.onboarding_interval_seconds() >= 3600              # +1h penalty in force
    MT.record_answer("fave_genres", "dnb")                       # answering clears it
    assert MT._load(MT.OWNER_SCOPE)["unanswered_streak"] == 0
    assert MT.onboarding_interval_seconds() < 3600


def test_schedule_is_chat_gated_and_freezes_during_silence(monkeypatch, tmp_path):
    """No proactive question fires unless the owner has been chatting recently; going quiet freezes the
    schedule (started_at slides forward) so it resumes where it left off rather than fast-forwarding
    off the end of the escalating window."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    clock = {"t": time.time()}
    monkeypatch.setattr(MT, "_now", lambda: clock["t"])
    MT.begin_if_new()
    clock["t"] += 5 * 60
    assert MT.due_for_onboarding_nudge() is False               # silent -> nothing
    MT.note_chat_activity()
    assert MT.due_for_onboarding_nudge() is True                # chatting -> due

    started_before = MT._load(MT.OWNER_SCOPE)["started_at"]
    clock["t"] += 3 * 24 * 3600                                 # days of silence
    MT.note_chat_activity()                                     # ...then return
    started_after = MT._load(MT.OWNER_SCOPE)["started_at"]
    assert started_after - started_before >= 3 * 24 * 3600 - 1  # clock slid forward by the idle gap
    assert MT.onboarding_interval_seconds() is not None         # still an early band, not fallen off


def test_onboarding_question_opens_with_by_the_way_name(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.music_taste as MT
    clock = {"t": time.time()}
    monkeypatch.setattr(MT, "_now", lambda: clock["t"])
    MT.begin_if_new(); MT.note_chat_activity()
    n = MT.onboarding_nudge(name="Alex")
    assert n is not None and n["text"].startswith("By the way Alex, ")
    # no name -> still a clean opener (distinct scope to keep the DB clean)
    sc = "noname_scope"
    MT.begin_if_new(sc); MT.note_chat_activity(sc)
    n2 = MT.onboarding_nudge(sc)
    assert n2 is not None and n2["text"].startswith("By the way, ")
