"""The 'what can I say?' guide: discoverable lingo that cannot drift from reality.

the owner's requirement: he should be able to ask chat in ANY natural language for the right
utterances to drive Mongoose (plugin build-out, self-heal, future capabilities) because
nobody remembers tool lingo by heart. These tests pin:
  1. topic matching in English AND Polish reaches the right workflow;
  2. the empty-topic overview lists every workflow with a first thing to say;
  3. ANTI-DRIFT CONTRACT: every workflow step that names a tool names one that exists in the
     live TOOLS registry, and every 'say' phrase is non-empty -- the guide can never teach
     utterances for tools that were renamed or removed;
  4. an unmatched topic degrades to the full map, never to nothing;
  5. the tool itself is registered, read-only (in READ_TOOLS), and its executor answers.
"""
from __future__ import annotations

import asyncio
import warnings

warnings.filterwarnings("ignore")

from mongoose.services.capability_guide import WORKFLOWS, guide


def test_overview_lists_every_workflow_with_an_opening_utterance():
    r = guide("")
    assert r["ok"]
    ids = {w["id"] for w in r["workflows"]}
    assert ids == {w["id"] for w in WORKFLOWS}
    assert all(w["first_thing_to_say"] for w in r["workflows"])


def test_english_and_polish_topics_reach_the_right_workflow():
    for topic, expected in [("plugin", "plugin_build_out"),
                            ("wordpress site", "plugin_build_out"),
                            ("wtyczka", "plugin_build_out"),
                            ("self heal", "self_heal"),
                            ("napraw", "self_heal"),
                            ("update", "update_mongoose"),
                            ("aktualizacja", "update_mongoose"),
                            ("service charge", "rtm_paperwork"),
                            ("gość", "guests")]:
        r = guide(topic)
        assert any(m["id"] == expected for m in r.get("matched", [])), (topic, r)


def test_matched_workflow_carries_full_steps_with_say_and_does():
    r = guide("plugin")
    wf = next(m for m in r["matched"] if m["id"] == "plugin_build_out")
    assert len(wf["steps"]) >= 4
    for step in wf["steps"]:
        assert step["say"].strip() and step["does"].strip()
    says = " ".join(s["say"] for s in wf["steps"])
    assert "Refresh the plugin template" in says and "confirm <token>" in says


def test_anti_drift_every_referenced_tool_exists():
    from mongoose.tools import TOOLS
    for wf in WORKFLOWS:
        for step in wf["steps"]:
            tool = step.get("tool", "")
            if tool:
                assert tool in TOOLS, (
                    f"workflow {wf['id']} step '{step['say']}' references tool '{tool}' "
                    "which is not in the live registry -- the guide is teaching dead lingo")


def test_unmatched_topic_degrades_to_full_map_never_nothing():
    r = guide("xyzzy quantum flamingo")
    assert r["ok"] and r["matched"] == []
    assert {w["id"] for w in r["workflows"]} == {w["id"] for w in WORKFLOWS}
    assert "hint" in r


def test_registry_search_surfaces_tools_without_recipes():
    r = guide("voicemail")
    names = {t["tool"] for t in r.get("related_tools", [])}
    assert "search_voicemails" in names


def test_tool_is_registered_read_only_and_answers():
    from mongoose.tools import TOOLS, READ_TOOLS, SENSITIVE_TOOLS
    assert "capability_guide" in TOOLS
    assert "capability_guide" in READ_TOOLS
    assert "capability_guide" not in SENSITIVE_TOOLS
    out = asyncio.run(TOOLS["capability_guide"]["executor"]({"topic": "fix a bug"}))
    assert out["ok"] and any(m["id"] == "self_heal" for m in out["matched"])
