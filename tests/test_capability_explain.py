"""The capability explainer answers 'can you do X' from the LIVE registry, positive-only, chat-native
only. Structural guarantees hold regardless of the embeddings model; match-quality assertions need
it, so they skip when it is absent."""
import asyncio

import pytest

from mongoose.services.capability_index import explain
from mongoose.services.embeddings import embed

_HAS_MODEL = embed(["ping"]) is not None


def test_guest_never_gets_the_owner_capability_explainer():
    assert explain("can you read my whatsapp messages", is_guest=True) is None


def test_out_of_scope_and_weak_matches_return_none_never_a_false_yes():
    for q in ["can you order a pizza", "can you pay my rent", "whats the weather"]:
        assert explain(q) is None, q


def test_routes_and_cli_are_never_surfaced_to_a_chat_user():
    """A user asking a capability question must never be answered with an HTTP route or a CLI
    command. Whatever explain returns, it must not contain those internal surfaces."""
    for q in ["how do i invite a guest", "can you read my whatsapp messages", "do you track bugs"]:
        ans = explain(q) or ""
        assert "GET /" not in ans and "POST /" not in ans and "mongoose " not in ans, (q, ans)


@pytest.mark.skipif(not _HAS_MODEL, reason="embeddings model absent; semantic recall degraded")
@pytest.mark.parametrize("q", [
    "can you read my whatsapp messages",
    "how do i invite a guest",
    "do you track bugs",
    "can you set me a reminder",
])
def test_real_capabilities_are_recognised(q):
    ans = explain(q)
    assert ans and ans.lower().startswith("yes"), (q, ans)


@pytest.mark.skipif(not _HAS_MODEL, reason="embeddings model absent; connector match degraded")
@pytest.mark.parametrize("q,product", [
    ("do you support outlook", "Outlook"),
    ("can you do gmail", "Gmail"),
    ("do you support whatsapp", "WhatsApp"),
    ("can you read pdfs", "PDF"),
])
def test_connector_questions_get_a_truthful_availability_answer(q, product):
    ans = explain(q)
    assert ans and ans.lower().startswith("yes") and product in ans, (q, ans)


def test_generic_message_words_do_not_hijack_a_connector():
    """'check my unread messages' is a tool question, not 'do you support WhatsApp'. A connector must
    only match when the product is NAMED."""
    for q in ["can you check my unread messages", "can you read my direct messages"]:
        ans = explain(q) or ""
        assert "not connected yet" not in ans and "Setup tab" not in ans, (q, ans)


def test_guest_gets_guest_safe_answers_and_never_owner_capabilities():
    # A guest asking about an owner-only capability must get nothing (no leak), not a connector or tool.
    # "check the owner emails" once false-matched the bug-report capability at 0.466; the raised guest
    # threshold rejects it.
    for q in ["can you read my whatsapp messages", "can you show me the arrears", "do you support outlook",
              "can you check the owner emails for me"]:
        assert explain(q, is_guest=True) is None, q


def test_guest_persona_carries_the_no_owner_access_boundary():
    """The system prompt for a guest chat must end with the boundary that forbids claiming owner
    access, otherwise the model free-chats its way into 'I can read your WhatsApp'."""
    from mongoose.persona import guest_persona_card, GUEST_BOUNDARY, persona_card
    card = guest_persona_card()
    assert GUEST_BOUNDARY in card
    assert card.endswith(GUEST_BOUNDARY)  # last word wins
    assert "private" in GUEST_BOUNDARY and "owner" in GUEST_BOUNDARY and "cannot" in GUEST_BOUNDARY
    assert GUEST_BOUNDARY not in persona_card()  # owner persona is unchanged


@pytest.mark.skipif(not _HAS_MODEL, reason="embeddings model absent; guest match degraded")
@pytest.mark.parametrize("q", [
    "can you help me plan my day",
    "can you look something up for me",
    "can you help me focus",
])
def test_guest_safe_capabilities_are_recognised(q):
    ans = explain(q, is_guest=True)
    assert ans and ans.lower().startswith("yes"), (q, ans)


@pytest.mark.skipif(not _HAS_MODEL, reason="embeddings model absent")
def test_chat_routes_capability_question_without_hijacking_specific_handlers():
    from mongoose.llm import chat_with_memory

    async def ask(q):
        return await chat_with_memory(q, conversation_id="tw-capexp", role="chat", skip_user_persist=True)

    cap = asyncio.run(ask("do you track bugs"))
    assert cap.get("source") == "capability_explain"
    # A specific handler must still win over the generic explainer.
    rtm = asyncio.run(ask("how are the arrears looking"))
    assert rtm.get("source") == "rtm_operational"
