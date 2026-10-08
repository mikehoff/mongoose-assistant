"""Turn observability. Every turn's shape is recorded: intent, handler, sources, how many facts survived,
what was rejected. This is both the debugging surface and the data behind 'show me the source': a rising
rejected count means a handler is under-fetching. In-memory ring for v1; a durable sink is pluggable."""
from __future__ import annotations

from collections import deque

from .base import TurnRequest, TurnResult

_RING: "deque[dict]" = deque(maxlen=300)


class TurnTrace:
    @staticmethod
    def emit(req: TurnRequest, result: TurnResult) -> None:
        rec = {
            "text": (req.text or "")[:200],
            "surface": req.surface,
            "speaker": req.speaker,
            "grounded_ok": result.grounded_ok,
            "reply": (result.text or "")[:400],
            **(result.trace or {}),
        }
        _RING.append(rec)

    @staticmethod
    def recent(n: int = 20) -> list[dict]:
        return list(_RING)[-n:]

    @staticmethod
    def clear() -> None:
        _RING.clear()
