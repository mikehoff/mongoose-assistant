"""The unified intent resolver: one confidence-based classifier that replaces the first-match regex
chain. This battery pins the behaviour that matters , the command-vs-status distinction, every
intent's canonical phrasing, comms-lookup deferral, and negatives that must fall through to the model.
Skips when the local embeddings model is absent (the resolver degrades to keyword-only there)."""
import pytest

from mongoose.services.intent_resolver import resolve
from mongoose.services.embeddings import embed

pytestmark = pytest.mark.skipif(embed(["ping"]) is None,
                                reason="local embeddings model not present; resolver is keyword-only")


@pytest.mark.parametrize("msg,intent", [
    # The bug that motivated this: a build COMMAND for the RTM product must hit the plugin flow, not
    # the empty build-plan, regardless of exact wording.
    ("build the next RTM iteration", "rtm_plugin"),
    ("build the next iteration of the wordpress plugin", "rtm_plugin"),
    ("improve the example court plugin and ship it", "rtm_plugin"),
    ("make the rtm better and deploy it", "rtm_plugin"),
    # Build STATUS (a query) must stay distinct from the build command.
    ("how is the RTM build going", "rtm_build"),
    ("whats left to build for example court", "rtm_build"),
    # The rest of the intents, canonical phrasings.
    ("how are the arrears looking", "rtm_operational"),
    ("who owes service charge", "rtm_operational"),
    ("what are my compliance deadlines", "rtm_compliance"),
    ("are the deeds verified", "rtm_knowledge"),
    ("set a timer for 10 minutes", "time_alert"),
    ("remind me to call the plumber at 5", "time_alert"),
    ("turn off the lounge lights", "home_control"),
    ("are the lights on", "home_read"),
    ("give me my daily brief", "brief"),
    ("how is my day shaping up", "daily_picture"),
    ("prepare me for the trustee meeting", "prepare"),
    ("surprise me", "suggestion"),
    ("is everything connected", "readiness"),
    ("is everything working", "readiness"),
    ("log my swim", "body_state"),
    # "what can you do" is the capability showcase, kept distinct from the tour.
    ("what can you do", "capabilities"),
    ("what can you actually do for me", "capabilities"),
    ("show me around", "tutorial"),
    ("do something cool", "tutorial"),
    # media requests resolve to home control (HA), not a dead None.
    ("play some jazz", "home_control"),
    ("put on some music", "home_control"),
])
def test_intents_route_by_best_match(msg, intent):
    got, conf, why = resolve(msg)
    assert got == intent, f"{msg!r} -> {got} ({conf}) [{why}], wanted {intent}"


@pytest.mark.parametrize("msg", [
    "whats the weather tomorrow",   # a tool, not a routed intent
    "whats 2 plus 2",
    "thanks thats great",
    "tell me about roman history",
    "draft an email to sam",
    "read me the voicemail from the leaseholders",  # comms lookup -> tool loop
    "who called me today",
    "any messages from sam",
])
def test_non_intents_defer_to_the_model_or_tools(msg):
    got, conf, why = resolve(msg)
    assert got is None, f"{msg!r} wrongly routed to {got} ({conf}) [{why}]"


def test_guest_never_gets_owner_only_intents():
    assert resolve("how are the arrears looking", guest=True)[0] is None
    assert resolve("build the next RTM iteration", guest=True)[0] is None


def test_command_beats_status_by_priority_on_close_scores():
    """The specific tie the first-match chain got wrong: a build command scores high for both
    rtm_plugin and rtm_build; priority must resolve it to the command."""
    got, _, _ = resolve("build the next RTM iteration")
    assert got == "rtm_plugin"
