"""Regression for the phone screenshots: 'Tell a joke' and 'Tell me a joke' were matched by the
deterministic message-SEND patterns (who='a' / who='me'), so the phone replied 'I couldn't find a
guest called a'. The send patterns must only fire on a real message to a named person."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _send_match(text):
    from mongoose.app import _MSG_SEND_PATTERNS
    for p in _MSG_SEND_PATTERNS:
        m = p.match(text)
        if m:
            return m.group("who"), m.group("body")
    return None


def test_chat_phrases_do_not_match_send():
    _fresh()
    for t in ("Tell a joke", "Tell me a joke", "tell us a story", "tell the truth",
              "let me know what you think", "text me later", "message a friend"):
        assert _send_match(t) is None, t


def test_real_messages_match_send():
    _fresh()
    assert _send_match("tell Sam i miss him") == ("Sam", "i miss him")
    assert _send_match("message Sam hello") == ("Sam", "hello")
    assert _send_match("let Sam know dinner is at 8") == ("Sam", "dinner is at 8")
    assert _send_match("tell Alex that I am late") == ("Alex", "I am late")


def test_phone_chat_does_not_hit_mailbox(monkeypatch):
    """End-to-end: a guest typing 'Tell a joke' must NOT get the 'no guest called a' mailbox reply —
    it must fall through to normal chat (handler returns None)."""
    _fresh()
    from mongoose.app import _maybe_handle_message_send
    dev = {"device_id": "abc", "profile": "trusted_guest"}
    # guest path
    assert _maybe_handle_message_send("Tell a joke", is_guest=True, dev=dev) is None
    assert _maybe_handle_message_send("Tell me a joke", is_guest=True, dev=dev) is None
