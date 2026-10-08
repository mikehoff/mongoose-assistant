"""Tests for Lane 2 , the model-tuning lane that vests over time. The load-bearing property
is HONESTY: it must never claim to be vested on thin data, the override must be reversible,
and auto-apply must only fire on a large, safe win over the model actually in use."""
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()
    # ensure no benchmark results leak in from a prior test sharing /tmp
    from mongoose.benchmark import _results_path
    p = _results_path()
    if p.exists():
        p.unlink()


def _add_usage(turns: int, days: int):
    from mongoose.db import connect
    from mongoose.services.memory import ensure_conversation
    cid = ensure_conversation()
    per_day = max(1, turns // max(1, days))
    with connect() as con:
        for d in range(days):
            day = (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
            for _ in range(per_day):
                con.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,'user','hi',?)",
                            (cid, day))
        con.commit()


def _seed_benchmark(models_quality: dict, rounds: int = 3):
    from mongoose.benchmark import _results_path
    p = _results_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for model, ok in models_quality.items():
        for rnd in range(rounds):
            rows.append({"model": model, "round": rnd, "result": {
                "model": model, "cold_load_s": 3.0, "task_latency_median_s": 1.0,
                "warm_chat_median_s": 1.0, "warm_chat": {"median": 1.0, "iqr": 0.1, "n": 6},
                "rounds": 1,
                "tasks": [{"task": "home_control", "ok": ok, "latency_median_s": 1.0},
                          {"task": "ha_automation_review", "ok": ok, "latency_median_s": 1.0}]}})
    p.write_text("\n".join(json.dumps(r) for r in rows))


# --- the honesty gate -------------------------------------------------------

def test_fresh_install_not_vested():
    _fresh()
    from mongoose.model_tuning import vesting_status
    v = vesting_status()
    assert v["vested"] is False
    assert v["stage"] == "accruing"


def test_usage_without_benchmarks_is_benchmarking_not_vested():
    _fresh()
    _add_usage(turns=45, days=3)
    from mongoose.model_tuning import vesting_status
    v = vesting_status()
    assert v["stage"] == "benchmarking"   # triggered, but no benchmark coverage yet
    assert v["vested"] is False


def test_usage_plus_coverage_vests():
    _fresh()
    _add_usage(turns=45, days=3)
    _seed_benchmark({"good": True, "bad": False}, rounds=3)
    from mongoose.model_tuning import vesting_status
    assert vesting_status()["stage"] == "vested"


def test_insufficient_rounds_does_not_vest():
    _fresh()
    _add_usage(turns=45, days=3)
    _seed_benchmark({"good": True, "bad": False}, rounds=1)   # below MIN_ROUNDS_PER_MODEL
    from mongoose.model_tuning import vesting_status
    assert vesting_status()["vested"] is False


def test_one_model_does_not_vest():
    _fresh()
    _add_usage(turns=45, days=3)
    _seed_benchmark({"only": True}, rounds=5)   # only one model = no real choice
    from mongoose.model_tuning import vesting_status
    assert vesting_status()["vested"] is False


# --- reversible override layer ----------------------------------------------

def test_override_applies_and_reverts():
    _fresh()
    from mongoose.settings import get_settings
    from mongoose.model_tuning import set_model_overrides, clear_model_overrides
    base = get_settings().model_for_role("chat")
    set_model_overrides({"chat": "switched-model:test"})
    assert get_settings().model_for_role("chat") == "switched-model:test"
    clear_model_overrides()
    assert get_settings().model_for_role("chat") == base


# --- auto-apply guardrails --------------------------------------------------

def test_auto_apply_fires_on_large_safe_win_over_incumbent():
    os.environ["OLLAMA_CHAT_MODEL"] = "bad"
    os.environ["OLLAMA_TOOL_MODEL"] = "bad"
    _fresh()
    _add_usage(turns=45, days=3)
    _seed_benchmark({"good": True, "bad": False}, rounds=3)
    from mongoose import model_tuning as MT
    from mongoose.settings import get_settings
    ev = MT.evaluate(allow_auto_apply=True)
    assert ev["auto_applied"] is True
    assert get_settings().model_for_role("chat") == "good"
    MT.revert()
    assert get_settings().model_for_role("chat") == "bad"
    del os.environ["OLLAMA_CHAT_MODEL"]
    del os.environ["OLLAMA_TOOL_MODEL"]


def test_no_auto_apply_when_incumbent_already_best():
    os.environ["OLLAMA_CHAT_MODEL"] = "good"
    os.environ["OLLAMA_TOOL_MODEL"] = "good"
    _fresh()
    _add_usage(turns=45, days=3)
    _seed_benchmark({"good": True, "bad": False}, rounds=3)
    from mongoose import model_tuning as MT
    ev = MT.evaluate(allow_auto_apply=True)
    assert ev["auto_applied"] is False
    assert ev["actionable"] is False   # already on the best, nothing to do
    del os.environ["OLLAMA_CHAT_MODEL"]
    del os.environ["OLLAMA_TOOL_MODEL"]


def test_dismiss_sticks_until_recommendation_changes():
    _fresh()
    _add_usage(turns=45, days=3)
    _seed_benchmark({"good": True, "bad": False}, rounds=3)
    from mongoose import model_tuning as MT
    MT.dismiss()
    ev = MT.evaluate(allow_auto_apply=False)
    assert ev["dismissed"] is True


def test_revert_clears_override_and_receipt():
    _fresh()
    from mongoose import model_tuning as MT
    MT.set_model_overrides({"chat": "x:test"})
    r = MT.revert()
    assert r["ok"] is True
    assert MT.get_model_overrides() == {}


def test_no_auto_apply_on_latency_regression():
    """The guard the docstring always promised: a big quality win that is meaningfully slower
    per turn is recommend-only, never auto-applied. (Wired in via the dead-constant gate:
    AUTO_APPLY_MAX_LATENCY_REGRESSION_S existed, unreferenced, while auto-apply checked only
    quality gain.)"""
    import json as _json
    os.environ["OLLAMA_CHAT_MODEL"] = "bad"
    os.environ["OLLAMA_TOOL_MODEL"] = "bad"
    _fresh()
    _add_usage(turns=45, days=3)
    from mongoose.benchmark import _results_path
    p = _results_path(); p.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for model, ok, warm in (("good", True, 4.5), ("bad", False, 1.0)):  # good is 3.5s/turn slower
        for rnd in range(3):
            rows.append({"model": model, "round": rnd, "result": {
                "model": model, "cold_load_s": 3.0, "task_latency_median_s": 1.0,
                "warm_chat_median_s": warm, "warm_chat": {"median": warm, "iqr": 0.1, "n": 6},
                "rounds": 1,
                "tasks": [{"task": "home_control", "ok": ok, "latency_median_s": 1.0},
                          {"task": "ha_automation_review", "ok": ok, "latency_median_s": 1.0}]}})
    p.write_text("\n".join(_json.dumps(r) for r in rows))
    from mongoose import model_tuning as MT
    from mongoose.settings import get_settings
    ev = MT.evaluate(allow_auto_apply=True)
    assert ev.get("auto_applied") is not True
    assert get_settings().model_for_role("chat") == "bad"  # incumbent untouched
    del os.environ["OLLAMA_CHAT_MODEL"]
    del os.environ["OLLAMA_TOOL_MODEL"]
