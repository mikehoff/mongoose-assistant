"""Conversational fallback: genuinely non-factual turns (banter, empathy, a joke).

This is the one handler that is not a data lookup, so it is marked factual=False: its voice may carry a
legit number (a time, a count) because there is no data being smuggled. Its safety rests on the router NOT
sending factual questions here, plus the base rule that a data claim would still have to be a sourced fact.
A deeper backstop (spotting a smuggled data claim in free chat) is future work, tracked in the doc.
"""
from __future__ import annotations

from typing import Any

from ..base import Draft, Evidence, TurnHandler, TurnRequest


class PlainChatHandler(TurnHandler):
    intent = "chat"

    async def fetch(self, req: TurnRequest, deps: Any) -> Evidence:
        return Evidence()  # a conversation is not evidence to cite

    async def compose(self, req: TurnRequest, evidence: Evidence, deps: Any) -> Draft:
        from ...llm import ask_persona
        try:
            reply = await ask_persona(req.text, role="chat")
        except Exception:
            reply = ""
        return Draft(voice=(reply or "").strip(), factual=False)
