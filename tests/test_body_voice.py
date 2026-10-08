"""Tests for the body-voice register correction: answers come back in the user's actual
body-language (puffy/held/guarded/swim-day), grounded in real logs, and topics that aren't
built yet (progression/nutrition/journalling) are recognised HONESTLY, never faked."""
import os
import tempfile
from datetime import datetime, timezone, timedelta


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def _baseline_and_today(**today):
    from mongoose.services import body_state as BS
    now = datetime.now(timezone.utc)
    for d in range(2, 12):
        BS.log_body_state(hrv=55, resting_hr=52, sleep_hours=7.5, at=now - timedelta(days=d))
    BS.log_body_state(at=now, **today)


# --- register: answers in the user's words, not 'load signal' ---------------

def test_body_today_uses_natural_register():
    _fresh()
    _baseline_and_today(hrv=47, tension=4)
    from mongoose.services.body_voice import body_today
    speech = body_today()["speech"].lower()
    # the user's register, not sports-science
    # plain, concrete register , states the signal, no borrowed mood-words
    assert "recovery" in speech or "under-recovered" in speech or "below your usual" in speech
    assert "load signal" not in speech    # the thing we're correcting away from
    for borrowed in ("held", "cooked", "fresh", "tense but trainable"):
        assert borrowed not in speech     # words the user said mean nothing to them


def test_body_today_never_diagnoses():
    _fresh()
    _baseline_and_today(hrv=44, tension=5, sleep_hours=5.0)
    from mongoose.services.body_voice import body_today
    speech = body_today()["speech"].lower()
    assert "you are stressed" not in speech
    assert "you should rest" not in speech


# --- focused intents grounded in real logs ----------------------------------

def test_swim_or_lift_uses_history():
    _fresh()
    from mongoose.services import body_state as BS
    _baseline_and_today(hrv=46, tension=4)
    # an old swim that helped tension
    BS.log_swim(duration_min=30, tension_before=4, tension_after=2,
                at=datetime.now(timezone.utc) - timedelta(days=6))
    from mongoose.services.body_voice import swim_or_lift
    speech = swim_or_lift()["speech"].lower()
    assert "swim" in speech
    # references real history: days since, or that swimming has helped
    assert "days since" in speech or "helped" in speech


def test_puffiness_is_honest_about_unknown_cause():
    _fresh()
    _baseline_and_today(puffiness=4)
    from mongoose.services.body_voice import puffiness_today
    speech = puffiness_today()["speech"].lower()
    assert "puffy" in speech
    # must NOT claim to know water vs salt vs food (no nutrition data)
    assert "nutrition" in speech or "can't split" in speech or "cause" in speech


def test_right_side_reads_logged_state():
    _fresh()
    _baseline_and_today(right_left="right guarded")
    from mongoose.services.body_voice import right_side
    speech = right_side()["speech"].lower()
    assert "right" in speech
    assert "primer" in speech or "calf" in speech


# --- honesty: not-built topics recognised, never faked ----------------------

def test_progression_is_honestly_not_built_without_data():
    _fresh()   # no Alpha data imported
    from mongoose.services.body_voice import not_built_yet
    speech = not_built_yet("progression")["speech"].lower()
    # with no data, it honestly says import-needed and doesn't invent an analysis
    assert "import" in speech or "alpha" in speech or "sets" in speech


def test_nutrition_is_honestly_not_built():
    _fresh()
    from mongoose.services.body_voice import not_built_yet
    speech = not_built_yet("nutrition")["speech"].lower()
    assert "mfp" in speech or "food data" in speech or "nutrition" in speech


# --- router: the natural phrasings route correctly --------------------------

def test_natural_phrasings_route():
    from mongoose.role_router import looks_like_body_readiness as br, body_readiness_focus as bf
    cases = {
        "How's my body today?": "today",
        "Should I swim or lift?": "swim_or_lift",
        "Am I puffy today?": "puffy",
        "Is my right side being weird again?": "right_side",
        "Do I need physio?": "physio",
        "What's the best session today?": "best_session",
        "Are my delts progressing?": "nb:progression",
        "Did I hit enough protein?": "nb:nutrition",
    }
    for phrase, expected in cases.items():
        assert br(phrase), f"{phrase!r} should match body readiness"
        assert bf(phrase) == expected, f"{phrase!r} -> {bf(phrase)} (wanted {expected})"
    # "name what I'm feeling" is now a BUILT guided flow, not a not-built deflection
    from mongoose.role_router import looks_like_body_flow
    assert looks_like_body_flow("Help me name what I'm feeling")


def test_router_ignores_non_body():
    from mongoose.role_router import looks_like_body_readiness as br
    assert not br("turn off the lights")
    assert not br("are you ready?")     # Mongoose-readiness, not body
    assert not br("set an alarm")
    assert not br("what is the weather")
