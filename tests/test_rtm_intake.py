"""Tests for the Example Court RTM intake + draft-response pipeline. The load-bearing properties
are SAFETY: deterministic safety classification is negation-aware; drafts are draft-only and
NOTHING auto-sends; the live mailbox and live send edges are DISABLED by default so the sandbox
(and an overnight run) never touch a real inbox or send a real email."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    # make sure live edges are OFF for tests (the safe default)
    os.environ.pop("RTM_LIVE_MAILBOX", None)
    os.environ.pop("RTM_LIVE_SEND", None)
    from mongoose.db import init_db
    init_db()


# --- the live edges are disabled by default ---------------------------------

def test_live_edges_off_by_default():
    _fresh()
    from mongoose.services import rtm_intake as R
    s = R.status()
    assert s["live_mailbox"] is False
    assert s["live_send"] is False


def test_poll_does_nothing_when_mailbox_disabled():
    _fresh()
    from mongoose.services import rtm_intake as R
    r = R.poll_once()
    assert r["ok"] is False and r["live"] is False
    assert "off" in r["note"].lower()


def test_approve_does_not_send_when_send_disabled():
    _fresh()
    from mongoose.services import rtm_intake as R
    res = R.intake_email(sender="A <a@x.com>", subject="Leak", body="leak in the communal hallway")
    d = R.generate_draft(res["intake_id"])
    ap = R.approve_draft(d["draft_id"])
    # approved, but explicitly NOT sent
    assert ap["sent"] is False
    assert ap["status"] == "approved"


# --- safety classification is deterministic + negation-aware ----------------

def test_real_safety_is_high_urgency():
    _fresh()
    from mongoose.services.rtm_intake import classify
    for body in ["I can smell gas in the hallway",
                 "there is a fire in the bin store",
                 "someone is stuck in the lift",
                 "water is pouring through the ceiling"]:
        c = classify("subject", body)
        assert c["urgency"] == "high", body
        assert c["safety"], body


def test_negation_does_not_trip_safety():
    _fresh()
    from mongoose.services.rtm_intake import classify
    # "no gas smell" / "resolved" must NOT be a high-urgency safety case
    c = classify("Update", "confirming there is no gas smell anymore, it was resolved")
    assert not c["safety"]
    assert c["urgency"] != "high"


def test_active_hazard_with_negated_resolution_still_trips():
    _fresh()
    from mongoose.services.rtm_intake import classify
    # an ACTIVE hazard described as "not yet fixed" must STILL trip , for safety a false negative
    # is worse than a false positive
    c = classify("x", "gas leak in the communal hall, not yet fixed")
    assert c["safety"]
    assert c["urgency"] == "high"


def test_responsibility_demise_vs_communal():
    _fresh()
    from mongoose.services.rtm_intake import classify
    assert classify("x", "leak inside my own flat kitchen")["responsibility"] == "demise"
    assert classify("x", "the communal hallway light is broken")["responsibility"] == "communal"


# --- drafting: draft-only, routine gets acknowledge -------------------------

def test_routine_gets_acknowledge_draft():
    _fresh()
    from mongoose.services import rtm_intake as R
    res = R.intake_email(sender="B <b@x.com>", subject="Thanks",
                         body="thank you, just confirming receipt for my records")
    assert res["classification"]["routine"] is True
    d = R.generate_draft(res["intake_id"])
    assert d["kind"] == "acknowledge"


def test_substantive_gets_reply_draft_with_review_marker():
    _fresh()
    from mongoose.services import rtm_intake as R
    res = R.intake_email(sender="C <c@x.com>", subject="Gas leak",
                         body="strong gas smell in the communal stairwell")
    d = R.generate_draft(res["intake_id"])
    assert d["kind"] == "reply"
    # safety drafts must carry a review-before-sending marker so nothing unreviewed goes out
    assert "DRAFT" in d["body"]


def test_draft_is_pending_not_sent():
    _fresh()
    from mongoose.services import rtm_intake as R
    res = R.intake_email(sender="D <d@x.com>", subject="Query", body="a general question")
    d = R.generate_draft(res["intake_id"])
    drafts = R.list_drafts("pending")
    assert any(x["id"] == d["draft_id"] for x in drafts)


# --- sender parsing (the hardening the audit asked for) ---------------------

def test_sender_parsed_exactly_not_substring():
    _fresh()
    from mongoose.services.rtm_intake import _parse_email
    assert _parse_email("Jane Smith <jane@example.com>") == "jane@example.com"
    # a spoofed display name doesn't change the real address
    assert _parse_email('"info@example-court.org" <attacker@evil.com>') == "attacker@evil.com"


# --- threading + cases ------------------------------------------------------

def test_replies_thread_into_one_case():
    _fresh()
    from mongoose.services import rtm_intake as R
    r1 = R.intake_email(sender="E <e@x.com>", subject="Leak", body="there is a leak")
    r2 = R.intake_email(sender="E <e@x.com>", subject="Re: Leak", body="still leaking")
    assert r1["case_id"] == r2["case_id"]   # same thread -> same case


def test_receipts_recorded():
    _fresh()
    from mongoose.services import rtm_intake as R
    res = R.intake_email(sender="F <f@x.com>", subject="X", body="communal door broken")
    d = R.generate_draft(res["intake_id"])
    R.approve_draft(d["draft_id"])
    rcpts = R.case_actions(res["case_id"])
    actions = [r["action"] for r in rcpts]
    assert "draft_created" in actions
    assert any("approved" in a for a in actions)


# --- merged from the v0.37 fork: voicemail import, FTS search, build plan, MCP ---

def test_voicemail_import_creates_case_and_draft():
    _fresh()
    import json as _json
    from mongoose.services import rtm_intake as R
    vm = tempfile.mkdtemp()
    meta = {"message_id": "vm1", "caller": "07700900123",
            "transcript_filename": "vm.txt", "received_time": "2026-06-20T10:00:00Z"}
    open(os.path.join(vm, "vm.json"), "w").write(_json.dumps(meta))
    open(os.path.join(vm, "vm.txt"), "w").write("the lift is stuck on the third floor")
    res = R.import_voicemail_dir(vm)
    assert res["ok"] is True
    assert res["imported"] == 1
    # idempotent: a second import skips it
    res2 = R.import_voicemail_dir(vm)
    assert res2["imported"] == 0 and res2["skipped"] == 1


def test_fts_search_finds_email_and_voicemail():
    _fresh()
    from mongoose.services import rtm_intake as R
    R.intake_email(sender="Jane <jane@x.com>", subject="Service charge query",
                   body="a question about my service charge")
    R.intake_email(sender="VoIP provider <vm@x.com>", subject="Voicemail",
                   body="the lift is stuck again", kind="voicemail")
    assert len(R.search_intake("service charge")) >= 1
    assert len(R.search_intake("lift")) >= 1


def test_voicemail_import_missing_dir_is_handled():
    _fresh()
    from mongoose.services import rtm_intake as R
    res = R.import_voicemail_dir("/nonexistent/path/xyz")
    assert res["ok"] is False and res["reason"] == "root_not_found"


def test_intake_mvp_build_plan_is_valid():
    _fresh()
    from mongoose.services.build_plans import build_example_court_intake_plan, EXAMPLE_COURT_INTAKE_MVP_STEPS
    plan = build_example_court_intake_plan()
    assert plan["name"] == "example-court-intake-mvp"
    assert len(EXAMPLE_COURT_INTAKE_MVP_STEPS) == 9


def test_rtm_mcp_tools_are_draft_only():
    _fresh()
    from mongoose.mcp_server import _tools
    t = _tools()
    assert "rtm_search" in t and "rtm_list" in t and "rtm_draft_email" in t
    # there is deliberately NO rtm send tool
    assert not any("send" in k for k in t if k.startswith("rtm_"))


# --- the optional model-drafting tier (privacy-first; off by default) --------

def _clear_tier_env():
    for k in ["RTM_DRAFT_MODEL_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "RTM_DRAFT_MODEL"]:
        os.environ.pop(k, None)


def test_model_tier_off_by_default():
    _fresh()
    _clear_tier_env()
    from mongoose.services import rtm_intake as R
    assert R._model_tier_enabled() is False
    # status makes the privacy posture explicit
    assert "no case content leaves" in R.status()["draft_tier"].lower()


def test_substantive_case_uses_template_without_key():
    _fresh()
    _clear_tier_env()
    from mongoose.services import rtm_intake as R
    res = R.intake_email(sender="Jane <jane@x.com>", subject="Service charge breakdown",
                         body="please explain the service charge increase")
    d = R.generate_draft(res["intake_id"])
    assert d["source"] == "template"   # no key -> local template, nothing external


def test_safety_always_template_even_with_key():
    _fresh()
    os.environ["RTM_DRAFT_MODEL_PROVIDER"] = "anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "sk-fake-not-used"
    try:
        from mongoose.services import rtm_intake as R
        # safety case must NEVER route to the model , a human-checkable skeleton is safer
        cls = {"safety": ["gas"], "urgency": "high", "routine": False}
        assert R._should_use_model_tier(cls) is False
        # high urgency too
        assert R._should_use_model_tier({"safety": [], "urgency": "high", "routine": False}) is False
        # routine too
        assert R._should_use_model_tier({"safety": [], "urgency": "low", "routine": True}) is False
        # but a substantive non-urgent case WOULD route to the model when configured
        assert R._should_use_model_tier({"safety": [], "urgency": "low", "routine": False}) is True
    finally:
        _clear_tier_env()


def test_model_failure_falls_back_to_template():
    _fresh()
    # configure the tier but point it at a key that will fail the real call; generate_draft must
    # fall back to the template, never crash, never leave a case without a draft
    os.environ["RTM_DRAFT_MODEL_PROVIDER"] = "anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "sk-definitely-invalid"
    try:
        from mongoose.services import rtm_intake as R
        import unittest.mock as mock
        # force the model call to return None (as a failure would)
        with mock.patch.object(R, "_draft_body_via_model", return_value=None):
            res = R.intake_email(sender="A <a@x.com>", subject="Billing query",
                                 body="a question about my account")
            d = R.generate_draft(res["intake_id"])
            assert d["ok"] is True
            assert d["source"] == "template"   # fell back cleanly
    finally:
        _clear_tier_env()


def test_model_tier_gating_verifier_passes():
    _fresh()
    from mongoose.release_check import _verify_rtm_model_tier_gated
    assert _verify_rtm_model_tier_gated()["ok"] is True


def test_case_action_log_renamed_and_no_receipt_misnomer():
    """Phase 4 (RTM_DATA_MODEL.md): the intake action log is rtm_case_actions, NOT rtm_receipts. A
    payment receipt is rtm_trust_receipts; this table must never re-acquire the ambiguous name."""
    import inspect
    from mongoose.services import rtm_intake as RI
    src = inspect.getsource(RI)
    assert "rtm_case_actions" in src and "rtm_receipts" not in src, "the misnamed rtm_receipts table came back"
    assert hasattr(RI, "case_actions") and not hasattr(RI, "case_receipts")
    # and the log actually records + reads back
    _fresh()
    res = RI.intake_email(sender="Ann <ann@example.com>", subject="Lift is stuck",
                          body="The lift stopped between floors.")
    RI.generate_draft(res["intake_id"])  # logs a 'draft_created' case action
    acts = RI.case_actions(res["case_id"])
    assert isinstance(acts, list) and any(a.get("action") == "draft_created" for a in acts)
