"""The voicemail hallucination ('you have one new voicemail from Sam' when there are zero) must not
recur. Two defences: (1) when the tool runs, the AUTHORITATIVE OUTCOME states the real count, and
(2) when no tool runs on a data-lookup question, the model is forbidden from inventing specifics."""
import asyncio
import json
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_salient_facts_states_zero():
    from mongoose.tools import _salient_result_facts
    f = _salient_result_facts({"ok": True, "voicemails": [], "stats": {"total": 0}})
    assert "EXACTLY 0 voicemails" in f
    assert "do not invent" in f


def test_salient_facts_states_count():
    from mongoose.tools import _salient_result_facts
    assert "EXACTLY 2 messages" in _salient_result_facts({"ok": True, "messages": [1, 2]})


def test_ground_truth_pins_empty_voicemails(monkeypatch):
    _fresh()
    import mongoose.tools as T
    cap = {}
    it = iter([json.dumps({"tool": "search_voicemails", "args": {}}), json.dumps({"tool": None})])

    async def fo(p, system=None, role=None):
        try:
            return next(it)
        except StopIteration:
            return json.dumps({"tool": None})

    async def fp(p, role=None):
        cap["p"] = p
        return "ok"

    monkeypatch.setattr(T, "ask_ollama", fo)
    monkeypatch.setattr(T, "ask_persona", fp)
    asyncio.run(T.run_tool_turn("any voicemails?"))
    assert "EXACTLY 0 voicemails" in cap["p"]
    assert "MUST NOT contradict" in cap["p"]


def test_data_lookup_detected():
    from mongoose.tools import _is_data_lookup
    for q in ("any voicemails?", "do I have any messages", "did Sam call", "anything from Jordan",
              "what's on my calendar", "check my voicemail"):
        assert _is_data_lookup(q), q
    for q in ("tell me a joke", "what is a mongoose", "how are you"):
        assert not _is_data_lookup(q), q


def test_no_tool_lookup_gets_guarded_prompt(monkeypatch):
    """If the planner free-answers a data lookup (no tool), the persona prompt forbids inventing."""
    _fresh()
    import mongoose.tools as T
    cap = {}

    async def fo(p, system=None, role=None):
        return json.dumps({"tool": None})  # planner declines to use a tool

    async def fp(p, role=None):
        cap["p"] = p
        return "I couldn't check just now."

    monkeypatch.setattr(T, "ask_ollama", fo)
    monkeypatch.setattr(T, "ask_persona", fp)
    asyncio.run(T.run_tool_turn("did Sam call?"))
    # the guarded prompt must forbid inventing specifics
    assert "Do NOT invent" in cap["p"] or "do not invent" in cap["p"].lower()
    assert "NO information" in cap["p"]


def test_local_plan_routes_generic_voicemail_to_canonical_index():
    from mongoose.tools import _local_status_plan

    for q in ("any voicemails?", "check my voicemail", "what voicemails do I have"):
        plan = _local_status_plan(q)
        assert plan is not None, q
        assert plan["tool"] == "search_voicemails", plan
        assert plan["args"] == {"limit": 10}

    plan = _local_status_plan("any voicemails from 07700 900123")
    assert plan == {"tool": "search_voicemails", "args": {"limit": 10, "query": "07700900123"}}


def test_local_plan_keeps_protocol_v3_voicemail_separate():
    from mongoose.tools import _local_status_plan

    for q in ("check the protocol v3 voicemail staging", "drain the HA/Pi staged voicemail"):
        assert _local_status_plan(q) == {"tool": "v3_coordination", "args": {"mode": "voicemail"}}

    assert _local_status_plan("fix voicemail route") is None


def test_role_router_voicemail_phrasings_are_tool_turns():
    from mongoose.role_router import classify_role

    for q in ("any voicemails?", "check my voicemail", "what voicemails do I have",
              "check the protocol v3 voicemail staging"):
        assert classify_role(q) == "tool", q
