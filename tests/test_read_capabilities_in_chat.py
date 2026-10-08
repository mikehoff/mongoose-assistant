"""Read-only capabilities that were CLI-only now run in chat: daily brief, daily operating picture,
prepare-me card, on-demand suggestion, RTM statutory compliance. They must route to their own intent
(not collide with rtm_operational/home), and execute owner-only through the same service the CLI uses."""
import asyncio

import pytest

from mongoose.role_router import looks_like_read_capability, looks_like_rtm_operational


@pytest.mark.parametrize("msg,intent", [
    ("give me my daily brief", "brief"),
    ("brief me on today", "brief"),
    ("how is my day shaping up", "daily_picture"),
    ("prepare me for the trustee meeting", "prepare"),
    ("surprise me", "suggestion"),
    ("any suggestions for me", "suggestion"),
    ("what are my compliance deadlines", "rtm_compliance"),
    ("when is the confirmation statement due", "rtm_compliance"),
])
def test_read_capability_phrasings_route(msg, intent):
    assert looks_like_read_capability(msg) == intent, (msg, looks_like_read_capability(msg))


@pytest.mark.parametrize("msg", [
    "how are the arrears looking",   # rtm_operational owns this, not a read-cap
    "turn the lounge lights off",
    "whats the weather",
    "remind me to call the plumber",
])
def test_ordinary_and_other_intents_are_not_stolen(msg):
    assert looks_like_read_capability(msg) is None, msg


def test_arrears_still_belongs_to_rtm_operational():
    assert looks_like_rtm_operational("how are the arrears looking") is True
    assert looks_like_read_capability("how are the arrears looking") is None


def test_rtm_compliance_executes_in_chat_owner_only():
    """A fast, model-free intent proves the chat wiring runs the real service and tags the source.
    Guests must NOT reach it (owner-only read capability)."""
    from mongoose.llm import chat_with_memory

    async def ask(gm):
        return await chat_with_memory("what are my compliance deadlines", conversation_id="tw-rc-test",
                                      role="chat", guest_mode=gm, skip_user_persist=True)

    owner = asyncio.run(ask(False))
    assert owner.get("source") == "rtm_compliance"
    assert owner["reply"]  # real text from rtm_secretary
    # Guest must not get the owner compliance report from this path.
    guest = asyncio.run(ask(True))
    assert guest.get("source") != "rtm_compliance"
