"""The embeddings-based capability router: reworded capability requests that the keyword detectors
miss must still route to the right capability, and ordinary chatter must NOT be dragged into any
capability handler. Skips cleanly when the local embeddings model is absent (CI without the model),
because in that state the whole layer degrades to pure-keyword by contract."""
import pytest

from mongoose.services import semantic_router as sr
from mongoose.services.embeddings import embed


def _embeddings_available() -> bool:
    return embed(["ping"]) is not None


pytestmark = pytest.mark.skipif(not _embeddings_available(),
                                reason="local embeddings model not present; router degrades to keyword-only")


@pytest.mark.parametrize("msg,intent", [
    ("whats the arrears situation", "rtm_operational"),
    ("how much money is the block owed", "rtm_operational"),
    ("anyone not paid their service charge", "rtm_operational"),
    ("hows the rtm site coming along", "rtm_build"),
    ("is everything up and running", "readiness"),
    ("are your connectors ok", "readiness"),
    ("what can i actually do with you", "capabilities"),
    ("what are you capable of", "capabilities"),
    ("how did the benchmarks go", "benchmark"),
])
def test_reworded_capability_requests_route(msg, intent):
    got, score = sr.classify(msg)
    assert got == intent, f"{msg!r} -> {got} ({score}), wanted {intent}"


@pytest.mark.parametrize("msg", [
    "whats the weather tomorrow",
    "tell me a joke",
    "remind me to call mum at 6",
    "draft an email to jordan about dinner",
    "play some jazz",
    "who won the football",
    "set a timer for ten minutes",
])
def test_ordinary_chatter_does_not_false_route(msg):
    got, score = sr.classify(msg)
    assert got is None, f"{msg!r} wrongly routed to {got} ({score})"


def test_capabilities_and_readiness_stay_separated():
    """Users found it unhelpful that 'what can I do with you' returned a dry health report. The two must not
    steal each other's messages."""
    assert sr.classify("what can I do with you")[0] == "capabilities"
    assert sr.classify("is anything broken")[0] == "readiness"


def test_resolver_owns_semantics_detectors_are_pure_keyword():
    """After the routing re-architecture, the unified intent_resolver owns semantics and the
    role_router detectors are pure keyword (the resolver-gated chain uses them only as the
    None-abstain fallback). So the REWORDINGS must be caught by the resolver, and a capability
    question must not be misclassified as readiness (the original bug)."""
    from mongoose.services.intent_resolver import resolve
    assert resolve("how much money is the block owed")[0] == "rtm_operational"
    assert resolve("are your connectors ok")[0] == "readiness"
    assert resolve("what can i actually do with you")[0] != "readiness"
