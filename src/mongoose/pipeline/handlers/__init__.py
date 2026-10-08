"""Trap doors: each a subclass of the sealed pipeline.TurnHandler. They fill in fetch() and compose() only."""
from .benchmark import BenchmarkHandler
from .document import DocumentHandler
from .plain_chat import PlainChatHandler
from .rtm_finance import RtmFinanceHandler
from .self_knowledge import SelfKnowledgeHandler

__all__ = ["BenchmarkHandler", "DocumentHandler", "PlainChatHandler", "RtmFinanceHandler",
           "SelfKnowledgeHandler"]
