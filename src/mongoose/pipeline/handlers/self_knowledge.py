"""Self-knowledge trap door: Mongoose answering about itself from its real self, not an improvised one."""
from __future__ import annotations

from typing import Any

from ..base import Draft, Evidence, TurnHandler, TurnRequest
from ..gateway import LlmGateway

_GUIDANCE = (
    "Answer about yourself using ONLY these self rows. If asked something they do not contain (for example "
    "whether a specific model 'fits' in RAM, which needs a calculation the rows do not give), name it as an "
    "unknown and offer to work it out, rather than guessing."
)


class SelfKnowledgeHandler(TurnHandler):
    intent = "self"

    def __init__(self, gateway: "LlmGateway | None" = None) -> None:
        self.gateway = gateway or LlmGateway()

    async def fetch(self, req: TurnRequest, deps: Any) -> Evidence:
        from ..selfknowledge import gather
        return gather(deps)

    async def compose(self, req: TurnRequest, evidence: Evidence, deps: Any) -> Draft:
        return await self.gateway.generate_draft(req.text, evidence, guidance=_GUIDANCE)
