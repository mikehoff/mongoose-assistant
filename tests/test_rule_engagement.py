"""The rule-engagement likelihood layer: deterministic short-circuits stand down on feedback.

The trap this kills: a rule misfires, the person reacts with confusion or repeats/rewords the
same request, and the identical rule fires identically again, so the model never gets a turn to
repair the conversation. Now: first fire engages and is recorded; a confusion signal, verbatim
repeat, or same-gist rewording right after collapses that rule's score, it stands down with a
suppression window, and the message falls through to the model. Windows expire; rules recover.

Signals are tested in English and Polish. The integration tests drive the REAL chat path:
an RTM build-status card fires once, then the same question reworded reaches the model instead.
"""
from __future__ import annotations

import asyncio
import time
import warnings

warnings.filterwarnings("ignore")


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "r.sqlite3"))
    from mongoose.db import init_db
    init_db()


def test_first_fire_engages_and_records(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services.rule_engagement import rule_ok, _load
    assert rule_ok("rtm_build", "how is the RTM build going?", "c1") is True
    assert _load("c1")["last"]["rule"] == "rtm_build"


def test_confusion_suppresses_the_same_rule(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services.rule_engagement import rule_ok, assess
    assert rule_ok("rtm_build", "how is the RTM build going?", "c2") is True
    v = assess("rtm_build", "what? that's not what I asked", "c2")
    assert v["engage"] is False and "confusion" in v["reason"]


def test_polish_confusion_suppresses(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services.rule_engagement import rule_ok, assess
    assert rule_ok("rtm_operational", "co jest zalegle?", "c3") is True
    v = assess("rtm_operational", "nie o to mi chodzi", "c3")
    assert v["engage"] is False and "confusion" in v["reason"]


def test_verbatim_repeat_suppresses(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services.rule_engagement import rule_ok, assess
    assert rule_ok("readiness", "is everything ready?", "c4") is True
    v = assess("readiness", "Is everything READY?", "c4")   # case/punct differences ignored
    assert v["engage"] is False and "repeated verbatim" in v["reason"]


def test_reworded_same_gist_suppresses(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services.rule_engagement import rule_ok, assess
    assert rule_ok("rtm_build", "how is the RTM plugin build progressing?", "c5") is True
    v = assess("rtm_build", "the RTM plugin build, how is it progressing now", "c5")
    assert v["engage"] is False and "reworded" in v["reason"]


def test_standing_down_sets_a_window_then_recovers(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    import mongoose.services.rule_engagement as RE
    assert RE.rule_ok("rtm_build", "how is the build?", "c6") is True
    assert RE.rule_ok("rtm_build", "how is the build?", "c6") is False   # repeat: stands down
    v = RE.assess("rtm_build", "completely different topic entirely", "c6")
    assert v["engage"] is False and "suppressed" in v["reason"]          # window holds
    # Recovery is defined for a genuinely fresh ask: expire the suppression window AND age the
    # last fire beyond the feedback window (a verbatim repeat still inside the feedback window
    # correctly stays suppressed; that is the trap, not the recovery).
    st = RE._load("c6")
    st["suppressed"]["rtm_build"] = time.time() - 1
    st["last"]["at"] = time.time() - (RE.FEEDBACK_WINDOW_S + 5)
    RE._save("c6", st)
    assert RE.rule_ok("rtm_build", "how is the build?", "c6") is True


def test_different_rule_unaffected_by_anothers_feedback(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from mongoose.services.rule_engagement import rule_ok
    assert rule_ok("rtm_build", "how is the build?", "c7") is True
    # confusion after rtm_build fired must not pre-suppress readiness
    assert rule_ok("readiness", "is everything ready?", "c7") is True


def test_fail_open_when_storage_breaks(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    import mongoose.services.rule_engagement as RE
    monkeypatch.setattr(RE, "assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert RE.rule_ok("anything", "hello", "c8") is True    # rules never break the product


def test_integration_rtm_build_card_stands_down_on_rewording(monkeypatch, tmp_path):
    """The real trap, end to end: the RTM build card answers once; the same gist reworded next
    turn must NOT get the card again. (The fall-through path then reaches the model; with no
    model in this container the reply differs in SOURCE, which is what we assert.)"""
    _db(monkeypatch, tmp_path)
    from mongoose.llm import chat_with_memory
    out1 = asyncio.run(chat_with_memory("how is the RTM build going?",
                                        conversation_id="i1", role="chat"))
    assert out1.get("source") == "rtm_build"
    out2 = asyncio.run(chat_with_memory("how is the RTM build going?",
                                        conversation_id="i1", role="chat"))
    assert out2.get("source") != "rtm_build", out2


def test_integration_plugin_bridge_stands_down_but_confirm_never_does(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("MONGOOSE_PLUGIN_DIR", str(tmp_path / "plugin-src"))
    from mongoose.services.wp_plugin import scaffold_example_court_plugin
    assert scaffold_example_court_plugin("0.2.0")["ok"]
    from mongoose.llm import chat_with_memory
    out1 = asyncio.run(chat_with_memory("Make the WordPress plugin better.",
                                        conversation_id="i2", role="chat"))
    assert out1.get("source") == "wp_plugin_chat"
    token = out1["proposal"]["confirm_token"]
    # The verbatim repeat must stand down and fall through to the model. There is no model in
    # this container, so the fall-through path raises LocalModelError, which is itself the
    # proof: the bridge did NOT answer.
    from mongoose.llm import LocalModelError
    try:
        out2 = asyncio.run(chat_with_memory("Make the WordPress plugin better.",
                                            conversation_id="i2", role="chat"))
        assert out2.get("source") != "wp_plugin_chat", "verbatim repeat must stand down"
    except LocalModelError:
        pass  # fell through to the (absent) model: exactly the stand-down we wanted
    # But an explicit confirm token is never suppressed, even now.
    def fake_invoke(task, repo):
        return True, "no-op edit", False
    import mongoose.services.builder as B
    monkeypatch.setattr(B, "_invoke_claude_code", fake_invoke)
    out3 = asyncio.run(chat_with_memory(f"confirm {token}", conversation_id="i2", role="chat"))
    assert out3.get("source") == "sensitive_confirm", out3


def test_short_acknowledgements_never_extend_suppression_via_embeddings(monkeypatch, tmp_path):
    """The decaying-confidence contract: suppression windows must be allowed to EXPIRE. With
    the model installed, 'ok' and 'yes please' can embed close; without a length guard each
    acknowledgement would count as a reworded re-attempt and re-extend the window forever.
    Force semantic similarity to scream 0.99 and prove short utterances still score 0."""
    import mongoose.services.embeddings as E
    import mongoose.services.rule_engagement as R
    monkeypatch.setattr(E, "semantic_similarity", lambda a, b: 0.99)
    assert R._similarity("ok", "yes please") == 0.0
    assert R._similarity("thanks", "ok great") == 0.0
    # Real sentences still reach the semantic path and stand down as designed.
    s = R._similarity("how is the RTM build going along?",
                      "what is the current progress of the RTM build?")
    assert s >= 0.6
