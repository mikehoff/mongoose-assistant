"""The one model door for the pipeline.

Two published constraints shape this (see docs/ARCHITECTURE_REENGINEERING.md, Sources):
  - Gemma has NO system role. System-level instruction goes at the head of the FIRST user turn, not a
    phantom `system` message that Ollama would merge and bury. So we build a single user turn.
  - Ollama structured outputs: pass a JSON schema as `format` so the model returns a parseable object, and
    still instruct it in-prompt to produce that shape (the format constraint is not shown to the model).

The gateway returns a Draft (structured claims), never a finished sentence. Whatever it returns still passes
through the grounding firewall in the sealed base, so even a model that fabricates cannot reach the user
with an unsourced claim. This module is self-contained (its own ollama call) to keep the new route isolated
from the legacy llm path.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from .base import Draft, Evidence, Fact, Inference

# Short, hard-first, Gemma-correct. The encyclopedic catalog does NOT live here; it becomes evidence/tools.
PERSONA = (
    "You are Mongoose, a local-first household assistant: warm, perceptive, dry, but never misleading. "
    "Answer ONLY from the EVIDENCE below. A fact must cite a source id and one of that source's fields, and "
    "must quote that field's real value; if you were asked something the evidence does not contain, put it "
    "in 'unknowns' and do NOT invent it. Never state a score, ranking, speed, comparison, or quality that is "
    "not a real value in the evidence. Inferences are your reads drawn from the cited facts, offered as "
    "reads and never as recorded results. Be perceptive about what the real data MEANS; never about data you "
    "do not have."
)

# The Draft contract as an Ollama/JSON schema. Mirrors pipeline.base.Draft.
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "source_id": {"type": "string"},
                           "field": {"type": "string"}},
            "required": ["text", "source_id", "field"]}},
        "inferences": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"},
                           "from_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "from_ids"]}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "voice": {"type": "string"},
    },
    "required": ["facts", "inferences", "unknowns"],
}

# A test/alternate model call: (prompt, schema, role) -> raw JSON string.
ModelCall = Callable[[str, dict, str], Awaitable[str]]


class LlmGateway:
    def __init__(self, call: "ModelCall | None" = None) -> None:
        # Inject `call` in tests to run the pipeline without a live model. Default is a real Ollama call.
        self._call = call

    async def generate_draft(self, question: str, evidence: Evidence, *, guidance: str = "",
                             role: str = "chat") -> Draft:
        prompt = self.build_prompt(question, evidence, guidance)
        raw = await self._invoke(prompt, DRAFT_SCHEMA, role)
        return self.parse(raw)

    def build_prompt(self, question: str, evidence: Evidence, guidance: str = "") -> str:
        lines = [PERSONA, ""]
        if guidance:
            lines += [guidance.strip(), ""]
        lines.append("EVIDENCE (the ONLY facts you may use; each has an id, a kind, and data fields):")
        if evidence.sources:
            for s in evidence.sources:
                lines.append(f"- id={s.id} kind={s.kind} data={json.dumps(_jsonable(s.data), ensure_ascii=False)}")
        else:
            lines.append("- (none. You have NO data for this. Any 'fact' would be invented; return unknowns.)")
        example = (
            '{"facts":[{"text":"gemma4 12b passed 3/7 checks","source_id":"bench:gemma4_12b",'
            '"field":"tasks_passed"}],'
            '"inferences":[{"text":"that is mid-pack for this run","from_ids":["bench:gemma4_12b"]}],'
            '"unknowns":["any reasoning or logic score"],"voice":"here is what the run actually shows"}')
        lines += [
            "",
            f"QUESTION: {question}",
            "",
            "Return ONLY a JSON object of EXACTLY this shape:",
            example,
            "Rules: every fact MUST set source_id to one of the evidence ids above, set field to one of that "
            "source's data keys, and QUOTE that field's real value inside text (do not put the id or field "
            "inside text). Put anything the evidence does not contain into unknowns. voice carries NO numbers.",
        ]
        return "\n".join(lines)

    def parse(self, raw: str) -> Draft:
        try:
            obj = json.loads(raw)
        except Exception:
            return Draft()  # nothing parseable means no claims, which is safe
        if not isinstance(obj, dict):
            return Draft()
        facts = [Fact(text=str(f.get("text", "")), source_id=str(f.get("source_id", "")),
                      field=str(f.get("field", "")))
                 for f in obj.get("facts", []) if isinstance(f, dict)]
        infs = [Inference(text=str(i.get("text", "")),
                          from_ids=[str(x) for x in i.get("from_ids", []) if isinstance(x, str)])
                for i in obj.get("inferences", []) if isinstance(i, dict)]
        unknowns = [str(u) for u in obj.get("unknowns", []) if isinstance(u, str)]
        voice = str(obj.get("voice", ""))
        return Draft(facts=facts, inferences=infs, unknowns=unknowns, voice=voice)

    async def _invoke(self, prompt: str, schema: dict, role: str) -> str:
        if self._call is not None:
            return await self._call(prompt, schema, role)
        # Reuse the shared, LOCKED ollama caller so we do not contend with the server's own inference, and
        # use Ollama's light `json` mode rather than the full nested schema (which stalls Gemma). The prompt
        # already specifies the shape. system=None keeps it Gemma-correct: one user turn, no system role.
        from ..llm import ask_ollama
        return await ask_ollama(prompt, system=None, role=role, response_format="json")


def _jsonable(data: object) -> object:
    try:
        json.dumps(data)
        return data
    except Exception:
        return str(data)
