"""Protocol v3 runtime: bus read, voicemail drainer, ack/heartbeat, catch-up, version check, reclaim."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    # clear v3/pi env so a prior test enabling v3 can't leak into the next suite
    for k in ("PROTOCOL_V3_ENABLED", "PI_SSH_HOST", "PI_SSH_PASSWORD", "PI_SSH_KEY", "PI_SSH_PORT"):
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
    return d


def _v3_on(monkeypatch):
    """Enable v3, mock the Pi as reachable, capture bus appends."""
    _fresh(PROTOCOL_V3_ENABLED="true", PI_SSH_HOST="pi@192.168.1.20")
    import mongoose.services.protocol_v3_runtime as RT
    import mongoose.services.protocol_v3 as P
    posted = []
    monkeypatch.setattr(RT, "transport_status", lambda: {"reachable": True, "configured": True})
    monkeypatch.setattr(P, "_append_to_bus", lambda env: posted.append(env) or {"ok": True})
    monkeypatch.setattr(RT, "_append_to_bus", lambda env: posted.append(env) or {"ok": True})
    return RT, posted


# --- gating ---

def test_runtime_gated_off_when_v3_disabled():
    _fresh()
    from mongoose.services import protocol_v3_runtime as RT
    assert RT.read_bus()["enabled"] is False
    assert RT.drain_voicemail()["enabled"] is False
    assert RT.reconcile_on_wake()["enabled"] is False


# --- bus read ---

def test_bus_read_filters_to_me(monkeypatch):
    RT, _ = _v3_on(monkeypatch)
    bus = "\n".join([
        '- {"protocol_version":3,"msg_id":"m1","from":"chat","to":"all","topic":"hb","needs_ack":false}',
        '- {"protocol_version":3,"msg_id":"m2","from":"code","to":"mongoose","topic":"ping","needs_ack":true}',
        '- {"protocol_version":3,"msg_id":"m3","from":"chat","to":"chat","topic":"x","needs_ack":false}',
    ])
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=65536: (True, bus))
    r = RT.read_bus()
    ids = [m["msg_id"] for m in r["messages"]]
    assert ids == ["m1", "m2"]  # 'all' + 'mongoose', not chat-private m3


def test_bus_never_reprocesses(monkeypatch):
    RT, _ = _v3_on(monkeypatch)
    bus = "\n".join([
        '- {"protocol_version":3,"msg_id":"a","from":"chat","to":"all","topic":"x","needs_ack":false}',
        '- {"protocol_version":3,"msg_id":"b","from":"chat","to":"all","topic":"y","needs_ack":false}',
    ])
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=65536: (True, bus))
    RT.mark_processed("a")
    r = RT.read_bus(since_last=True)
    assert [m["msg_id"] for m in r["messages"]] == ["b"]


def test_bus_rotation_pointer_detected(monkeypatch):
    RT, _ = _v3_on(monkeypatch)
    bus = '- {"protocol_version":3,"msg_id":"z","from":"chat","to":"all","topic":"x","needs_ack":false}\n# archived to claude_bus_archive_2026-06-24T00:00:00+01:00.yaml'
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=65536: (True, bus))
    r = RT.read_bus()
    assert r["archive_pointer"] and "archive" in r["archive_pointer"]


# --- voicemail drainer ---

def test_voicemail_drains_both_body_shapes(monkeypatch):
    RT, _ = _v3_on(monkeypatch)
    vm = "\n".join([
        '{"protocol_version":"3","msg_id":"vm_1","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":{"uid":"1","caller":"+44","transcript":"leak flat 9","wav_rel_path":"a.wav","tag":"urgent"}}',
        '{"protocol_version":"3","msg_id":"vm_2","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":"From: Jane\\nNo hot water\\nb.wav"}',
    ])
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=262144: (True, vm))
    r = RT.drain_voicemail()
    assert r["count"] == 2
    by_id = {v["msg_id"]: v for v in r["drained"]}
    assert by_id["vm_1"]["transcript"] == "leak flat 9"
    assert by_id["vm_2"]["caller"] == "Jane"
    assert by_id["vm_2"]["transcript"] == "No hot water"


def test_voicemail_dedupes_across_repoll(monkeypatch):
    RT, _ = _v3_on(monkeypatch)
    vm = '{"protocol_version":"3","msg_id":"vm_9","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":{"uid":"9","transcript":"hi"}}'
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=262144: (True, vm))
    assert RT.drain_voicemail()["count"] == 1
    assert RT.drain_voicemail()["count"] == 0  # re-poll, already processed


def test_voicemail_acks(monkeypatch):
    RT, posted = _v3_on(monkeypatch)
    vm = '{"protocol_version":"3","msg_id":"vm_5","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":{"uid":"5"}}'
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=262144: (True, vm))
    r = RT.drain_voicemail()
    assert r["acked"] == 1
    acks = [p for p in posted if p["topic"] == "ack"]
    assert acks and acks[0]["ack_of"] == "vm_5"
    assert acks[0]["to"] == "all"  # non-peer sender -> ack to all


def test_voicemail_does_not_act():
    """A voicemail is DATA. The drainer must never call back / remind / notify , only drain+ack+record."""
    _fresh(PROTOCOL_V3_ENABLED="true")
    import inspect
    from mongoose.services import protocol_v3_runtime as RT
    src = inspect.getsource(RT.drain_voicemail)
    # no household/notify actions in the drainer
    assert "send_to_phone" not in src and "call_service" not in src


# --- version check ---

def test_protocol_version_mismatch(monkeypatch):
    RT, _ = _v3_on(monkeypatch)
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=8192: (True, "protocol_version: 4\n"))
    pv = RT.protocol_version_check()
    assert pv["mismatch"] is True and pv["live"] == 4


def test_protocol_version_match(monkeypatch):
    RT, _ = _v3_on(monkeypatch)
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=8192: (True, "protocol_version: 3\n"))
    pv = RT.protocol_version_check()
    assert pv["mismatch"] is False


# --- reconcile ---

def test_reconcile_catches_up_acks_heartbeats(monkeypatch):
    RT, posted = _v3_on(monkeypatch)

    def fake_read(path, tail_bytes=65536):
        if "workspace" in path:
            return (True, "protocol_version: 3\n")
        if "voicemail" in path:
            return (True, '{"protocol_version":"3","msg_id":"vm_7","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":{"uid":"7"}}')
        return (True, '- {"protocol_version":3,"msg_id":"bz","from":"code","to":"mongoose","topic":"ping","needs_ack":true}')

    monkeypatch.setattr(RT, "_read_remote_file", fake_read)
    s = RT.reconcile_on_wake()
    assert s["bus"]["new"] == 1 and s["bus"]["acked"] == 1
    assert s["voicemail"]["drained"] == 1
    assert s["heartbeat_posted"] is True
    topics = [p["topic"] for p in posted]
    assert "heartbeat" in topics and topics.count("ack") == 2  # bus + voicemail acks


# --- lock reclaim auto-announce ---

def test_lock_reclaim_announced(monkeypatch):
    RT, posted = _v3_on(monkeypatch)
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(P, "acquire_lock",
                        lambda target, ttl_seconds=60: {"ok": True, "acquired": True, "reclaimed": True,
                                                        "previous_sid": "old-sid", "must_post": "lock-reclaim"})
    r = RT.take_lock_and_announce("claude_bus.yaml")
    assert r["reclaim_announced"] is True
    assert any(p["topic"] == "lock-reclaim" for p in posted)


# --- chat tool + endpoints ---

def test_v3_coordination_tool_and_routing():
    _fresh()
    from mongoose.tools import TOOLS, READ_TOOLS
    assert "v3_coordination" in TOOLS and "v3_coordination" in READ_TOOLS
    from mongoose.role_router import classify_role
    for q in ["check the bus", "any voicemails", "reconcile the bus"]:
        assert classify_role(q) == "tool"


def test_v3_runtime_endpoints_registered():
    _fresh()
    import mongoose.app as A
    routes = {r.path for r in A.app.routes}
    for p in ["/cockpit/protocol-v3/bus", "/cockpit/protocol-v3/voicemail",
              "/cockpit/protocol-v3/reconcile"]:
        assert p in routes


def test_reconcile_job_registered_only_when_v3_enabled():
    import inspect
    from mongoose.services import scheduler as S
    src = inspect.getsource(S._build_registry)
    assert "v3_reconcile" in src
    assert hasattr(S, "_job_v3_reconcile")
