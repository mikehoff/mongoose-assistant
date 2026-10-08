"""Tests for the RTM capability/candidate model, sync checkpoints, and Example Court real data."""
import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    for k in list(os.environ):
        if k.startswith("MONGOOSE_SYNC_"):
            os.environ.pop(k, None)
    from mongoose.db import init_db
    init_db()


# --- capability + build-candidate state --------------------------------------

def test_capability_state_derived_from_data():
    _fresh()
    from mongoose.services import rtm_management as M, rtm_capability as C
    cap0 = C.capability_state()
    assert cap0["ok"] and cap0["in_use"] == 0   # nothing yet
    M.seed_flats()
    M.create_demand(document_number="D-1", flat="1ECF1", period="2026", amount=900, issue_date="2026-07-16")
    cap1 = C.capability_state()
    assert cap1["in_use"] >= 1   # R1/R3/R4 now in use
    reqs = {r["requirement"]: r for r in cap1["requirements"]}
    assert reqs["R1"]["state"] in ("in_use", "verified")


def test_candidates_create_promote_prune():
    _fresh()
    from mongoose.services import rtm_capability as C
    a = C.create_candidate("wp-admin", approach="PHP plugin")
    b = C.create_candidate("wp-admin", approach="React SPA")
    assert len(C.list_candidates("wp-admin")) == 2
    r = C.promote_candidate(a["id"])
    assert r["chosen"] == a["id"] and b["id"] in r["superseded"]
    # the superseded one is kept until pruned
    assert any(c["status"] == "superseded" for c in C.list_candidates("wp-admin"))
    p = C.prune_candidates()
    assert b["id"] in p["pruned"]
    # the chosen one survives pruning
    assert any(c["id"] == a["id"] for c in C.list_candidates("wp-admin"))


def test_build_lock_prevents_collision():
    _fresh()
    from mongoose.services import rtm_capability as C
    l1 = C.acquire_build_lock("adhoc:test")
    assert l1["ok"]
    l2 = C.acquire_build_lock("scheduled:daily")
    assert not l2["ok"] and l2["held_by"] == "adhoc:test"
    rel = C.release_build_lock(l1["token"])
    assert rel["ok"]
    l3 = C.acquire_build_lock("scheduled:daily")
    assert l3["ok"]


def test_build_lock_release_requires_token():
    _fresh()
    from mongoose.services import rtm_capability as C
    l1 = C.acquire_build_lock("adhoc:test")
    bad = C.release_build_lock("wrong-token")
    assert not bad["ok"]
    # the real token still works
    assert C.release_build_lock(l1["token"])["ok"]


# --- sync checkpoints --------------------------------------------------------

def test_sync_channels_enabled_day_one():
    _fresh()
    from mongoose.services import sync_checkpoint as SC
    ch = SC.channel_status()
    # all four channels remain AVAILABLE by default (day-one override): the capability is ready.
    for name in ("chat_pro", "claude_pro", "anthropic_api", "openai_api"):
        assert ch[name]["enabled"] is True
    # but AVAILABILITY is not CONSENT: outbound delivery is off until explicitly opted in, so no
    # channel is 'deliverable' on a fresh install (claim-integrity: 'nothing leaves the machine').
    assert ch["_delivery_enabled"] is False
    assert ch["chat_pro"]["deliverable"] is False
    assert ch["anthropic_api"]["deliverable"] is False


def test_sync_channels_deliverable_when_opted_in():
    _fresh()
    os.environ["MONGOOSE_SYNC_DELIVERY"] = "on"
    try:
        import mongoose.settings as st
        st._SETTINGS = None
        from mongoose.services import sync_checkpoint as SC
        ch = SC.channel_status()
        assert ch["_delivery_enabled"] is True
        # human channels deliver once opted in; API channels still need their key
        assert ch["chat_pro"]["deliverable"] is True
        assert ch["anthropic_api"]["deliverable"] is False  # no key in test
    finally:
        os.environ.pop("MONGOOSE_SYNC_DELIVERY", None)
        import mongoose.settings as st
        st._SETTINGS = None


def test_sync_channel_can_be_disabled():
    _fresh()
    os.environ["MONGOOSE_SYNC_OPENAI_API"] = "off"
    try:
        from mongoose.services import sync_checkpoint as SC
        assert SC.channel_status()["openai_api"]["enabled"] is False
    finally:
        os.environ.pop("MONGOOSE_SYNC_OPENAI_API", None)


