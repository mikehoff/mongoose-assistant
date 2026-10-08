"""The anti-fabrication guarantee, proven at the skeleton level.

These tests are the whole point of the re-engineering: a trap door (subclass) CANNOT get a fabricated fact
to the user, because the sealed base class enforces the grounding contract on the Draft it returns and owns
the final string. No model, no DB: the guarantee is structural and unit-testable.

The headline is `test_benchmark_incident_cannot_recur`: it reproduces the exact fabrication from 2026-07-20
and shows the pipeline strips it.
"""
import asyncio

import pytest

from mongoose.pipeline import Draft, Evidence, Fact, Inference, Source, TurnHandler, TurnRequest


def _run(handler, text="q"):
    return asyncio.run(handler.run(TurnRequest(text=text)))


# --- the real 2026-07-20 scoreboard, as evidence rows ---
def _benchmark_evidence() -> Evidence:
    return Evidence(sources=[
        Source(id="bench:gemma4_12b", kind="benchmark_result",
               data={"model": "gemma4:12b-it-q4_K_M", "tasks_passed": "3/7", "cold_load_s": 2.8,
                     "warm_chat_median_s": 2.24, "tool_calls_valid": "0/5"}),
        Source(id="bench:gemma4_26b", kind="benchmark_result",
               data={"model": "gemma4:26b-a4b-it-q4_K_M", "tasks_passed": "4/7", "cold_load_s": 7.8,
                     "warm_chat_median_s": 0.98, "tool_calls_valid": "0/5"}),
    ])


class _HonestBenchmark(TurnHandler):
    intent = "benchmark"
    async def fetch(self, req, deps):
        return _benchmark_evidence()
    async def compose(self, req, evidence, deps):
        return Draft(
            facts=[Fact("gemma4 12b passed 3/7 checks", "bench:gemma4_12b", field="tasks_passed"),
                   Fact("gemma4 26b passed 4/7 checks", "bench:gemma4_26b", field="tasks_passed")],
            inferences=[Inference("26b cleared one more check than 12b in this run",
                                  ["bench:gemma4_12b", "bench:gemma4_26b"])],
            unknowns=["any reasoning or logic score (the benchmark does not measure that)"])


class _FabricatingBenchmark(TurnHandler):
    """The exact 2026-07-20 failure: a plausible narrative with no source behind it."""
    intent = "benchmark"
    async def fetch(self, req, deps):
        return _benchmark_evidence()
    async def compose(self, req, evidence, deps):
        return Draft(facts=[
            # cites a real source+field, but the text states a narrative, not the field's real value ("4/7")
            Fact("26b scored significantly higher on the logic and deep-context tasks",
                 "bench:gemma4_26b", field="tasks_passed"),
            # cites a real field, but never states its real value ("2.24")
            Fact("12b beat the 26b quite easily on throughput",
                 "bench:gemma4_12b", field="warm_chat_median_s"),
            # no such source at all
            Fact("qwen3 30b outperformed the smaller models convincingly",
                 "bench:qwen3_30b", field="tasks_passed"),
        ])


class _WrongNumber(TurnHandler):
    intent = "benchmark"
    async def fetch(self, req, deps):
        return _benchmark_evidence()
    async def compose(self, req, evidence, deps):
        # cites a REAL source but states a number that is not in it (7/7 vs the real 3/7)
        return Draft(facts=[Fact("gemma4 12b passed 7/7 checks", "bench:gemma4_12b", field="tasks_passed")])


class _EmptyEvidence(TurnHandler):
    intent = "benchmark"
    async def fetch(self, req, deps):
        return Evidence(sources=[])
    async def compose(self, req, evidence, deps):
        # model tries to answer anyway; every claim must be rejected
        return Draft(facts=[Fact("the 30b fit comfortably in RAM", "sys:ram", field="ram_ok")])


def test_honest_handler_passes_and_reads_naturally():
    r = _run(_HonestBenchmark())
    assert r.grounded_ok and not r.rejected
    assert "3/7" in r.text and "4/7" in r.text
    assert "Reading into that" in r.text            # inference is marked as a read, not a result
    assert "do not have" in r.text.lower() or "does not measure" in r.text.lower()


def test_benchmark_incident_cannot_recur():
    r = _run(_FabricatingBenchmark())
    # every fabricated claim is stripped
    assert not r.grounded_ok
    assert len(r.rejected) == 3
    for lie in ("logic", "throughput", "outperformed"):
        assert lie not in r.text.lower(), f"fabrication leaked: {lie}"


def test_valid_source_but_wrong_number_is_caught():
    r = _run(_WrongNumber())
    assert not r.grounded_ok
    assert "7/7" not in r.text                       # the wrong number never reaches the user


def test_empty_evidence_yields_honest_nothing_not_a_guess():
    r = _run(_EmptyEvidence())
    assert not r.grounded_ok
    assert "ram" not in r.text.lower() and "fit" not in r.text.lower()
    assert "do not have" in r.text.lower()


def test_voice_may_not_smuggle_a_figure():
    class _SneakyVoice(TurnHandler):
        intent = "benchmark"
        async def fetch(self, req, deps):
            return _benchmark_evidence()
        async def compose(self, req, evidence, deps):
            return Draft(voice="Honestly the 12b hit like 9/10 for me",
                         facts=[Fact("gemma4 12b passed 3/7 checks", "bench:gemma4_12b", field="tasks_passed")])
    r = _run(_SneakyVoice())
    assert "9/10" not in r.text
    assert "3/7" in r.text


def test_compose_returning_free_prose_is_refused():
    class _ProseHandler(TurnHandler):
        intent = "x"
        async def fetch(self, req, deps):
            return Evidence()
        async def compose(self, req, evidence, deps):
            return "the 26b is the heavyweight champion"   # not a Draft
    r = _run(_ProseHandler())
    assert not r.grounded_ok and "heavyweight" not in r.text.lower()


def test_lifecycle_methods_are_sealed_against_override():
    with pytest.raises(TypeError):
        class _Escapee(TurnHandler):
            intent = "x"
            async def fetch(self, req, deps):
                return Evidence()
            async def compose(self, req, evidence, deps):
                return Draft()
            async def run(self, req, deps=None):           # forbidden
                from mongoose.pipeline import TurnResult
                return TurnResult(text="I bypassed the firewall", grounded_ok=True)

    with pytest.raises(TypeError):
        class _Escapee2(TurnHandler):
            intent = "x"
            async def fetch(self, req, deps):
                return Evidence()
            async def compose(self, req, evidence, deps):
                return Draft()
            def render_text(self, draft):                  # also forbidden
                return "whatever I want"
