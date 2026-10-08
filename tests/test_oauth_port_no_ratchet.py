"""Regression guard for the Google OAuth redirect-port ratchet.

THE BUG (user-reported): every failed sign-in attempt walked the redirect port up , 8766, then 8767,
then 8768 , and each time told the user to register the new number in the Google Cloud Console. The
target never stabilised, so redirect_uri_mismatch was guaranteed forever. Two causes:

  1. On a busy port, the code picked a higher port and PERSISTED it. Since the "registered port" is read
     back from that persisted value, it climbed on every attempt.
  2. The port-free probe set SO_REUSEADDR, so it could bind a TIME_WAIT port and report "free" when the
     real server bind (no SO_REUSEADDR) would then fail , manufacturing a false drift.

The fix makes the registered port a FIXED target the user controls: a busy port is reported as busy with
actionable guidance, and the port is never auto-advanced or auto-persisted. These tests lock that in.

DB isolation is automatic via conftest.
"""
from __future__ import annotations

import socket


def test_registered_port_never_advances_on_busy_attempt(monkeypatch):
    """The core anti-ratchet property: a busy registered port must NOT change the registered port."""
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "8766")
    from mongoose import oauth as O

    before = O.google_oauth_redirect_port()
    assert before == 8766

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", 8766))
        sock.listen(1)

        class FakeFlow:
            def run_local_server(self, **k):
                raise AssertionError("must not start the server while the port is busy")

        try:
            O.run_google_consent(FakeFlow())
        except RuntimeError:
            pass  # expected: clear error

        after = O.google_oauth_redirect_port()
        assert after == before == 8766, "registered OAuth port drifted on a busy attempt (ratchet bug)"
    finally:
        sock.close()


def test_busy_port_error_keeps_same_port_and_is_actionable(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "8766")
    from mongoose import oauth as O

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", 8766))
        sock.listen(1)

        class FakeFlow:
            def run_local_server(self, **k):
                raise OSError(98, "Address already in use")

        try:
            O.run_google_consent(FakeFlow())
            assert False, "should have raised"
        except RuntimeError as e:
            msg = str(e)
            assert "8766" in msg
            # must not tell the user to register a drifted port
            for drift in (8767, 8768, 8769):
                assert f"localhost:{drift}/" not in msg
        except OSError:
            pass
    finally:
        sock.close()


def test_no_drift_helpers_remain():
    """The two functions that implemented the ratchet must be gone, so it cannot be reintroduced."""
    from mongoose import oauth as O
    assert not hasattr(O, "_find_free_port"), "_find_free_port (port-scan drift) must be removed"
    assert not hasattr(O, "_persist_redirect_port"), "_persist_redirect_port (drift persistence) must be removed"


def test_port_probe_agrees_with_real_bind():
    """The free-probe must not use SO_REUSEADDR, so it agrees with the plain server bind and does not
    report a TIME_WAIT/occupied port as free."""
    from mongoose import oauth as O
    sock = socket.socket()
    sock.bind(("127.0.0.1", 8779))
    sock.listen(1)
    try:
        assert O._port_is_free(8779) is False, "probe reported an occupied port as free (SO_REUSEADDR bug)"
    finally:
        sock.close()


def test_describe_port_holder_is_actionable():
    from mongoose import oauth as O
    text = O._describe_port_holder(8766)
    assert isinstance(text, str) and text
    assert ("lsof" in text or "kill" in text or "PID" in text)
