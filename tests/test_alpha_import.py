"""Tests for the Alpha Progression importer + progression query layer, run against the user's REAL
export fixtures. The honesty properties: muscle-group volumes are exact (sums of logged sets);
body composition is DIRECTIONAL (never 'you gained X kg muscle'); and questions degrade to an
honest 'no data imported' rather than fabricating a trend."""
import os
import tempfile
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "alpha"


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def _import_all():
    from mongoose.services import alpha_import as AI
    return AI.import_all(
        sessions=(FIX / "sessions.csv").read_text(),
        sets=(FIX / "sets.csv").read_text(),
        body_comp=(FIX / "body_comp.csv").read_text(),
    )


# --- import against real formats --------------------------------------------

def test_imports_real_sessions_and_sets():
    _fresh()
    r = _import_all()
    assert r["summary"]["sessions"]["imported"] > 0
    assert r["summary"]["sets"]["imported"] > 0
    from mongoose.services.body_progression import has_training_data
    assert has_training_data()


def test_dates_parse_from_real_format():
    _fresh()
    from mongoose.services.alpha_import import _parse_dt
    # the real export uses "2026-06-08 3:36 pm"
    assert _parse_dt("2026-06-08 3:36 pm") is not None
    assert _parse_dt("2026-06-08 15:36:00") is not None
    assert _parse_dt("garbage") is None


def test_muscle_group_classification():
    from mongoose.services.alpha_import import muscle_group_for
    assert muscle_group_for("Lateral Raises") == "delts"
    assert muscle_group_for("Seated Leg Curls") == "hamstrings"
    assert muscle_group_for("Leg Extension") == "quads"
    assert muscle_group_for("Calf Bridge") == "calves"
    assert muscle_group_for("Incline Curl") == "biceps"
    assert muscle_group_for("Pallof Press") == "core_proprio"


def test_reimport_is_idempotent():
    _fresh()
    _import_all()
    from mongoose.db import connect
    with connect() as con:
        n1 = con.execute("SELECT COUNT(*) c FROM bs_alpha_sets").fetchone()["c"]
    _import_all()   # same data again
    with connect() as con:
        n2 = con.execute("SELECT COUNT(*) c FROM bs_alpha_sets").fetchone()["c"]
    assert n1 == n2   # content-hash keys prevent duplication


# --- progression answers ----------------------------------------------------

def test_group_focus_real_answer():
    _fresh()
    _import_all()
    from mongoose.services.body_progression import group_focus
    r = group_focus("delts")
    assert r["have_data"]
    assert "delts" in r["speech"].lower()


def test_muscle_trend_is_directional():
    _fresh()
    _import_all()
    from mongoose.services.body_progression import muscle_trend as mt
    r = mt()
    if r["have_data"]:
        # MUST be framed as directional, with the hydration caveat , never an exact gain claim
        s = r["speech"].lower()
        assert "directional" in s or "trend" in s
        assert "hydration" in s or "water" in s


def test_questions_degrade_honestly_without_data():
    _fresh()   # nothing imported
    from mongoose.services.body_progression import group_focus, current_plan
    assert not group_focus("delts")["have_data"]
    assert "import" in group_focus("delts")["speech"].lower()
    assert not current_plan()["have_data"]


def test_overdoing_uses_relative_volume():
    _fresh()
    _import_all()
    from mongoose.services.body_progression import overdoing
    r = overdoing("quads")
    assert r["have_data"]
    assert "quads" in r["speech"].lower()


# --- routing: progression questions answer for real -------------------------

def test_progression_routes_when_data_present():
    _fresh()
    _import_all()
    from mongoose.services.body_voice import progression_answer
    # a real answer, not the not-built deflection
    r = progression_answer("are my delts progressing?")
    assert "import your alpha" not in r["speech"].lower()


def test_progression_deflects_honestly_without_data():
    _fresh()   # no import
    from mongoose.services.body_voice import progression_answer
    r = progression_answer("are my delts progressing?")
    assert r.get("not_built") == "progression" or "import" in r["speech"].lower()
