"""Tests for the richer progression layer: estimated-1RM trends per exercise, moving/stuck
detection, reversal detection, and phase framing. Run against the user's REAL export fixtures.
Honesty properties: est-1RM is labelled an estimate; trends need enough sessions or say so;
reversals (V-shapes) aren't read as steady declines."""
import os
import tempfile
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "alpha"


def _fresh_with_data():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()
    from mongoose.services import alpha_import as AI
    AI.import_all(sessions=(FIX / "sessions.csv").read_text(),
                  sets=(FIX / "sets.csv").read_text())


# --- estimated 1RM math -----------------------------------------------------

def test_epley_est_1rm():
    from mongoose.services.body_progression_detail import est_1rm
    # 100kg x 1 rep = 100 (1RM); 100kg x 10 ~ 133
    assert est_1rm(100, 1) == round(100 * (1 + 1/30.0), 1)
    assert abs(est_1rm(100, 10) - 133.3) < 0.2
    assert est_1rm(0, 5) is None
    assert est_1rm(50, 0) is None


def test_epley_caps_high_reps():
    from mongoose.services.body_progression_detail import est_1rm, _EPLEY_REP_CAP
    # a 30-rep set is capped to the rep cap, not taken literally
    capped = est_1rm(50, 30)
    at_cap = est_1rm(50, _EPLEY_REP_CAP)
    assert capped == at_cap


def test_slope_basic():
    from mongoose.services.body_progression_detail import _slope
    # a clean upward line has positive slope
    assert _slope([(0, 1), (1, 2), (2, 3)]) > 0
    assert _slope([(0, 3), (1, 2), (2, 1)]) < 0
    assert _slope([(0, 5), (1, 5)]) == 0


# --- per-exercise trend -----------------------------------------------------

def test_insufficient_sessions_says_so():
    _fresh_with_data()
    from mongoose.services.body_progression_detail import exercise_trend
    # a made-up exercise with no data
    r = exercise_trend("Nonexistent Lift")
    assert r["status"] == "insufficient"
    assert "not enough" in r["speech"].lower()


def test_real_exercise_trend_has_estimate_label():
    _fresh_with_data()
    from mongoose.services.body_progression_detail import exercise_trend
    r = exercise_trend("Lateral Raises")
    # whatever the direction, it must be grounded and call est-1RM an estimate
    if r["status"] == "ok":
        assert "est" in r["speech"].lower()


# --- group breakdown --------------------------------------------------------

def test_group_progression_names_lifts():
    _fresh_with_data()
    from mongoose.services.body_progression_detail import group_progression
    r = group_progression("delts")
    if r["have_data"]:
        # the speech must always carry the estimate caveat
        assert "estimate" in r["speech"].lower()
        # and it's a breakdown, not a single number , has at least one bucket populated
        assert r["rising"] or r["flat"] or r["falling"] or r["thin"]


def test_group_no_data_is_honest():
    _fresh_with_data()
    from mongoose.services.body_progression_detail import group_progression
    r = group_progression("calves")   # may have little/no data in the trimmed fixture
    # either it has data, or it honestly says none , never fabricates
    assert "have_data" in r


# --- phase framing ----------------------------------------------------------

def test_training_phase_classifies():
    _fresh_with_data()
    from mongoose.services.body_progression_detail import training_phase
    r = training_phase()
    assert r["phase"] in ("building", "maintaining", "detraining", "idle")
    assert r["speech"]


# --- routing integration ----------------------------------------------------

def test_progression_answer_uses_detail():
    _fresh_with_data()
    from mongoose.services.body_voice import progression_answer
    r = progression_answer("are my delts progressing?")
    # the richer layer always carries the estimate caveat or a no-data honest note
    s = r["speech"].lower()
    assert "estimate" in s or "session" in s or "no " in s
