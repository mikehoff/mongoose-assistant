"""The single route. Every surface calls TurnRouter.handle(); it classifies intent once, picks exactly one
trap door (or the conversational fallback), runs it through the sealed lifecycle, and traces it. There is no
other path to the model from a user turn. In Phase 0 this runs in shadow beside the live chain."""
from __future__ import annotations

import re

from .base import TurnHandler, TurnRequest, TurnResult
from .trace import TurnTrace

# A self-question: about Mongoose itself (its ram, version, models, tools, wiring, capabilities).
_SELF_RE = re.compile(
    r"(?i)\b(your|you)\s+(ram|memory|version|models?|tools?|wiring|config|setup|brain)\b"
    r"|\bwhat can you do\b|\bdo you have\b|\bhow much (ram|memory)\b|\bwhich model\b|\bare you (able|running)\b")
# A benchmark/model-performance question (family name or a size token).
_BENCH_RE = re.compile(r"(?i)\bbenchmark|\b(gemma|qwen|gpt[-\s]?oss|llama|mistral|phi|deepseek)\b|\b\d{1,3}b\b")
# A question about a specific document / invoice / attachment.
_DOC_RE = re.compile(r"(?i)\b(invoice|attachment|receipt|pdf|the .{0,30}\bdocument\b|bank details on|sort code on)\b")
# An RTM service-charge / money question.
_RTM_RE = re.compile(
    r"(?i)\b(rtm|service charge|arrears|leaseholder|demand[s]?|owe[sd]?|outstanding|reserve fund|"
    r"example court|apportion|paid|unpaid)\b")


class TurnRouter:
    def __init__(self, handlers: "list[TurnHandler]", fallback: TurnHandler) -> None:
        self._by_intent = {h.intent: h for h in handlers}
        self._fallback = fallback

    async def handle(self, req: TurnRequest) -> TurnResult:
        handler = self.select(req)
        result = await handler.run(req)
        TurnTrace.emit(req, result)
        return result

    def select(self, req: TurnRequest) -> TurnHandler:
        return self._by_intent.get(self.classify(req), self._fallback)

    def classify(self, req: TurnRequest) -> str:
        t = req.text or ""
        # 1) the existing resolver, when it names an intent we actually have a trap door for
        try:
            from ..services.intent_resolver import resolve
            r = (resolve(t, guest=req.guest) or [None])[0]
            if r in self._by_intent:
                return r
            if isinstance(r, str) and r.startswith("rtm") and "rtm" in self._by_intent:
                return "rtm"
        except Exception:
            pass
        # 2) self-questions before model-token questions, so "which model are you" goes to self, not benchmark
        if _SELF_RE.search(t) and "self" in self._by_intent:
            return "self"
        if _DOC_RE.search(t) and "document" in self._by_intent:
            return "document"
        if _RTM_RE.search(t) and "rtm" in self._by_intent:
            return "rtm"
        if _BENCH_RE.search(t) and "benchmark" in self._by_intent:
            return "benchmark"
        # 3) everything else is conversation
        return "chat"
