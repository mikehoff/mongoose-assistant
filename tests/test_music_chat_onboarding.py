"""Music onboarding is chat-native now (there is no Music tab anywhere). These tests pin the pieces
that make that work: a question asked in chat becomes pending, the owner's next chatty reply is
captured as its answer (resetting the unanswered streak), a stale question stops being pending, and an
opted-out scope never captures. DB isolation is automatic via conftest."""
from __future__ import annotations


def _mt(monkeypatch):
    from mongoose.services import music_taste as MT
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(MT, "_now", lambda: clock["t"])
    return MT, clock


def test_question_asked_in_chat_becomes_pending(monkeypatch):
    MT, _ = _mt(monkeypatch)
    MT.begin_if_new(MT.OWNER_SCOPE)
    MT.note_chat_activity(MT.OWNER_SCOPE)
    n = MT.onboarding_nudge(MT.OWNER_SCOPE, name="Alex")
    assert n and n["facet"]
    # onboarding_nudge only GENERATES the question; the delivering caller marks it asked once it is
    # actually shown (this split stops a generated-but-filtered question from silently eating the
    # owner's next message). Mirror that real flow here.
    MT.mark_asked(n["facet"], MT.OWNER_SCOPE)
    assert MT.pending_facet(MT.OWNER_SCOPE) == n["facet"]
    # asking without an answer bumps the unanswered streak
    assert MT._load(MT.OWNER_SCOPE)["unanswered_streak"] == 1


def test_capture_answer_records_and_resets_streak(monkeypatch):
    MT, _ = _mt(monkeypatch)
    MT.begin_if_new(MT.OWNER_SCOPE)
    MT.note_chat_activity(MT.OWNER_SCOPE)
    facet = MT.onboarding_nudge(MT.OWNER_SCOPE, name="Alex")["facet"]
    MT.mark_asked(facet, MT.OWNER_SCOPE)  # delivering caller marks it asked once shown
    got = MT.capture_answer("shoegaze and drum and bass", MT.OWNER_SCOPE)
    assert got == facet
    assert "shoegaze and drum and bass" in MT.profile(MT.OWNER_SCOPE)["facets"][facet]
    assert MT._load(MT.OWNER_SCOPE)["unanswered_streak"] == 0
    # once answered it is no longer pending
    assert MT.pending_facet(MT.OWNER_SCOPE) is None


def test_stale_question_is_not_pending(monkeypatch):
    MT, clock = _mt(monkeypatch)
    MT.begin_if_new(MT.OWNER_SCOPE)
    MT.note_chat_activity(MT.OWNER_SCOPE)
    MT.mark_asked(MT.onboarding_nudge(MT.OWNER_SCOPE, name="Alex")["facet"], MT.OWNER_SCOPE)
    assert MT.pending_facet(MT.OWNER_SCOPE) is not None
    clock["t"] += MT._ANSWER_CAPTURE_WINDOW_SECONDS + 60
    assert MT.pending_facet(MT.OWNER_SCOPE) is None
    # and capture is a no-op past the window
    assert MT.capture_answer("some late answer", MT.OWNER_SCOPE) is None


def test_opted_out_scope_never_pending(monkeypatch):
    MT, _ = _mt(monkeypatch)
    MT.begin_if_new(MT.OWNER_SCOPE)
    MT.note_chat_activity(MT.OWNER_SCOPE)
    MT.onboarding_nudge(MT.OWNER_SCOPE, name="Alex")
    MT.opt_out(MT.OWNER_SCOPE)
    assert MT.pending_facet(MT.OWNER_SCOPE) is None
    assert MT.capture_answer("jazz", MT.OWNER_SCOPE) is None
