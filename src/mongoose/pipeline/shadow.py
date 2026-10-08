"""The verification gate: run the new route and the old chain on the same turn and diff them, shipping
NOTHING. This is how the pipeline earns the single cutover, by being watched beside the live chain on real
turns first. It touches no live surface; it is a comparator you invoke on demand (or, later, behind a flag
in the device route so it observes real traffic).
"""
from __future__ import annotations

from typing import Any

from .base import TurnRequest
from .handlers import (
    BenchmarkHandler,
    DocumentHandler,
    PlainChatHandler,
    RtmFinanceHandler,
    SelfKnowledgeHandler,
)
from .router import TurnRouter


def default_router() -> TurnRouter:
    """The Phase-0 router: the factual trap doors plus the conversational fallback."""
    return TurnRouter(
        [BenchmarkHandler(), SelfKnowledgeHandler(), DocumentHandler(), RtmFinanceHandler()],
        PlainChatHandler(),
    )


async def compare(prompt: str, *, guest: bool = False) -> dict[str, Any]:
    """Run one prompt through BOTH routes and return both answers for a side-by-side diff. Never raises."""
    req = TurnRequest(text=prompt, guest=guest)
    router = default_router()
    handler = type(router.select(req)).__name__
    try:
        new = await router.handle(req)
        new_text, new_grounded, new_rejected = new.text, new.grounded_ok, len(new.trace.get("rejected", []))
    except Exception as exc:  # the new route must never break the comparison
        new_text, new_grounded, new_rejected = f"(new route error: {exc})", False, -1
    try:
        from ..llm import chat_with_memory
        res = await chat_with_memory(prompt, conversation_id="shadow-compare", role="chat",
                                     skip_user_persist=True)
        old_text = (res or {}).get("reply", "")
    except Exception as exc:
        old_text = f"(old route error: {exc})"
    return {
        "prompt": prompt,
        "handler": handler,
        "new": new_text,
        "new_grounded": new_grounded,
        "new_rejected": new_rejected,
        "old": old_text,
    }
