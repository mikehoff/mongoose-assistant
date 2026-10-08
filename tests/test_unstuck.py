"""Tests for the unstucking / active-recovery layer. The load-bearing property: it encodes
'recovery is active and playful, never passive', so it must NEVER emit rest/break/downtime framings,
and it suggests active things from the user's repertoire. Plus: honest detection, honest learning."""
import os
import tempfile
from datetime import datetime, timezone, timedelta


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def _make_stuck(now):
    from mongoose.services import body_state as BS
    for d in range(2, 12):
        BS.log_body_state(hrv=55, resting_hr=52, sleep_hours=7.5, at=now - timedelta(days=d))
    BS.log_body_state(hrv=44, resting_hr=58, sleep_hours=5.5, tension=5, at=now)
    BS.log_body_state(tension=4, tags=["tense"], at=now - timedelta(days=1))
    BS.log_body_state(tension=4, tags=["overwhelmed"], at=now - timedelta(days=2))


# --- the core guardrail: NEVER passive framing ------------------------------

def test_never_says_rest_or_break():
    _fresh()
    from mongoose.services import unstuck as U
    now = datetime.now(timezone.utc)
    _make_stuck(now)
    # generate many outputs , none may contain the forbidden passive framings
    for _ in range(40):
        speech = U.unstick(now=now)["speech"].lower()
        for bad in ("take a break", "downtime", "wind down", "time off", "have a rest",
                    " rest", "relax", "switch off", "do nothing"):
            assert bad not in speech, f"emitted forbidden framing {bad!r}: {speech}"


def test_suggestions_are_active_doings():
    _fresh()
    from mongoose.services import unstuck as U
    now = datetime.now(timezone.utc)
    _make_stuck(now)
    # the suggestion is one of the repertoire's active things
    seen = set()
    for _ in range(40):
        seen.add(U.unstick(now=now)["suggestion"])
    assert seen.issubset({i["id"] for i in U._REPERTOIRE})
    # and it actually varies (not the same nudge every time)
    assert len(seen) >= 2


def test_guardrail_raises_if_forbidden():
    from mongoose.services import unstuck as U
    # the internal assert_clean must catch a forbidden framing
    try:
        U._assert_clean("You should take a break and rest")
        assert False, "should have raised"
    except AssertionError as e:
        assert "forbidden" in str(e).lower()


# --- detection: a pattern read, not one number ------------------------------

def test_detect_stuck_reads_cluster():
    _fresh()
    from mongoose.services import unstuck as U
    now = datetime.now(timezone.utc)
    _make_stuck(now)
    det = U.detect_stuck(now=now)
    assert det["stuck"]
    assert "under-recovered" in det["signals"] or "carrying tension" in det["signals"]


def test_not_stuck_is_honest():
    _fresh()
    from mongoose.services import unstuck as U
    now = datetime.now(timezone.utc)
    from mongoose.services import body_state as BS
    # a fine day: good sleep, no tension, a recent swim (play logged)
    for d in range(1, 10):
        BS.log_body_state(hrv=56, resting_hr=51, sleep_hours=7.8, at=now - timedelta(days=d))
    BS.log_swim(duration_min=25, at=now - timedelta(days=1))
    det = U.detect_stuck(now=now)
    assert not det["stuck"]
    # and unstick (without force) says so rather than inventing a problem
    r = U.unstick(now=now)
    assert not r["stuck"]


# --- learning: what actually helps THIS person ------------------------------

def test_learns_what_helps():
    _fresh()
    from mongoose.services import unstuck as U
    for _ in range(3):
        U.record_did("water", helped=True)
    U.record_did("yoga", helped=True)
    U.record_did("retro", helped=False)
    r = U.what_helps()
    assert r["have_data"]
    assert "water" in r["speech"].lower()


def test_what_helps_honest_when_no_data():
    _fresh()
    from mongoose.services import unstuck as U
    r = U.what_helps()
    assert not r["have_data"]
    assert "not enough" in r["speech"].lower()


def test_did_without_helped_is_not_forced():
    _fresh()
    from mongoose.services import unstuck as U
    # recording a 'did' without volunteering helped must not fabricate a helped=True
    U.record_did("dog")   # no helped
    weights = U._helped_weights()
    assert "dog" not in weights   # nothing learned because helpfulness wasn't volunteered


# --- routing ----------------------------------------------------------------

def test_unstuck_intent_and_negatives():
    from mongoose.role_router import looks_like_unstuck as u
    assert u("I am stuck")
    assert u("help me get unstuck")
    assert u("I've been pushing too hard")
    # negatives , these belong to other layers
    assert not u("how's my body today")
    assert not u("are my delts progressing")
    assert not u("turn off the lights")
