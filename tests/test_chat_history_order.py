"""G22: the phone chat thread reads oldest-first.

The launchpad now renders /device/chat/history in the order it arrives (it no longer
reverses). That is only correct because the history is returned oldest-first. If the
backend order ever flips to newest-first, the phone thread would silently show in reverse
again, which is the exact bug a real-device run hit. This pins the invariant.
"""
from mongoose.services.memory import (
    add_message, recent_messages, ensure_conversation, DEFAULT_CONVERSATION_ID,
)


def test_recent_messages_returns_oldest_first():
    conv = DEFAULT_CONVERSATION_ID
    ensure_conversation(conv)
    add_message(conv, "user", "alpha")
    add_message(conv, "assistant", "bravo")
    add_message(conv, "user", "charlie")
    contents = [m["content"] for m in recent_messages(conv, limit=10)]
    # oldest first: the order the phone appends and scrolls-to-bottom expects
    assert contents == ["alpha", "bravo", "charlie"], contents
