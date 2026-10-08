"""Anti-regression guard for the 'silent env default' bug class.

The recurring failure signature: a default value that is quietly wrong and only bites at the edge (an
iPhone that never gets a push, a server that binds the wrong interface, a model outage that returns
bland silence). Each is cheap to fix once found and expensive to rediscover. These tests pin the safe,
LOUD behaviour so it can never silently drift back. See docs and the VAPID .local incident.
"""
import inspect
import re


def test_vapid_default_subject_is_never_dot_local():
    # Apple's push service 403s a mailto at a .local (or any non-routable) domain, so every iPhone
    # push silently failed while Android worked. The default must be a routable mailto:/https: sub.
    from mongoose.services.web_push import _default_vapid_subject
    sub = _default_vapid_subject()
    assert not sub.endswith(".local"), f"VAPID default fell back to a .local subject: {sub!r}"
    assert sub.startswith(("mailto:", "https:")), f"VAPID subject not a valid sub: {sub!r}"


def test_mongoose_host_default_is_loopback():
    # A wrong bind default is a security + reachability footgun. The default when MONGOOSE_HOST is
    # unset must be loopback, never 0.0.0.0 by accident.
    src = inspect.getsource(inspect.getmodule(
        __import__("mongoose.access_urls", fromlist=["x"])))
    m = re.search(r'os\.getenv\(\s*["\']MONGOOSE_HOST["\']\s*,\s*["\']([^"\']+)["\']', src)
    assert m, "MONGOOSE_HOST default not found in access_urls (did the resolver move?)"
    assert m.group(1) == "127.0.0.1", f"MONGOOSE_HOST default is {m.group(1)!r}, expected loopback"


def test_local_model_failure_is_typed_not_silent():
    # A model outage must raise a typed LocalModelError with an actionable message, never return an
    # empty string that reads to the user as Mongoose ignoring them.
    from mongoose import llm
    assert issubclass(llm.LocalModelError, Exception)
    src = inspect.getsource(llm.ask_ollama)
    # the unreachable/timeout branch must raise, and must not swallow into bland silence
    assert "raise LocalModelError" in src, "ask_ollama no longer raises a typed model error"
    connect_branch = src[src.index("asyncio.TimeoutError"):]
    assert "raise LocalModelError" in connect_branch, "connect/timeout path stopped raising loudly"
    assert 'return ""' not in connect_branch and "return None" not in connect_branch, \
        "ask_ollama swallows a model outage into silence instead of raising"
