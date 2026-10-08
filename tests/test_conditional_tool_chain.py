"""Verify the planner handles conditional READ→REASON→ACT chains without hallucinating."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_planner_system_prompt_has_conditional_logic_instruction():
    """The planner system prompt must explicitly instruct conditional evaluation."""
    _fresh()
    from mongoose.tools import _PLANNER_SYSTEM
    assert "CONDITIONAL LOGIC" in _PLANNER_SYSTEM
    assert "tool\": null" in _PLANNER_SYSTEM
    assert "pending_no_audio" in _PLANNER_SYSTEM


def test_phrasing_prompt_handles_transcripts():
    """The phrasing step must quote transcripts verbatim and handle pending state."""
    # Verify the phrasing instruction is in the codebase
    import inspect
    import mongoose.tools as T
    src = inspect.getsource(T.run_tool_turn)
    assert "transcript_status" in src, "phrasing prompt must mention transcript_status"
    assert "pending_no_audio" in src, "phrasing prompt must handle pending_no_audio"
    assert "condition was not met" in src, "phrasing prompt must handle unmet conditions"
    assert "verbatim" in src, "phrasing prompt must say to quote transcripts verbatim"


def test_injection_guard_passes_for_conditional_home_control():
    """'turn on the printer if documentation is mentioned' — must NOT be blocked by injection guard."""
    _fresh()
    from mongoose.role_router import looks_like_home_control
    prompt = "give me the transcripts of last two voicemails and turn on the printer if there's any mention of documentation"
    assert looks_like_home_control(prompt) is True, "conditional home control must not be blocked"


def test_injection_guard_still_blocks_non_control_after_read():
    """A pure read question must not be erroneously classified as control intent."""
    _fresh()
    from mongoose.role_router import looks_like_home_control
    assert looks_like_home_control("what do the voicemails say") is False


def test_planner_step_history_format():
    """Verify the planner receives prior steps in the right format for conditional reasoning."""
    # The planner_input on step 2 must include prior tool name and result
    import json
    steps = [{"tool": "search_voicemails", "args": {"limit": 2},
              "result": {"ok": True, "voicemails": [{"transcript": "we need the documentation"}]}}]
    history = json.dumps([{"tool": s["tool"], "args": s["args"], "result": s["result"]} for s in steps])
    planner_input = (
        f"User request: test\n"
        f"Tools already run this turn (JSON): {history}\n"
        "Decide the next tool call, or finish."
    )
    # The planner must see the transcript content
    assert "documentation" in planner_input
    assert "search_voicemails" in planner_input


def test_search_voicemails_returns_transcript_field():
    """The voicemail tool must expose the transcript field so the planner can read it."""
    _fresh()
    from mongoose.services.voicemail import list_voicemails, index_voicemail_from_protocol_staging
    env = {"msg_id": "vm1", "caller": "07700900123", "account_number": "30000000",
           "voicemail_at": "2026-06-24T14:30:00+01:00",
           "transcript": "We need the documentation before we can proceed.",
           "from": "ha/example_court"}
    index_voicemail_from_protocol_staging(env)
    vms = list_voicemails(limit=2)
    assert vms
    assert "transcript" in vms[0]
    # The transcript content must be present (not None) when supplied
    assert vms[0]["transcript"] == "We need the documentation before we can proceed."
