"""Tests for the benchmark recommender , the layer that turns benchmark results into an
actual 'use this model / this combo' decision, accounting for the M4's one-model-at-a-time
swap cost."""
from mongoose.benchmark_recommend import recommend, format_recommendation

KINDS = {"home_control": "tool", "find_intent": "tool",
         "ha_automation_review": "chat", "draft_correspondence": "chat"}


def _model(tag, results, cold):
    """Build a per-model benchmark result dict from {task: ok} and a latency."""
    tasks = [{"task": t, "ok": ok, "latency_median_s": 1.0} for t, ok in results.items()]
    return {"model": tag, "cold_load_s": cold, "task_latency_median_s": 1.0, "tasks": tasks}


def test_single_model_wins_all_roles():
    results = [
        _model("big", {"home_control": True, "find_intent": True,
                       "ha_automation_review": True, "draft_correspondence": True}, 6.0),
        _model("tiny", {"home_control": False, "find_intent": True,
                        "ha_automation_review": False, "draft_correspondence": False}, 2.0),
    ]
    rec = recommend(results, KINDS, cold_load_by_model={"big": 6.0, "tiny": 2.0})
    assert rec["ok"]
    assert rec["verdict"] == "single"
    assert set(rec["combo"].values()) == {"big"}
    assert rec["role_winners"]["chat"]["model"] == "big"
    assert rec["role_winners"]["tool"]["model"] == "big"


def test_combo_recommended_when_gain_big_and_swap_cheap():
    results = [
        _model("chatster", {"home_control": False, "find_intent": False,
                            "ha_automation_review": True, "draft_correspondence": True}, 3.0),
        _model("toolster", {"home_control": True, "find_intent": True,
                            "ha_automation_review": False, "draft_correspondence": False}, 2.5),
    ]
    rec = recommend(results, KINDS, cold_load_by_model={"chatster": 3.0, "toolster": 2.5})
    assert rec["verdict"] == "combo"
    assert rec["combo"]["chat"] == "chatster"
    assert rec["combo"]["tool"] == "toolster"
    assert rec["quality_gain"] > 0


def test_single_preferred_when_swap_too_expensive():
    """Same role split, but the per-switch reload is punishing on this machine , a
    responsive single model is the better real-world choice."""
    results = [
        _model("chatster", {"home_control": False, "find_intent": False,
                            "ha_automation_review": True, "draft_correspondence": True}, 12.0),
        _model("toolster", {"home_control": True, "find_intent": True,
                            "ha_automation_review": False, "draft_correspondence": False}, 11.0),
    ]
    rec = recommend(results, KINDS, cold_load_by_model={"chatster": 12.0, "toolster": 11.0})
    assert rec["verdict"] == "single"          # swap too costly despite the quality gain
    assert rec["swap_penalty_s"] >= 8.0
    assert "reload" in rec["rationale"].lower() or "swap" in rec["rationale"].lower()


def test_quality_ties_break_on_latency():
    """When two models pass the same tasks for a role, the faster one wins that role."""
    fast = _model("fast", {"home_control": True, "find_intent": True,
                           "ha_automation_review": True, "draft_correspondence": True}, 2.0)
    slow = _model("slow", {"home_control": True, "find_intent": True,
                           "ha_automation_review": True, "draft_correspondence": True}, 2.0)
    # make 'fast' genuinely faster on the tool tasks
    for t in fast["tasks"]:
        t["latency_median_s"] = 0.5
    for t in slow["tasks"]:
        t["latency_median_s"] = 3.0
    rec = recommend([fast, slow], KINDS, cold_load_by_model={"fast": 2.0, "slow": 2.0})
    assert rec["role_winners"]["tool"]["model"] == "fast"
    assert rec["role_winners"]["chat"]["model"] == "fast"


def test_empty_results_is_handled():
    rec = recommend([], KINDS)
    assert rec["ok"] is False


def test_format_includes_apply_settings():
    results = [
        _model("big", {"home_control": True, "find_intent": True,
                       "ha_automation_review": True, "draft_correspondence": True}, 6.0),
    ]
    rec = recommend(results, KINDS, cold_load_by_model={"big": 6.0})
    text = format_recommendation(rec)
    assert "MONGOOSE_CHAT_MODEL" in text       # the copy-paste settings must be shown
    assert "Recommendation:" in text
