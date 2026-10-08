"""Real-utterance regression net for chat intent routing.

This is the thing the two prior routing "fixes" never had: a table of the owner's ACTUAL phrasings
mapped to the intent that MUST win, run against the live resolver. The long-standing disaster was
structural , whole categories (capture a task, list tasks, mark paid, send a receipt) had NO intent,
so the classifier abstained and a greedy bare-word "rtm" keyword fallback dumped the arrears report.

Each case is a real message shape. If routing regresses, this fails loudly instead of silently
mis-routing in the owner's face. Add a row here before fixing any new mis-route.
"""
from __future__ import annotations

import pytest

from mongoose.services.intent_resolver import resolve

# (utterance, expected_intent). expected None means "defer to the model/tool loop" on purpose.
CASES: list[tuple[str, str | None]] = [
    # ── task capture: the exact disaster from the screenshot and its siblings ──
    ("keep it as an outstanding rtm task", "task_capture"),
    ("keep this as a task", "task_capture"),
    ("keep that as an rtm task", "task_capture"),
    ("add a task to call the surveyor", "task_capture"),
    ("add a task to chase the window cleaning quote", "task_capture"),
    ("make a note to order parking signage", "task_capture"),
    ("note down: renew the buildings insurance", "task_capture"),
    ("jot this down as a to-do", "task_capture"),
    ("new task: respond to the managing agent", "task_capture"),
    # ── task listing ──
    ("what rtm tasks are outstanding", "task_list"),
    ("what are my outstanding tasks", "task_list"),
    ("show me my to-do list", "task_list"),
    ("list my rtm tasks", "task_list"),
    # ── mark paid (a real chat action the owner wants) ──
    ("mark Barnes as paid", "rtm_mark_paid"),
    ("record 1ECF3 as paid", "rtm_mark_paid"),
    ("mark flat 3ECF9 paid", "rtm_mark_paid"),
    ("log Petrov's payment as received", "rtm_mark_paid"),
    # ── send a receipt (send an EXISTING receipt, distinct from generating one) ──
    ("send martins receipt", "rtm_send_receipt"),
    ("email Barnes his receipt", "rtm_send_receipt"),
    ("resend the receipt to 1ECF3", "rtm_send_receipt"),
    # ── operational QUERIES must still route to the money view (no regression) ──
    ("how are the arrears looking", "rtm_operational"),
    ("who owes money", "rtm_operational"),
    ("what's the rtm money position", "rtm_operational"),
    ("what service charges are overdue", "rtm_operational"),
    ("how much is owed on the flats", "rtm_operational"),
    # ── document GENERATION stays with rtm_document (create, not send) ──
    ("generate a receipt for Petrov", "rtm_document"),
    ("create the welcome letter for flat 8", "rtm_document"),
    # ── build stays with rtm_plugin (no regression) ──
    ("build the next rtm plugin iteration", "rtm_plugin"),
    # ── timed reminders still go to time_alert (handler captures untimed ones) ──
    ("remind me about the insurance renewal at 9am tomorrow", "time_alert"),
    ("set an alarm for 7am", "time_alert"),
]


@pytest.mark.parametrize("utterance,expected", CASES)
def test_intent_routes_correctly(utterance: str, expected: str | None) -> None:
    got, score, why = resolve(utterance)
    assert got == expected, f"{utterance!r} -> {got!r} ({score}, {why}); expected {expected!r}"


def test_no_bare_rtm_noun_triggers_operational() -> None:
    """The core disease: a domain noun with no state/query signal must NOT dump arrears."""
    from mongoose.role_router import looks_like_rtm_operational
    for benign in ["keep it as an rtm task", "add an rtm note", "the rtm is great",
                   "what rtm tasks are outstanding"]:
        assert not looks_like_rtm_operational(benign), f"bare-noun {benign!r} still trips operational"
