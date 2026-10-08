"""The gateway loop: model JSON -> Draft -> firewall -> string, with a fake model so it is hermetic.

Proves that even when the MODEL itself fabricates (returns a plausible narrative as a 'fact'), the pipeline
strips it, and that the prompt we send is Gemma-correct (one user turn, no system role).
"""
import asyncio
import json

from mongoose.pipeline import Draft, Evidence, Source, TurnHandler, TurnRequest
from mongoose.pipeline.gateway import DRAFT_SCHEMA, PERSONA, LlmGateway


def _evidence():
    return Evidence(sources=[
        Source(id="bench:gemma4_12b", kind="benchmark_result",
               data={"model": "gemma4:12b-it-q4_K_M", "tasks_passed": "3/7", "warm_chat_median_s": 2.24}),
        Source(id="bench:gemma4_26b", kind="benchmark_result",
               data={"model": "gemma4:26b-a4b-it-q4_K_M", "tasks_passed": "4/7", "warm_chat_median_s": 0.98}),
    ])


def _handler(gateway):
    class _H(TurnHandler):
        intent = "benchmark"
        async def fetch(self, req, deps):
            return _evidence()
        async def compose(self, req, evidence, deps):
            return await gateway.generate_draft(req.text, evidence)
    return _H()


def _fake(returning_json: str):
    async def _call(prompt, schema, role):
        _fake.last_prompt = prompt
        _fake.last_schema = schema
        return returning_json
    return _call


def test_model_fabrication_is_stripped_end_to_end():
    # the model returns a plausible narrative dressed as a fact; enforce must reject it
    bad = json.dumps({"facts": [
        {"text": "26b scored higher on logic and reasoning", "source_id": "bench:gemma4_26b", "field": "tasks_passed"},
        {"text": "qwen3 30b outperformed everything", "source_id": "bench:qwen3_30b", "field": "tasks_passed"}],
        "inferences": [], "unknowns": [], "voice": ""})
    r = asyncio.run(_handler(LlmGateway(call=_fake(bad))).run(TurnRequest(text="compare them")))
    assert not r.grounded_ok
    for lie in ("logic", "reasoning", "outperformed"):
        assert lie not in r.text.lower()


def test_honest_model_output_survives_and_reads():
    good = json.dumps({"facts": [
        {"text": "gemma4 12b passed 3/7 checks", "source_id": "bench:gemma4_12b", "field": "tasks_passed"},
        {"text": "gemma4 26b passed 4/7 checks", "source_id": "bench:gemma4_26b", "field": "tasks_passed"}],
        "inferences": [{"text": "26b cleared one more than 12b", "from_ids": ["bench:gemma4_12b", "bench:gemma4_26b"]}],
        "unknowns": ["any logic or reasoning score"], "voice": "here is what the run actually shows"})
    r = asyncio.run(_handler(LlmGateway(call=_fake(good))).run(TurnRequest(text="how did they do")))
    assert r.grounded_ok and not r.rejected
    assert "3/7" in r.text and "4/7" in r.text and "Reading into that" in r.text


def test_prompt_is_gemma_correct_and_grounded():
    gw = LlmGateway()
    prompt = gw.build_prompt("how did gemma 12b do?", _evidence(), guidance="benchmark measures speed only")
    # one user turn: we never emit a system role; the persona + evidence + question are all in the prompt
    assert PERSONA[:40] in prompt
    assert "EVIDENCE" in prompt and "bench:gemma4_12b" in prompt and "3/7" in prompt
    assert "QUESTION: how did gemma 12b do?" in prompt
    assert "benchmark measures speed only" in prompt


def test_parse_tolerates_garbage():
    gw = LlmGateway()
    assert gw.parse("not json at all") == Draft()
    assert gw.parse("[1,2,3]") == Draft()          # valid json, wrong shape -> empty, safe


def test_schema_matches_the_draft_contract():
    assert set(DRAFT_SCHEMA["properties"]) == {"facts", "inferences", "unknowns", "voice"}
