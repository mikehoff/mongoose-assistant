"""Tests for the delight layer: the warm, interest-aware companion register."""
import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def test_fun_fact_is_interest_aware_and_shipped():
    _fresh()
    from mongoose.services import delight as D
    f = D.fun_fact(allow_model=False)
    assert f["text"] and f["source"] == "shipped"
    assert f["interest"] in (list(D._INTERESTS.keys()) + ["general"])


def test_joke_returns_something():
    _fresh()
    from mongoose.services import delight as D
    j = D.joke(allow_model=False)
    assert j["text"] and j["source"] in ("shipped", "model")


def test_interest_targeting():
    _fresh()
    from mongoose.services import delight as D
    f = D.fun_fact(interest="local_history", allow_model=False)
    assert f["interest"] == "local_history"
    j = D.joke(interest="dog", allow_model=False)
    assert j["interest"] == "dog"


def test_no_immediate_repeat():
    _fresh()
    from mongoose.services import delight as D
    # pull many; within the window we shouldn't see the exact same text twice in a row often.
    seen = []
    for _ in range(6):
        seen.append(D.delight(allow_model=False)["text"])
    # at least a few distinct results across 6 pulls (de-dupe working)
    assert len(set(seen)) >= 3


def test_varies_across_interests():
    _fresh()
    from mongoose.services import delight as D
    interests = {D.delight(allow_model=False)["interest"] for _ in range(12)}
    # should not be stuck on a single interest
    assert len(interests) >= 3


def test_model_fact_is_labelled():
    _fresh()
    from mongoose.services import delight as D
    # force a fake model nugget to verify labelling/caveat
    import mongoose.services.delight as DD
    orig = DD._model_nugget
    DD._model_nugget = lambda kind, interest: "A totally made up but plausible fact." if kind == "fact" else None
    try:
        f = D.fun_fact(interest="cooking", allow_model=True)
        assert f["source"] == "model" and "caveat" in f
    finally:
        DD._model_nugget = orig


# --- brain routing -----------------------------------------------------------

def test_brain_routes_joke_to_delight_not_tutorial():
    _fresh()
    from mongoose.llm import chat_with_memory
    r = asyncio.run(chat_with_memory("tell me a joke", conversation_id="j"))
    assert r["source"] == "delight"


def test_brain_routes_fun_fact():
    _fresh()
    from mongoose.llm import chat_with_memory
    r = asyncio.run(chat_with_memory("give me a fun fact", conversation_id="f"))
    assert r["source"] == "delight"


def test_brain_routes_cheer_me_up():
    _fresh()
    from mongoose.llm import chat_with_memory
    r = asyncio.run(chat_with_memory("cheer me up", conversation_id="c"))
    assert r["source"] == "delight"


def test_delight_does_not_hijack_real_questions():
    _fresh()
    from mongoose.role_router import looks_like_delight
    assert not looks_like_delight("what is the capital of france")
    assert not looks_like_delight("what's overdue at example court")
    assert not looks_like_delight("how loaded am i today")


def test_tutorial_still_works_for_capability_questions():
    _fresh()
    from mongoose.role_router import looks_like_tutorial, looks_like_delight
    # "what can you do" is tutorial, not delight
    assert looks_like_tutorial("what can you do")
    assert not looks_like_delight("what can you do")


# --- proactive surpriser integration -----------------------------------------

def test_surpriser_silent_by_default_no_delight(monkeypatch):
    _fresh()
    # Proactive delight now defaults ON (so Mongoose feels alive). With it explicitly OFF, no delight
    # candidate is produced — the original silence contract still holds when opted out.
    monkeypatch.setenv("MONGOOSE_PROACTIVE_DELIGHT", "false")
    import mongoose.settings as st
    if hasattr(st, "_settings"): st._settings = None
    if hasattr(st, "_SETTINGS"): st._SETTINGS = None
    if hasattr(st.get_settings, "cache_clear"): st.get_settings.cache_clear()
    from mongoose.services.surpriser import candidate_suggestions
    cands = asyncio.run(candidate_suggestions())
    assert not [c for c in cands if c.get("kind") == "delight"]  # off when opted out


def test_surpriser_offers_delight_on_quiet_cycle():
    _fresh()
    os.environ["MONGOOSE_PROACTIVE_DELIGHT"] = "1"
    import mongoose.settings as st; st._SETTINGS = None
    try:
        from mongoose.services.surpriser import candidate_suggestions
        cands = asyncio.run(candidate_suggestions())
        # opted-in + bare install (nothing urgent) -> a low-scored delight may be offered
        delights = [c for c in cands if c.get("kind") == "delight"]
        assert len(delights) >= 1
        assert delights[0]["score"] < 30  # always lower priority than real findings
    finally:
        os.environ.pop("MONGOOSE_PROACTIVE_DELIGHT", None)
        st._SETTINGS = None
