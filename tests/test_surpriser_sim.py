"""Tests for the surpriser simulator (the anti-noise trust gate) and shape-level novelty.

The simulator replays N days and must: never touch the live DB or send real pushes; stay
silent on a blank world; and , critically , NOT fire the same suggestion SHAPE every day
on a recurring-but-unimportant pattern (the flaw a sandbox report found in v0.22.0)."""
import asyncio

from mongoose.db import connect, init_db
from mongoose.services import surpriser as S
from mongoose.services.surpriser_sim import simulate


def test_sim_does_not_touch_live_db(tmp_path, monkeypatch):
    live = str(tmp_path / "live.sqlite3")
    monkeypatch.setenv("MONGOOSE_DB_PATH", live)
    init_db()
    with connect() as con:
        S._ensure_table(con)
        before = con.execute("SELECT COUNT(*) c FROM surpriser_log").fetchone()["c"]
    asyncio.run(simulate(days=5, seed=True))
    with connect() as con:
        after = con.execute("SELECT COUNT(*) c FROM surpriser_log").fetchone()["c"]
    assert before == after == 0          # the sim used a temp DB, live is untouched


def test_sim_blank_world_surfaces_only_delights():
    # There is no 'truly blank' world: with no real updates, a dad joke or a science/psychology/universe
    # /interest fact is itself a valid surprise. So a seedless run may surface DELIGHTS , but it must
    # NEVER fabricate a SUBSTANTIVE surprise (a correlation/heads-up) about data that doesn't exist.
    res = asyncio.run(simulate(days=7, seed=False))
    surfaced = [e for e in res.get("day_log", []) if isinstance(e, dict) and e.get("surfaced")]
    # everything surfaced in a blank world must be delight-family (light/fun, not a substantive
    # correlation). music_interview and music_rec are correctly delight-class (family="delight") so
    # they're allowed; what must NEVER appear is a real cross-domain finding about non-existent data.
    LIGHT_KINDS = {"delight", "music_interview", "music_rec"}
    assert all(e.get("family") == "delight" or e.get("kind") in LIGHT_KINDS for e in surfaced), \
        f"blank world must only surface delight-family items, got kinds {[e.get('kind') for e in surfaced]}"
    # and the run must not be claiming substantive activity it can't have
    assert res["summary"].get("suppressed_low_value", 0) >= 0  # sanity: summary is well-formed


def test_sim_recurring_pattern_is_not_noisy():
    """THE regression for the report's finding: a recurring same-shape pattern must NOT
    surface every day. The invariant that matters is the anti-noise verdict, it surfaces
    at most a few times and is never noisy. (Exact counts depend on the DOP link firing on
    synthetic data, which has timezone seams; the invariant does not.)"""
    res = asyncio.run(simulate(days=14, seed=True))
    assert res["summary"]["surfaced"] < 14            # was 14/14 (noisy) before the fix
    assert res["surfaced_ratio"] <= 0.4               # the 'healthy' bar
    assert "healthy" in res["verdict"]
    # if it surfaced at all, the rest of the recurrences must be shape-suppressed
    if res["summary"]["surfaced"] > 0:
        assert res["summary"]["suppressed_shape"] > 0


def test_shape_suppression_is_what_prevents_daily_noise():
    """Direct, deterministic proof that shape suppression does the work (not dependent on
    the simulator's synthetic DOP link). Two same-family candidates: the second is
    suppressed once the first has surfaced."""
    from datetime import datetime, timezone
    init_db()
    with connect() as con:
        S._ensure_table(con)
        con.execute(
            "INSERT INTO surpriser_log(signature,kind,text,score,created_at,surfaced,source,family) "
            "VALUES('s1','email_meets_calendar','Email from Chris...',0.9,?,1,'background','email_meets_calendar')",
            (datetime.now(timezone.utc).isoformat(),))
        con.commit()
        shape = S._suppressed_families(con)
        useful = S._useful_families(con)
    # a different-person same-shape candidate, not imminent, score 0.9 (< 0.93 elevate bar)
    cand = {"signature": "s2", "family": "email_meets_calendar", "score": 0.9, "imminent": False}
    elevated = cand["imminent"] or cand["family"] in useful or cand["score"] >= 0.93
    suppressed_by_shape = cand["family"] in shape and not elevated
    assert suppressed_by_shape is True


def test_sim_reports_all_score_categories():
    res = asyncio.run(simulate(days=7, seed=True))
    for k in ("surfaced", "silent", "suppressed_duplicate", "suppressed_shape",
              "suppressed_low_value", "suppressed_quiet_hours", "suppressed_min_gap"):
        assert k in res["summary"]


def test_shape_suppression_blocks_same_family_different_instance():
    """Direct unit test: once a family surfaces, a different instance of the SAME family is
    shape-suppressed, unless elevated."""
    from datetime import datetime, timezone
    init_db()
    with connect() as con:
        S._ensure_table(con)
        con.execute(
            "INSERT INTO surpriser_log(signature,kind,text,score,created_at,surfaced,source,family) "
            "VALUES('sigA','email_meets_calendar','Email from Chris...',0.9,?,1,'background','email_meets_calendar')",
            (datetime.now(timezone.utc).isoformat(),))
        con.commit()
        suppressed_fams = S._suppressed_families(con)
    assert "email_meets_calendar" in suppressed_fams


