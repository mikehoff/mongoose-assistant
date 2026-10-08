from mongoose.benchmark import _row, _median, DEFAULT_SHORTLIST, looks_fact_bearing


def test_shortlist_is_five_ollama_fitting_models():
    assert len(DEFAULT_SHORTLIST) == 5
    # none of the too-big models that don't fit 24GB
    joined = " ".join(DEFAULT_SHORTLIST)
    assert "120b" not in joined and "235b" not in joined and "397b" not in joined


def test_median():
    assert _median([3, 1, 2]) == 2
    assert _median([4, 1, 2, 3]) == 2.5
    assert _median([]) != _median([])  # nan


def test_row_handles_error_and_success():
    assert "ERROR" in _row({"model": "x", "error": "boom"})
    ok = _row({"model": "m", "cold_load_s": 5.0, "warm_chat_median_s": 2.0,
               "warm_chat_max_s": 2.5, "tool_calls_valid": "5/5",
               "tool_latency_median_s": 2.1, "memory_extract_ok": True, "memory_extract_n": 2})
    assert "tool_ok=  5/5" in ok and "mem=Y" in ok


def test_prefilter_used_by_benchmark():
    # the benchmark relies on the same pre-filter; sanity check it's the real one
    assert looks_fact_bearing("I like teal") is True
    assert looks_fact_bearing("what's the weather") is False
