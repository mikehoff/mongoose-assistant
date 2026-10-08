"""Context sanity across side-quests: the tool loop's planner prompt must stay bounded no matter how
large individual tool results are or how long the chain runs. Before this, every prior step's full
result was re-serialised into the planner prompt on every subsequent step — a single big search
result could balloon the prompt to hundreds of KB and wreck Gemma's planning."""

import mongoose.tools as T


def _big_result(n=500):
    return {"ok": True, "results": [{"id": i, "body": "x" * 200} for i in range(n)]}


def test_compact_result_shrinks_large_lists():
    out = T._compact_result(_big_result())
    assert len(out) <= 700
    # status preserved, list summarised by count
    assert '"ok": true' in out.lower() or '"ok":true' in out.lower()
    assert "count" in out


def test_compact_result_preserves_decision_keys():
    out = T._compact_result({"ok": False, "error": "no confident device match", "candidates": [1, 2, 3]})
    assert "no confident device match" in out
    assert '"ok": false' in out.lower() or '"ok":false' in out.lower()


def test_planner_history_is_bounded_across_a_chain():
    steps = [
        {"tool": "search_email", "args": {"query": "invoices"}, "result": _big_result()},
        {"tool": "search_files", "args": {"query": "contract"}, "result": _big_result()},
        {"tool": "search_notifications", "args": {"query": "x"}, "result": _big_result()},
        {"tool": "get_weather", "args": {}, "result": {"ok": True, "current": "12C"}},
    ]
    rendered = T._summarise_steps_for_planner(steps)
    # The naive serialisation would be > 200KB; bounded version must be tiny.
    assert len(rendered) < 3000
    # Most recent step must always survive the budget.
    assert "get_weather" in rendered


def test_planner_history_keeps_recent_drops_oldest():
    # Many steps, each moderately sized: oldest should be dropped, newest kept.
    steps = [
        {"tool": f"tool_{i}", "args": {"n": i}, "result": {"ok": True, "blob": "y" * 400}}
        for i in range(20)
    ]
    rendered = T._summarise_steps_for_planner(steps)
    assert "tool_19" in rendered   # newest kept
    assert "tool_0" not in rendered  # oldest dropped under budget


def test_single_huge_result_still_bounded():
    steps = [{"tool": "research", "args": {}, "result": {"ok": True, "notes": "z" * 100000}}]
    rendered = T._summarise_steps_for_planner(steps)
    assert len(rendered) < 3000
    assert "research" in rendered
