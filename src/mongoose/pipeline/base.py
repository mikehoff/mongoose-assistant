"""The hardened turn skeleton.

One sealed base class every trap door subclasses. The guarantee is inheritance, not discipline: a subclass
fills in fetch() and compose() and NOTHING ELSE; the base owns the lifecycle, the grounding contract, and
the final rendered string. A handler physically cannot emit a factual claim it did not fetch, because:

  1. compose() must return a Draft (structured facts/inferences/unknowns), never free prose.
  2. run() enforces the grounding contract on that Draft (see grounding.enforce) before rendering.
  3. run() and render_text() are SEALED: __init_subclass__ refuses any subclass that overrides them.

This module has no model or DB dependency, so the guarantee is unit-testable in isolation.
See docs/ARCHITECTURE_REENGINEERING.md.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Methods a trap door may NOT override. The whole safety property rests on these staying base-owned.
_SEALED = ("run", "render_text")


@dataclass
class TurnRequest:
    """One turn from any surface (phone chat, voice, lounge, MCP, CLI). The single input to the router."""
    text: str
    speaker: str = "owner"          # "owner" or "guest:<device_id>"
    surface: str = "chat"           # chat | voice | lounge | mcp | cli
    guest: bool = False
    conversation_id: str = "default"


@dataclass
class Source:
    """One row of real evidence, kept raw so it can back a claim AND power 'show me the source'."""
    id: str
    kind: str                       # e.g. "benchmark_result", "attachment", "self_config"
    data: Any                       # the actual row/dict/text this claim must be true against


@dataclass
class Evidence:
    """Everything a handler actually fetched this turn. If it is empty, no factual claim can survive."""
    sources: list[Source] = field(default_factory=list)

    def ids(self) -> set[str]:
        return {s.id for s in self.sources}

    def get(self, source_id: str) -> "Source | None":
        return next((s for s in self.sources if s.id == source_id), None)

    @property
    def empty(self) -> bool:
        return not self.sources


@dataclass
class Fact:
    """A claim that MUST be literally true against a cited FIELD of a cited source. The contract requires the
    real value of `field` to appear verbatim in `text`, which is what defeats qualitative fabrication: a
    claim like "26b scored higher on logic" names no real field value and is rejected, even though it cites a
    real source and contains no wrong number. To state a fact you must quote a real value."""
    text: str
    source_id: str
    field: str = ""                 # advisory; the contract DERIVES the verified field from the quoted value


@dataclass
class Inference:
    """A read DRAWN FROM cited facts. Allowed and encouraged, but rendered as a read, never as a result."""
    text: str
    from_ids: list[str] = field(default_factory=list)


@dataclass
class Draft:
    """What compose() returns: structured, verifiable, NEVER the final prose. The base renders the string."""
    facts: list[Fact] = field(default_factory=list)
    inferences: list[Inference] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)   # what was asked but is not in evidence
    voice: str = ""                                       # tone/framing only; must carry no factual claim
    factual: bool = True                                  # False for pure conversation (jokes, empathy):
    #                                                       then voice may carry legit numbers (a time, a
    #                                                       count) because there is no data being smuggled.


@dataclass
class TurnResult:
    text: str
    grounded_ok: bool
    rejected: list[str] = field(default_factory=list)
    trace: dict = field(default_factory=dict)


class TurnHandler(ABC):
    """The one hardened base. Subclass it, set `intent`, implement fetch() and compose(). That is all you
    are permitted to do: run() and render_text() are sealed by __init_subclass__."""

    intent: str = ""
    reflect: bool = False           # opt in to a second, evidence-checked pass for high-stakes turns

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in _SEALED:
            if name in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} may not override the sealed method {name!r}. "
                    "The anti-fabrication guarantee depends on it staying base-owned; "
                    "implement fetch() and compose() instead.")

    # --- the trap door fills these in ---
    @abstractmethod
    async def fetch(self, req: TurnRequest, deps: Any) -> Evidence:
        """Gather REAL data (DB rows, tool results, files, self-knowledge). May legitimately be empty."""

    @abstractmethod
    async def compose(self, req: TurnRequest, evidence: Evidence, deps: Any) -> Draft:
        """Produce a Draft FROM evidence only. Return structured claims, never a finished sentence."""

    # --- SEALED: the base owns these; a subclass that overrides them fails at import ---
    async def run(self, req: TurnRequest, deps: Any = None) -> TurnResult:
        from .grounding import enforce
        evidence = await self.fetch(req, deps)
        draft = await self.compose(req, evidence, deps)
        if not isinstance(draft, Draft):
            # A handler that returns free prose is a bug, and letting it through would defeat the guarantee.
            return TurnResult(
                text="I don't have that in front of me right now.",
                grounded_ok=False,
                rejected=["compose() did not return a Draft"],
                trace={"intent": self.intent, "handler": type(self).__name__, "error": "no_draft"})
        clean, rejected = enforce(draft, evidence)
        text = self.render_text(clean)
        return TurnResult(
            text=text,
            grounded_ok=(not rejected),
            rejected=rejected,
            trace={
                "intent": self.intent,
                "handler": type(self).__name__,
                "sources": [s.id for s in evidence.sources],
                "facts": len(clean.facts),
                "inferences": len(clean.inferences),
                "unknowns": len(clean.unknowns),
                "rejected": rejected,
            })

    def render_text(self, draft: Draft) -> str:
        """Assemble the final string from the VERIFIED draft only. Facts stated plainly; inferences marked
        as reads; unknowns named honestly. Deterministic in v1 (a warmth pass, itself re-checked, comes
        later): correctness before charm."""
        parts: list[str] = []
        if draft.voice:
            parts.append(draft.voice.strip())
        if draft.facts:
            parts.append(" ".join(_sentence(f.text) for f in draft.facts))
        if draft.inferences:
            reads = " ".join(_sentence(i.text) for i in draft.inferences)
            parts.append(f"Reading into that: {reads}")
        if draft.unknowns:
            parts.append("What I do not have: " + "; ".join(u.strip().rstrip('.') for u in draft.unknowns) + ".")
        if not parts:
            return "I do not have anything on that in front of me right now. Want me to go and fetch it?"
        return " ".join(p for p in parts if p).strip()


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return text if text[-1] in ".!?" else text + "."
