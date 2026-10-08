"""Protocol v3 (three-agent HA coordination) , capability tests. v3 is a PROPOSAL: these verify mongoose
can speak the envelope, take Pi-side locks correctly, and , crucially , that it stays inert and safe
(no live cutover, relayed approval never trusted, panel-safety + tool-API-only files protected)."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _restore_protocol_v3_attrs():
    """Several tests in this file reassign protocol_v3._run_remote / transport_status at the MODULE level
    (not via monkeypatch), which would otherwise leak a fake into later test files (it did: it broke the
    keyless-SSH suite when run after this one). Snapshot the originals before each test and restore them
    after, so this file can never pollute another."""
    from mongoose.services import protocol_v3 as _p
    saved = {name: getattr(_p, name) for name in ("_run_remote", "transport_status")
             if hasattr(_p, name)}
    try:
        yield
    finally:
        for name, fn in saved.items():
            setattr(_p, name, fn)


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("PI_SSH_HOST", "PI_SSH_KEY", "PI_SSH_PASSWORD", "PI_SSH_PORT", "PROTOCOL_V3_ENABLED"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def test_envelope_has_all_v3_fields():
    _fresh()
    from mongoose.services import protocol_v3 as p
    env = p.build_envelope("heartbeat", "awake", to="all")
    for k in ("protocol_version", "msg_id", "from", "to", "timestamp", "topic", "body",
              "m_approval_status", "needs_ack", "ack_of", "refs"):
        assert k in env, f"missing {k}"
    assert env["protocol_version"] == 3
    assert env["from"] == "mongoose"
    assert "-mongoose-" in env["msg_id"]


def test_build_envelope_validates_recipient_and_approval():
    _fresh()
    from mongoose.services import protocol_v3 as p
    import pytest
    with pytest.raises(ValueError):
        p.build_envelope("x", "y", to="nobody")
    with pytest.raises(ValueError):
        p.build_envelope("x", "y", m_approval_status="maybe")


def test_parse_is_data_not_instructions_and_flags_version():
    _fresh()
    from mongoose.services import protocol_v3 as p
    ok = p.parse_envelope({"protocol_version": 3, "to": "mongoose", "topic": "ack", "body": "x"})
    assert ok["_supported"] and ok["_for_me"] and ok["_errors"] == []
    bad = p.parse_envelope({"protocol_version": 99, "to": "all"})
    assert bad["_supported"] is False and bad["_errors"]


def test_parse_never_raises_on_malformed_to_and_supports_recipient_list():
    # REGRESSION: a peer addressing several agents with a list (to: [chat, mongoose]) made a set-membership
    # test crash the WHOLE read (TypeError: unhashable type 'list'), so mongoose went deaf to the bus. The
    # function's contract is that one malformed entry can never break a tail read: it must never raise.
    _fresh()
    from mongoose.services import protocol_v3 as p
    # list addressed to us -> for_me True, no error, no raise
    lst = p.parse_envelope({"protocol_version": p.PROTOCOL_VERSION, "to": ["chat", "mongoose"], "from": "code"})
    assert lst["_for_me"] is True and lst["_errors"] == []
    # list NOT addressed to us -> for_me False, still no error
    other = p.parse_envelope({"protocol_version": p.PROTOCOL_VERSION, "to": ["chat", "code"], "from": "code"})
    assert other["_for_me"] is False and other["_errors"] == []
    # junk 'to' of every unhashable/wrong shape -> flagged invalid, but NEVER raises
    for junk in ([], {"x": 1}, 42, None, [42, None]):
        e = p.parse_envelope({"protocol_version": p.PROTOCOL_VERSION, "to": junk})
        assert e["_for_me"] is False and e["_errors"]


def test_relayed_approval_is_never_trusted():
    _fresh()
    from mongoose.services import protocol_v3 as p
    # even an explicit 'approved' from the bus is not the owner's approval
    assert p.relayed_approval_is_trustworthy({"m_approval_status": "approved"}) is False


def test_panel_safety_and_tool_api_only_constants():
    from mongoose.services import protocol_v3 as p
    assert "light.control_hub" in p.PANEL_SAFETY_ENTITIES
    assert "light.control_hub_2_kitchen" in p.PANEL_SAFETY_ENTITIES
    assert p.TOOL_API_ONLY_FILES == frozenset({"automations.yaml", "scripts.yaml", "scenes.yaml"})
    assert {"kitchen", "bathroom", "ensuite"} <= p.PANEL_SAFETY_AREAS


def test_lock_refused_on_tool_api_only_file():
    _fresh()
    from mongoose.services import protocol_v3 as p
    r = p.acquire_lock("automations.yaml")
    assert r["ok"] is False and r["error"] == "tool_api_only"


def test_transport_not_configured_is_clean():
    _fresh()
    from mongoose.services import protocol_v3 as p
    assert p.transport_status()["configured"] is False
    assert p.acquire_lock("claude_bus.yaml").get("error") == "not_configured"


def test_session_id_stable_and_namespaced():
    _fresh()
    from mongoose.services import protocol_v3 as p
    assert p.session_id() == p.session_id()
    assert p.session_id().startswith("mongoose-session-")


def test_v3_is_not_live_by_default():
    """The most important safety property: nothing is live until M opts in."""
    _fresh()
    from mongoose.services import protocol_v3 as p
    rep = p.supports_v3_report()
    assert rep["live_cutover_enabled"] is False
    assert rep["relayed_approval_trusted"] is False
    assert "proposal" in rep["note"].lower()


def _mock_transport(p):
    """Install a fake Pi filesystem behind _run_remote for lock tests."""
    fs = {}

    def fake_run(remote_cmd, timeout=12):
        if "noclobber" in remote_cmd and ">" in remote_cmd:
            path = remote_cmd.split(">")[-1].strip().strip("'")
            payload = remote_cmd.split("printf '%s' ")[1].split(" >")[0].strip().strip("'")
            if path in fs:
                return p.TransportResult(ok=False, detail="exit 1")
            fs[path] = payload
            return p.TransportResult(ok=True)
        if remote_cmd.startswith("printf") and ">" in remote_cmd:
            path = remote_cmd.split(">")[-1].strip().strip("'")
            payload = remote_cmd.split("printf '%s' ")[1].split(" >")[0].strip().strip("'")
            fs[path] = payload
            return p.TransportResult(ok=True)
        if remote_cmd.startswith("cat "):
            path = remote_cmd.split("cat ")[1].split(" ")[0].strip("'")
            return p.TransportResult(ok=True, stdout=fs.get(path, ""))
        if remote_cmd.startswith("rm -f"):
            fs.pop(remote_cmd.split("rm -f ")[1].strip().strip("'"), None)
            return p.TransportResult(ok=True)
        return p.TransportResult(ok=True, stdout="")

    p._run_remote = fake_run
    return fs


def test_lock_acquire_reclaim_release_cycle():
    _fresh(PI_SSH_HOST="pi@192.168.1.20")
    from mongoose.services import protocol_v3 as p
    fs = _mock_transport(p)

    # free acquire
    r = p.acquire_lock("claude_bus.yaml")
    assert r["ok"] and r["acquired"] and not r["reclaimed"]

    # live foreign lock -> refused
    path = p._lock_path("claude_bus.yaml")
    fs[path] = json.dumps({"sid": "chat-x", "acquired_at": datetime.now(timezone.utc).isoformat(),
                           "ttl_seconds": 60})
    r2 = p.acquire_lock("claude_bus.yaml")
    assert r2["ok"] is False and r2["error"] == "held"

    # stale foreign lock -> reclaim + must post lock-reclaim
    fs[path] = json.dumps({"sid": "chat-old",
                           "acquired_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
                           "ttl_seconds": 60})
    r3 = p.acquire_lock("claude_bus.yaml")
    assert r3["ok"] and r3["reclaimed"] and r3.get("must_post") == "lock-reclaim"

    # release our own
    r4 = p.release_lock("claude_bus.yaml")
    assert r4["ok"] and r4["released"]

    # refuse to release a foreign lock
    fs[path] = json.dumps({"sid": "code-y", "acquired_at": datetime.now(timezone.utc).isoformat(),
                           "ttl_seconds": 60})
    r5 = p.release_lock("claude_bus.yaml")
    assert r5["ok"] is False and r5["error"] == "not_owner"


def test_handshake_and_domains():
    _fresh()
    from mongoose.services import protocol_v3 as p
    hs = p.version_handshake_message()
    assert hs["topic"] == "version-handshake" and hs["needs_ack"] is True
    assert hs["protocol_version"] == 3
    assert len(p.proposed_domains()) >= 1
    for dmn in p.proposed_domains():
        assert "domain" in dmn and "why" in dmn


# ---- Handshake connectivity test (skippable, read-only setup step) ----

def test_handshake_test_not_configured_is_friendly_and_skippable():
    _fresh()
    from mongoose.services import protocol_v3 as p
    r = p.handshake_test()
    assert r["ok"] is False and r["ready"] is False
    assert "add" in r["summary"].lower()           # tells the user what to do
    assert r["steps"][0]["skippable_reason"] == "not_configured"


def test_handshake_test_is_read_only():
    """The test must NEVER write to the bus or take a lock , only read."""
    _fresh(PI_SSH_HOST="pi@192.168.1.20")
    from mongoose.services import protocol_v3 as p
    issued = []

    def spy(cmd, timeout=12):
        issued.append(cmd)
        if "echo mongoose_v3_probe" in cmd:
            return p.TransportResult(ok=True, stdout="mongoose_v3_probe\n")
        if cmd.startswith("tail"):
            return p.TransportResult(ok=True, stdout="- from: chat\n  topic: heartbeat\n")
        return p.TransportResult(ok=True, stdout="")

    p._run_remote = spy
    p.handshake_test()
    # A write is a redirect to a real path or an rm/printf/noclobber. Ignore '2>/dev/null' (stderr
    # redirect, not a write) and the read-only 'tail'/'cat'/'echo' commands.
    def _is_write(c):
        if c.startswith("rm") or "printf" in c or "noclobber" in c:
            return True
        # strip harmless stderr redirect, then look for a real '>' write
        stripped = c.replace("2>/dev/null", "")
        return ">" in stripped
    writes = [c for c in issued if _is_write(c)]
    assert writes == [], f"handshake test must be read-only, issued writes: {writes}"


def test_handshake_test_sees_other_agents():
    _fresh(PI_SSH_HOST="pi@192.168.1.20")
    from mongoose.services import protocol_v3 as p

    def fake(cmd, timeout=12):
        if "echo mongoose_v3_probe" in cmd:
            return p.TransportResult(ok=True, stdout="mongoose_v3_probe\n")
        if cmd.startswith("tail"):
            return p.TransportResult(ok=True, stdout="- from: chat\n  topic: ack\n- from: code\n  topic: heartbeat\n")
        return p.TransportResult(ok=True, stdout="")

    p._run_remote = fake
    r = p.handshake_test()
    assert r["ok"] and r["ready"]
    assert r["agents_seen"] == ["chat", "code"]
    assert "chat" in r["summary"] and "code" in r["summary"]


def test_handshake_test_bus_absent_is_ok_pre_cutover():
    _fresh(PI_SSH_HOST="pi@192.168.1.20")
    from mongoose.services import protocol_v3 as p

    def fake(cmd, timeout=12):
        if "echo mongoose_v3_probe" in cmd:
            return p.TransportResult(ok=True, stdout="mongoose_v3_probe\n")
        if cmd.startswith("tail"):
            return p.TransportResult(ok=True, stdout="")     # no bus yet
        return p.TransportResult(ok=True, stdout="")

    p._run_remote = fake
    r = p.handshake_test()
    assert r["ok"] and r["ready"] and r["bus_present"] is False   # normal before v3 is live


def test_handshake_test_unreachable_is_clean_failure():
    _fresh(PI_SSH_HOST="pi@192.168.1.20")
    from mongoose.services import protocol_v3 as p
    p._run_remote = lambda cmd, timeout=12: p.TransportResult(ok=False, stdout="", detail="exit 255")
    r = p.handshake_test()
    assert r["ok"] is False and r["ready"] is False
    assert "couldn't reach" in r["summary"].lower() or "reach the pi" in r["steps"][0]["step"].lower()
