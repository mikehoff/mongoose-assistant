"""Benchmark trap door: the incident's home, rebuilt as a subclass that cannot fabricate.

fetch() pulls the REAL per-model scoreboard rows as evidence; compose() hands them to the gateway and gets a
Draft back. The sealed base then enforces the contract and renders. There is no path in this class that can
state a per-model score the benchmark did not measure: "26b scored higher on logic" names no real field, so
it dies in enforce(), no matter what the model writes.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Evidence, Source, TurnHandler, TurnRequest, Draft
from ..gateway import LlmGateway

_GUIDANCE = (
    "This benchmark measures OPERATIONAL fitness only: startup (cold_load_s) and response "
    "(warm_chat_median_s, task_latency_median_s) speed, tool-call validity (tool_calls_valid), and memory "
    "extraction (memory_extract_ok). It does NOT measure reasoning, logic, intelligence, or answer quality, "
    "and nothing here is about RAM footprint. Never claim any of those; if asked, put it in unknowns."
)


# The salient, citable fields. Dropping the nested method/iqr/n noise keeps the evidence flat and small,
# which both shrinks the prompt and gives the model clean fields to cite.
_KEEP = ("model", "tasks_passed", "cold_load_s", "warm_chat_median_s", "task_latency_median_s",
         "tool_calls_valid", "tool_latency_median_s", "memory_extract_ok", "timeouts")


def _slug(model: str) -> str:
    return "bench:" + re.sub(r"[^a-z0-9]+", "_", (model or "").lower()).strip("_")


def _trim(row: dict) -> dict:
    return {k: row[k] for k in _KEEP if k in row}


class BenchmarkHandler(TurnHandler):
    intent = "benchmark"

    def __init__(self, gateway: "LlmGateway | None" = None) -> None:
        self.gateway = gateway or LlmGateway()

    async def fetch(self, req: TurnRequest, deps: Any) -> Evidence:
        try:
            from ...benchmark import load_results_for_recommend
            rows, _ = load_results_for_recommend()
        except Exception:
            rows = []
        sources = [Source(id=_slug(r.get("model", f"m{i}")), kind="benchmark_result", data=_trim(r))
                   for i, r in enumerate(rows) if isinstance(r, dict)]
        return Evidence(sources=sources)

    async def compose(self, req: TurnRequest, evidence: Evidence, deps: Any) -> Draft:
        return await self.gateway.generate_draft(req.text, evidence, guidance=_GUIDANCE)
