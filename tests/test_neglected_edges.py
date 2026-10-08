"""Tests for previously-neglected edge cases and failure modes, the 'small things that break':
liveness presentation robustness, DDNS non-interactive paths, and graceful degradation on corrupt
state. These exercise behaviours no other test touched."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    for k in [k for k in os.environ if k.startswith("DDNS_")]:
        os.environ.pop(k)
    from mongoose.db import init_db
    init_db()
    return d


# ---- liveness: the trust surface must never crash and must read sensibly ----

def test_human_age_boundaries():
    from mongoose.liveness import _human_age
    assert _human_age(None) == "never"
    assert _human_age(0.5) == "30 min ago"
    assert _human_age(0.99) == "59 min ago"
    assert _human_age(1.0) == "1h ago"
    assert _human_age(47.9) == "47h ago"
    assert _human_age(48.0) == "2 days ago"
    assert _human_age(168) == "7 days ago"


def test_human_age_clamps_future_timestamp():
    """A future timestamp (clock skew) must not render as negative time."""
    from mongoose.liveness import _human_age
    assert _human_age(-2.0) == "just now"
    assert _human_age(-0.5) == "just now"


def test_age_hours_handles_malformed_and_naive():
    from mongoose.liveness import _age_hours
    assert _age_hours(None) is None
    assert _age_hours("") is None
    assert _age_hours("not a date") is None
    # naive timestamp assumed UTC
    naive = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(tzinfo=None).isoformat()
    age = _age_hours(naive)
    assert age is not None and 4.5 < age < 5.5
    # Z-suffixed
    z = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    assert 2.5 < _age_hours(z) < 3.5


def test_format_liveness_survives_malformed_checks():
    """One malformed check dict must not crash the whole health report."""
    from mongoose.liveness import format_liveness
    # missing 'state' and 'detail' entirely , previously raised KeyError
    out = format_liveness({"headline": "x", "checks": [{"subsystem": "email"}]})
    assert "email" in out
    # a totally empty check
    out2 = format_liveness({"checks": [{}]})
    assert isinstance(out2, str)
    # missing checks key
    assert isinstance(format_liveness({}), str)


def test_format_liveness_renders_fix_hints():
    from mongoose.liveness import format_liveness, STALE
    out = format_liveness({"headline": "h", "checks": [
        {"subsystem": "email", "state": STALE, "detail": "stale", "fix": "re-auth Google"}]})
    assert "re-auth Google" in out


# ---- DDNS non-interactive paths (the way `mongoose ddns setup --provider X` runs) ----

def test_setup_ddns_unknown_provider_fails_cleanly():
    _fresh()
    from mongoose.setup_ddns import setup_ddns
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = setup_ddns(provider="notareal", yes=True)
    assert rc != 0
    out = buf.getvalue().lower()
    assert "unknown provider" in out
    assert "duckdns" in out          # lists valid options


def test_duckdns_is_a_known_provider():
    """The user uses DuckDNS , it must resolve as a first-class provider."""
    _fresh()
    from mongoose.ddns import provider_by_slug
    p = provider_by_slug("duckdns")
    assert p is not None
    assert "DDNS_DUCKDNS_TOKEN" in p.config_keys
    assert "DDNS_DUCKDNS_DOMAIN" in p.config_keys


# ---- graceful degradation on corrupt persisted state ----

def test_install_progress_survives_corrupt_state():
    _fresh()
    from mongoose.db import set_app_state
    set_app_state("install_progress", "{not valid json")
    from mongoose.install_progress import install_progress
    p = install_progress()           # must not raise
    assert "ready" in p and "steps" in p


def test_forget_pending_corrupt_state_does_not_break_chat():
    """Leftover corrupt forget-pending state must degrade to 'no pending', not crash a chat turn."""
    import asyncio
    _fresh()
    from mongoose.db import set_app_state
    from mongoose.services import memory as M
    M.remember_fact("The user likes truffle.", source="test")
    # corrupt the pending-forget slot for this conversation
    set_app_state("forget_pending:c", "{broken json")
    from mongoose.llm import chat_with_memory
    # a forget selection reply with corrupt pending state must not raise
    r = asyncio.run(chat_with_memory("the first one", conversation_id="c", role="chat"))
    assert isinstance(r.get("reply"), str)
