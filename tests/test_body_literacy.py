"""Tests for the body-literacy guided flows: body check-in, proprioception/grounding resets,
emotional-literacy journalling. The load-bearing properties are SAFETY: the flows facilitate
noticing and naming, they NEVER assert what the user feels, they always allow 'not now', and they
log what the user named (not an inferred diagnosis)."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


# --- check-in flow ----------------------------------------------------------

def test_checkin_is_multi_turn_and_saves_what_user_named():
    _fresh()
    from mongoose.services import body_literacy as BL, body_state as BS
    cid = "t"
    BL.start_checkin(cid)
    assert BL.flow_active(cid)
    BL.advance(cid, "jaw and right side")
    BL.advance(cid, "guarded and tight")
    BL.advance(cid, "angry, exposed, sad")
    BL.advance(cid, "a pause before replying")
    r = BL.advance(cid, "just leave it here")
    assert r["done"]
    assert not BL.flow_active(cid)
    entries = BS.list_entries("journal")
    assert len(entries) == 1
    e = entries[0]
    # it stored the USER's words, verbatim , not an inference
    assert e["body_signal"] == "jaw and right side"
    assert "angry" in e["emotion_words"]
    assert e["need"] == "a pause before replying"


def test_checkin_ends_with_offer_not_prescription():
    _fresh()
    from mongoose.services import body_literacy as BL
    cid = "t"
    BL.start_checkin(cid)
    BL.advance(cid, "chest")
    BL.advance(cid, "tight")
    BL.advance(cid, "anxious")
    r = BL.advance(cid, "to slow down")
    # the penultimate step OFFERS choices ("your pick"), never tells the user what to do
    speech = r["speech"].lower()
    assert "your pick" in speech or "or just leave it" in speech
    # never prescriptive / diagnostic language
    assert "you should" not in speech
    assert "you are" not in speech


def test_not_now_stops_any_time_and_keeps_partial():
    _fresh()
    from mongoose.services import body_literacy as BL, body_state as BS
    cid = "t"
    BL.start_checkin(cid)
    BL.advance(cid, "shoulders")
    r = BL.advance(cid, "not now")
    assert r["done"]
    assert not BL.flow_active(cid)
    # what was named before stopping is not lost
    assert len(BS.list_entries("journal")) == 1


def test_stop_words_work():
    _fresh()
    from mongoose.services import body_literacy as BL
    for stop in ("stop", "cancel", "later", "leave it"):
        cid = f"t-{stop}"
        BL.start_checkin(cid)
        r = BL.advance(cid, stop)
        assert r["done"], f"{stop!r} should end the flow"
        assert not BL.flow_active(cid)


# --- reset flow -------------------------------------------------------------

def test_reset_menu_then_practice_then_learns_effect():
    _fresh()
    from mongoose.services import body_literacy as BL, body_state as BS
    cid = "t"
    started = BL.start_reset(cid)   # no kind -> menu
    assert "60-second" in started["speech"] or "3-minute" in started["speech"]
    BL.advance(cid, "the 3 minute one")
    r = BL.advance(cid, "much more settled")
    assert r["done"]
    e = BS.list_entries("journal")[0]
    assert e["mode"] == "reset"
    assert e["effect"] == "better"   # it learned this one helped


def test_reset_records_when_it_did_not_help():
    _fresh()
    from mongoose.services import body_literacy as BL, body_state as BS
    cid = "t"
    BL.start_reset(cid, kind="3min")
    r = BL.advance(cid, "more braced actually")
    assert r["done"]
    e = BS.list_entries("journal")[0]
    assert e["effect"] == "worse"   # honest , doesn't pretend everything helps


def test_direct_reset_kind_skips_menu():
    _fresh()
    from mongoose.services import body_literacy as BL
    started = BL.start_reset("t", kind="primer")
    assert "primer" in started["speech"].lower()
    assert not started["done"]   # waiting for the after-rating


# --- journal flow -----------------------------------------------------------

def test_journal_difficult_walks_structured_prompts():
    _fresh()
    from mongoose.services import body_literacy as BL, body_state as BS
    cid = "t"
    BL.start_journal(cid, kind="difficult")
    BL.advance(cid, "the criticism")
    BL.advance(cid, "to defend myself")
    BL.advance(cid, "my competence")
    r = BL.advance(cid, "a clean boundary")
    assert r["done"]
    e = BS.list_entries("journal")[0]
    assert "difficult" in e["mode"]
    assert e["need"] == "a clean boundary"


def test_journal_freeform_captures_text():
    _fresh()
    from mongoose.services import body_literacy as BL, body_state as BS
    cid = "t"
    BL.start_journal(cid, kind="freeform")
    r = BL.advance(cid, "today felt heavy but I got through it")
    assert r["done"]
    e = BS.list_entries("journal")[0]
    assert "heavy" in e["text"]


# --- router -----------------------------------------------------------------

def test_flow_start_intents():
    from mongoose.role_router import looks_like_body_flow as bf, body_flow_kind as bk
    assert bf("start a body check-in") and bk("start a body check-in")["flow"] == "checkin"
    assert bf("start a proprioception reset") and bk("start a proprioception reset")["flow"] == "reset"
    assert bf("give me a pre-gym primer") and bk("give me a pre-gym primer")["kind"] == "primer"
    assert bf("help me name what I'm feeling") and bk("help me name what I'm feeling")["flow"] == "journal"


def test_flow_intents_dont_match_ordinary():
    from mongoose.role_router import looks_like_body_flow as bf
    assert not bf("turn off the lights")
    assert not bf("how is my body today")     # a readiness question, not a flow
    assert not bf("should I swim or lift")
