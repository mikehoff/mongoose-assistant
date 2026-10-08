"""A resent chat message must never print mongoose's reply twice.

Real bug: the phone queued a message whose send appeared to fail, then flushOutbox re-POSTed it with
the same client_msg_id. The server correctly answered `duplicate: True` (one stored row, no second
turn), but the client called addBubble('assistant', d.reply) regardless, so the identical reply
appeared twice a minute apart. It then STAYED on screen because loadHistory skips its rebuild when
the server history is unchanged, so nothing ever cleaned the spurious bubble up.
"""
import re

from mongoose import cockpit


def _flush_outbox_source() -> str:
    """The flushOutbox function body as served to the phone."""
    src = cockpit.DEVICE_LAUNCHPAD_HTML
    i = src.index("async function flushOutbox")
    return src[i:i + 2200]


def test_flush_outbox_honours_the_server_duplicate_flag():
    body = _flush_outbox_source()
    assert "d.duplicate" in body, (
        "flushOutbox must check the server's duplicate flag; without it a resend of an "
        "already-answered message draws a second identical reply bubble")


def test_duplicate_branch_does_not_add_an_assistant_bubble():
    """The duplicate branch must continue BEFORE any addBubble('assistant', ...) call."""
    body = _flush_outbox_source()
    dup = body.index("d.duplicate")
    branch = body[dup:body.index("continue", dup)]
    assert "addBubble('assistant'" not in branch, (
        "the duplicate path must not draw a reply bubble; the reply is already on screen and "
        "already persisted server-side")


def test_duplicate_branch_forces_a_history_refresh():
    """Clearing _histSig is what lets the canonical server copy re-render (the poll otherwise
    short-circuits on an unchanged signature and a stray bubble would persist)."""
    body = _flush_outbox_source()
    dup = body.index("d.duplicate")
    branch = body[dup:body.index("continue", dup)]
    assert "_histSig" in branch and "loadHistory" in branch


def test_server_still_reports_duplicate_for_a_resent_chat_message():
    """The client guard is worthless if the server stops flagging duplicates."""
    import pathlib
    app_src = pathlib.Path(cockpit.__file__).with_name("app.py").read_text(encoding="utf-8")
    assert re.search(r'"duplicate":\s*True', app_src), \
        "/device/chat must keep flagging a resent client_msg_id as duplicate"
