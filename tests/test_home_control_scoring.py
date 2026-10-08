"""Regression tests for the all-models-fail-home_control bug.

Root cause: the scorer required args.device_id, but the control_home tool also
accepts entity_id (e.g. light.lounge) and room, which is what well-behaved models
actually produce, so correct plans scored as failures. Plus _parse_plan rejected any
JSON wrapped in prose. These tests lock in the lenient behaviour."""
from mongoose.benchmark import _score_home_control
from mongoose.tools import _parse_plan, _extract_first_json_object


def test_home_control_accepts_device_id():
    assert _score_home_control('{"tool":"control_home","args":{"device_id":"lounge","action":"off"}}')


def test_home_control_accepts_entity_id():
    # the idiomatic HA form: this MUST pass (it was failing for every model)
    assert _score_home_control('{"tool":"control_home","args":{"entity_id":"light.lounge","service":"turn_off"}}')


def test_home_control_accepts_room():
    assert _score_home_control('{"tool":"control_home","args":{"room":"lounge","action":"off"}}')


def test_home_control_accepts_prose_wrapped_json():
    raw = 'Sure, here is the plan:\n{"tool":"control_home","args":{"entity_id":"light.study","service":"turn_off"}}'
    assert _score_home_control(raw)


def test_home_control_accepts_fenced_json():
    raw = '```json\n{"tool":"control_home","args":{"entity_id":"light.x"}}\n```'
    assert _score_home_control(raw)


def test_home_control_accepts_tool_calls_envelope():
    raw = '{"tool_calls":[{"name":"control_home","arguments":{"entity_id":"light.lounge"}}]}'
    assert _score_home_control(raw)


def test_home_control_rejects_chat_reply():
    assert not _score_home_control('{"tool":null}')


def test_home_control_rejects_wrong_tool():
    assert not _score_home_control('{"tool":"search_email","args":{"query":"x"}}')


def test_home_control_rejects_control_without_any_target():
    # control_home but no device_id/entity_id/room -> not actionable
    assert not _score_home_control('{"tool":"control_home","args":{"action":"off"}}')


# --- tolerant plan parsing ---------------------------------------------------
def test_parse_plan_pure_json():
    assert _parse_plan('{"tool":"x","args":{}}')["tool"] == "x"


def test_parse_plan_prose_wrapped():
    assert _parse_plan('here you go: {"tool":"x"} cheers')["tool"] == "x"


def test_parse_plan_braces_in_strings():
    # a brace inside a string value must not confuse the extractor
    obj = _extract_first_json_object('text {"tool":"x","note":"a } b"} more')
    assert obj == '{"tool":"x","note":"a } b"}'


def test_parse_plan_no_json_returns_null_tool():
    assert _parse_plan("no json here at all")["tool"] is None