def test_checkpoint_assembles_done_surfaced_asking():
    _fresh()
    from mongoose.services import rtm_management as M, sync_checkpoint as SC
    M.seed_flats()
    M.create_demand(document_number="OD-1", flat="1ECF1", period="2026", amount=500, issue_date="2026-01-01")
    M.refresh_all_demand_statuses()
    cp = SC.assemble_checkpoint()
    assert "done" in cp and "surfaced" in cp and "asking" in cp
    assert cp["asking"]   # at least one question
    # arrears should be surfaced
    assert any("overdue" in s["detail"].lower() or "arrears" in s["detail"].lower()
               for s in cp["surfaced"])


def test_checkpoint_recorded_to_history():
    _fresh()
    from mongoose.services import sync_checkpoint as SC
    r = asyncio.run(SC.run_checkpoint())
    assert r["checkpoint"]["id"]
    hist = SC.checkpoint_history()
    assert len(hist) == 1


# --- Example Court real data -------------------------------------------------

def test_real_data_fixture_validates():
    _fresh()
    from mongoose.services import rtm_example_court as PC
    v = PC.validate_against_management()
    assert v["ok"]
    assert v["computed_total"] == "\u00a317,685.24"
    assert v["rounding_delta"] == "\u00a30.24"
    assert v["flats"] == 18


def test_apply_real_data_loads_flats_and_budget():
    _fresh()
    from mongoose.services import rtm_example_court as PC, rtm_management as M
    r = PC.apply_real_data(with_demands=False)
    assert r["ok"] and r["flats_loaded"] == 18 and r["budget_lines_loaded"] == 24
    flats = M.list_flats()
    assert len(flats) == 18
    # real payment references present
    refs = {f.get("payment_reference") for f in flats}
    assert "MORGAN" in refs and "CRANE" in refs


def test_apply_real_demands_uses_14_day_due_and_no_money_bug():
    _fresh()
    from mongoose.services import rtm_example_court as PC, rtm_management as M
    PC.apply_real_data(with_demands=True)
    demands = M.list_demands(limit=50)
    assert len(demands) == 18
    # find Morgan's demand: £1,080.21, due 2026-07-30 (issue + 14, not + 28)
    hoff = next(d for d in demands if d["flat_id"] == "1ECF1")
    assert hoff["amount"] == "\u00a31,080.21"      # exact money, no bug
    assert hoff["due_date"] == "2026-07-30"      # the real 14-day policy preserved


def test_real_data_caveats_present():
    _fresh()
    from mongoose.services import rtm_example_court as PC
    codes = {c["code"] for c in PC.caveats()}
    assert "ROUNDING_DELTA" in codes
    assert "DUE_DATE_POLICY" in codes
    assert "RECONCILIATION_WORDING_PRESENT" in codes
    assert "DEED_VERIFICATION" in codes


def test_rounding_delta_not_treated_as_arrears():
    _fresh()
    from mongoose.services import rtm_example_court as PC, rtm_management as M
    PC.apply_real_data(with_demands=True)
    # total raised is exactly the demand-pack total, not the budget , the 24p is in the demands
    # by design, flagged as a reconciliation item, never injected as a separate arrears entry
    pos = M.fund_position()
    assert pos["demands_raised"] == "\u00a317,685.24"


# --- brain routing for the new knowledge/sync intents ------------------------

def test_brain_routes_rounding_and_sync():
    _fresh()
    from mongoose.services import rtm_example_court as PC
    PC.apply_real_data(with_demands=True)
    from mongoose.llm import chat_with_memory
    r1 = asyncio.run(chat_with_memory("what is the rounding situation at example court?", conversation_id="a"))
    assert r1["source"] == "rtm_knowledge" and "0.24" in r1["reply"]
    r2 = asyncio.run(chat_with_memory("run a sync checkpoint", conversation_id="b"))
    assert r2["source"] == "sync_checkpoint"


def test_knowledge_intent_does_not_hijack_general_law():
    _fresh()
    from mongoose.role_router import looks_like_rtm_knowledge
    assert not looks_like_rtm_knowledge("tell me about deeds of trust in general law")
    assert not looks_like_rtm_knowledge("what is the weather")
