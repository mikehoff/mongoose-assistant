"""Mongoose turn pipeline: one sealed route, trap doors as subclasses, fabrication blocked by inheritance.

See docs/ARCHITECTURE_REENGINEERING.md. This package is being built behind a flag and is not yet wired to
any live surface; the base skeleton and its grounding guarantee are unit-tested in isolation first.
"""
from .base import (
    Draft,
    Evidence,
    Fact,
    Inference,
    Source,
    TurnHandler,
    TurnRequest,
    TurnResult,
)
from .grounding import enforce
from .router import TurnRouter
from .trace import TurnTrace

__all__ = [
    "Draft",
    "Evidence",
    "Fact",
    "Inference",
    "Source",
    "TurnHandler",
    "TurnRequest",
    "TurnResult",
    "TurnRouter",
    "TurnTrace",
    "enforce",
]
