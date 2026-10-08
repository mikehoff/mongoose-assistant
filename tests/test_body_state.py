"""Tests for Body State: the local body/movement ledger + the three-mode load estimate. The
load-bearing property is HONESTY , it never diagnoses, never asserts population research as a
personal fact, uses the user's OWN baseline, and falls back to clearly-flagged research
hypotheses (not a hollow 'uncertain') when there isn't enough personal data."""
import os
import tempfile
from datetime import datetime, timezone, timedelta


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


# --- ledger -----------------------------------------------------------------

def test_log_and_list_all_kinds():
    _fresh()
    from mongoose.services import body_state as BS
    BS.log_workout(focus="legs", rpe=8)
    BS.log_swim(duration_min=30, paired_sauna=True)
    BS.log_physio(focus="left calf", effect="helped")
    BS.log_body_comp(weight_kg=78.5, source="smart_scale", confidence="noisy")
    BS.log_body_state(load=3, tension=4, tags=["tense"])
    BS.log_journal(mode="checkin", body_signal="jaw", emotion_words=["tense"])
    for kind in BS.KINDS:
        assert len(BS.list_entries(kind)) == 1


def test_body_comp_keeps_confidence():
    _fresh()
    from mongoose.services import body_state as BS
    e = BS.log_body_comp(muscle_mass_kg=36.0, source="smart_scale", confidence="noisy")
    # consumer muscle-mass is directional , confidence must be preserved, not dropped
    assert e["confidence"] == "noisy"


def test_subjective_state_is_first_class():
    _fresh()
    from mongoose.services import body_state as BS
    e = BS.log_body_state(load=2, tension=3, right_left="right guarded", tags=["tense", "guarded"])
    assert e["right_left"] == "right guarded"
    assert "guarded" in e["tags"]


# --- three-mode load estimate -----------------------------------------------

def test_thin_data_gives_hypothesis_not_diagnosis():
    _fresh()
    from mongoose.services import body_state as BS, body_readiness as BR
    BS.log_body_state(sleep_hours=5.5, hrv=42, tension=4)   # one log, no baseline
    est = BR.load_estimate()
    assert est["mode"] == "hypothesis"
    # it must NOT assert a personal read; it must flag the patterns as general
    assert "general" in est["speech"].lower() or "not a read on you" in est["speech"].lower()
    assert est["hypotheses"]   # offers research-framed hypotheses


def test_hypothesis_never_claims_personal_fact():
    _fresh()
    from mongoose.services import body_state as BS, body_readiness as BR
    BS.log_body_state(sleep_hours=5.0, hrv=40)
    est = BR.load_estimate()
    # the HRV caveat from the cited research must be honest about inconsistency
    speech = est["speech"].lower()
    assert "inconsistent" in speech or "weak" in speech or "not a read on you" in speech


def test_enough_baseline_gives_personalised():
    _fresh()
    from mongoose.services import body_state as BS, body_readiness as BR
    now = datetime.now(timezone.utc)
    for d in range(2, 12):
        BS.log_body_state(hrv=55, resting_hr=52, sleep_hours=7.5, at=now - timedelta(days=d))
    BS.log_body_state(hrv=44, resting_hr=58, sleep_hours=5.5, tension=4, at=now)
    est = BR.load_estimate(now=now)
    assert est["mode"] == "personalised"
    # personalised reasons reference the user's OWN baseline, not population norms
    assert any("baseline" in r for r in est["reasons"])
    assert est["band"] in ("moderate", "elevated", "high")


def test_personalised_never_diagnoses():
    _fresh()
    from mongoose.services import body_state as BS, body_readiness as BR
    now = datetime.now(timezone.utc)
    for d in range(2, 12):
        BS.log_body_state(hrv=55, resting_hr=52, sleep_hours=7.5, at=now - timedelta(days=d))
    BS.log_body_state(hrv=44, resting_hr=58, sleep_hours=5.0, at=now)
    est = BR.load_estimate(now=now)
    speech = est["speech"].lower()
    # never says "you are stressed" / "you should rest" , stays a load signal + suggestion
    assert "you are stressed" not in speech
    assert "load signal" in speech
    assert est["disclaimer"]


def test_protective_actions_reduce_load():
    _fresh()
    from mongoose.services import body_state as BS, body_readiness as BR
    now = datetime.now(timezone.utc)
    for d in range(2, 12):
        BS.log_body_state(hrv=55, resting_hr=52, sleep_hours=7.5, at=now - timedelta(days=d))
    BS.log_body_state(hrv=49, resting_hr=54, sleep_hours=6.8, at=now)
    BS.log_swim(duration_min=25, paired_sauna=True, at=now)
    BS.log_physio(focus="left calf", at=now)
    est = BR.load_estimate(now=now)
    # the docs: notice what HELPS, not just load. walk/stretch/physio appear as protective.
    assert est["protective"]


# --- calibration loop (confirmed-for-you) -----------------------------------

def test_checkins_confirm_hypothesis_for_user():
    _fresh()
    from mongoose.services import body_readiness as BR
    for _ in range(BR._CONFIRM_MIN_CHECKINS):
        BR.record_checkin(felt_load=4, hypotheses_present=["sleep_strain"])
    assert "sleep_strain" in BR._confirmed_hypotheses()


def test_low_felt_load_does_not_confirm():
    _fresh()
    from mongoose.services import body_readiness as BR
    for _ in range(BR._CONFIRM_MIN_CHECKINS):
        BR.record_checkin(felt_load=1, hypotheses_present=["sleep_strain"])   # felt fine
    # a hypothesis isn't confirmed when the felt-load didn't actually match
    assert "sleep_strain" not in BR._confirmed_hypotheses()


# --- natural-language capture -----------------------------------------------

def test_voice_logging_detects_kinds():
    _fresh()
    from mongoose.services.body_log_parse import parse_body_log
    assert parse_body_log("log swim 30 min less puffy after")["kind"] == "swim"
    assert parse_body_log("log physio left calf helped")["kind"] == "physio"
    assert parse_body_log("body state: tense, right side guarded")["kind"] == "body_state"
    assert parse_body_log("log leg day, no back pain")["kind"] == "workout"
    assert parse_body_log("remind me to buy milk") is None


def test_voice_log_writes_to_ledger():
    _fresh()
    from mongoose.services.body_log_parse import log_from_text
    from mongoose.services import body_state as BS
    r = log_from_text("log swim 30 minutes with yoga")
    assert r["ok"] and r["kind"] == "swim"
    assert len(BS.list_entries("swim")) == 1


# --- intent routing ---------------------------------------------------------

def test_body_intents_and_negatives():
    from mongoose.role_router import looks_like_body_log as bl, looks_like_body_readiness as br
    assert bl("log leg day left calf weak")
    assert bl("weighed 70 kg today")
    assert br("how loaded am I today?")
    assert br("should I lift or swim today?")
    # negatives
    assert not bl("turn off the lights")
    assert not br("are you ready?")     # readiness-of-Mongoose, not body