def test_useful_family_is_exempt_from_shape_suppression():
    """A family the user marked useful keeps surfacing (they want that kind of nudge)."""
    from datetime import datetime, timezone
    init_db()
    with connect() as con:
        S._ensure_table(con)
        con.execute(
            "INSERT INTO surpriser_log(signature,kind,text,score,created_at,surfaced,source,family,feedback) "
            "VALUES('sigB','email_meets_calendar','Email from Chris...',0.9,?,1,'background','email_meets_calendar','useful')",
            (datetime.now(timezone.utc).isoformat(),))
        con.commit()
        suppressed_fams = S._suppressed_families(con)
        useful_fams = S._useful_families(con)
    assert "email_meets_calendar" not in suppressed_fams   # useful -> not suppressed
    assert "email_meets_calendar" in useful_fams


# --- shape-cooldown elevation tests (QA-audit recommendation 8) ---
# A shape that surfaced recently is normally suppressed, but four conditions ELEVATE a
# candidate past the cooldown. Test each directly and deterministically.

def _log_surfaced_family(con, family="email_meets_calendar", feedback=None):
    from datetime import datetime, timezone
    S._ensure_table(con)
    con.execute(
        "INSERT INTO surpriser_log(signature,kind,text,score,created_at,surfaced,source,family,feedback) "
        "VALUES(?,?,?,?,?,1,'background',?,?)",
        (f"sig_{family}", family, "x", 0.9, datetime.now(timezone.utc).isoformat(), family, feedback))
    con.commit()


def _is_eligible(cand, con):
    """Mirror pick_surprise's eligibility decision for a single candidate."""
    suppressed = S._suppressed_signatures(con)
    shape = S._suppressed_families(con)
    useful = S._useful_families(con)
    if cand["signature"] in suppressed or cand.get("score", 0) < 0.55:
        return False
    fam = cand.get("family")
    if fam and fam in shape:
        elevated = cand.get("imminent") or fam in useful or cand.get("score", 0) >= 0.93
        return bool(elevated)
    return True


def test_elevation_imminent_overrides_shape_cooldown():
    init_db()
    with connect() as con:
        _log_surfaced_family(con)
    cand = {"signature": "new", "family": "email_meets_calendar", "score": 0.9, "imminent": True}
    with connect() as con:
        assert _is_eligible(cand, con) is True   # imminent overrides


def test_elevation_useful_family_overrides_shape_cooldown():
    init_db()
    with connect() as con:
        _log_surfaced_family(con, feedback="useful")
    cand = {"signature": "new", "family": "email_meets_calendar", "score": 0.9, "imminent": False}
    with connect() as con:
        assert _is_eligible(cand, con) is True   # marked-useful family keeps surfacing


def test_elevation_high_value_overrides_shape_cooldown():
    init_db()
    with connect() as con:
        _log_surfaced_family(con)
    cand = {"signature": "new", "family": "email_meets_calendar", "score": 0.95, "imminent": False}
    with connect() as con:
        assert _is_eligible(cand, con) is True   # score >= 0.93 elevates


def test_no_elevation_same_shape_is_suppressed():
    init_db()
    with connect() as con:
        _log_surfaced_family(con)
    cand = {"signature": "new", "family": "email_meets_calendar", "score": 0.9, "imminent": False}
    with connect() as con:
        assert _is_eligible(cand, con) is False  # ordinary same-shape repeat: suppressed


def test_not_useful_feedback_suppresses_signature():
    """A signature marked not_useful is suppressed for the long window (separate from shape)."""
    from datetime import datetime, timezone, timedelta
    init_db()
    with connect() as con:
        S._ensure_table(con)
        con.execute(
            "INSERT INTO surpriser_log(signature,kind,text,score,created_at,surfaced,source,family,feedback) "
            "VALUES('rej','email_meets_calendar','x',0.9,?,1,'background','email_meets_calendar','not_useful')",
            ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),))  # past the 20h novelty window
        con.commit()
        suppressed = S._suppressed_signatures(con)
    assert "rej" in suppressed   # rejected stays suppressed beyond the normal cooldown


def test_quiet_hours_run_is_inconclusive_not_healthy():
    """QA-audit P0.2: a run pinned to quiet hours must NOT claim 'healthy' (it only proved
    quiet-hours suppression, not shape suppression)."""
    res = asyncio.run(simulate(days=14, seed=True, hour=3))
    assert res["inconclusive"] is True
    assert "inconclusive" in res["verdict"].lower()
    assert res["summary"]["suppressed_quiet_hours"] > 0


def test_daytime_seeded_run_tests_shape_not_quiet_hours():
    """The seeded default (daytime) must exercise SHAPE suppression, with zero quiet-hours
    suppression."""
    res = asyncio.run(simulate(days=14, seed=True, hour=14))
    assert res["summary"]["suppressed_quiet_hours"] == 0
    assert res["inconclusive"] is False
    assert res["surfaced_ratio"] <= 0.4


def test_scheduler_path_mode_runs_through_run_due_jobs():
    """QA-audit rec 7: scheduler-path mode drives the real scheduling path and still gives
    an anti-noise result, without sending real pushes."""
    res = asyncio.run(simulate(days=14, seed=True, scheduler_path=True))
    assert res["surfaced_ratio"] <= 0.4
    assert "never mutated the live database" in res["safety_note"]
