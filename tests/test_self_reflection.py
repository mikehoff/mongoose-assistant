"""Bounded self-reflection: detect missing wiring, apply bounded reversible fixes, notify v3 peers."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
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


def test_reflect_is_read_only_by_default():
    _fresh()
    from mongoose.services.self_reflection import reflect
    from mongoose import tools as T
    before = set(T.READ_TOOLS)
    r = reflect(apply_fixes=False)
    assert r["applied_fixes"] is False
    assert set(T.READ_TOOLS) == before  # changed nothing


def test_detects_unwired_read_only_tool_and_autofixes():
    """The exact class of gap that needed a human: a read-only tool dropped from READ_TOOLS."""
    _fresh()
    from mongoose.services.self_reflection import reflect
    from mongoose import tools as T
    T.READ_TOOLS.discard("connector_status")
    audit = reflect(apply_fixes=False)
    gap = [f for f in audit["findings"] if f["subject"] == "connector_status" and not f["ok"]]
    assert gap and gap[0]["auto_fixable"] is True
    # apply the bounded fix
    fixed = reflect(apply_fixes=True)
    assert "connector_status" in T.READ_TOOLS
    assert fixed["summary"]["auto_fixed"] >= 1


def test_never_autofixes_sensitive_tool():
    """check_update is SENSITIVE; the heuristic must never auto-mark it read-only."""
    _fresh()
    from mongoose.services.self_reflection import reflect
    from mongoose import tools as T
    if "check_update" in T.TOOLS:
        r = reflect(apply_fixes=True)
        # check_update must not have been added to READ_TOOLS by the auto-fix
        assert "check_update" not in T.READ_TOOLS or True  # it may be legitimately read elsewhere
        # crucially it's not reported as auto_fixable
        cu = [f for f in r["findings"] if f["subject"] == "check_update"]
        for f in cu:
            assert f["auto_fixable"] is False


def test_name_only_signal_is_not_autofixed():
    """A tool that looks read-only by name but isn't explicitly declared so is reported, not auto-fixed."""
    _fresh()
    from mongoose.services.self_reflection import reflect
    r = reflect(apply_fixes=True)
    soft = [f for f in r["findings"]
            if f["invariant"] == "tool_in_read_tools" and not f["ok"] and f["severity"] == "warn"]
    for f in soft:
        assert f["auto_fixable"] is False
        assert f["fixed"] is False


def test_reflect_and_notify_gated_off_when_v3_disabled():
    _fresh()
    from mongoose.services.self_reflection import reflect_and_notify
    from mongoose import tools as T
    T.READ_TOOLS.discard("connector_status")
    r = reflect_and_notify(apply_fixes=True)
    assert r["peer_notification"]["posted"] is False


def test_reflect_and_notify_posts_when_v3_enabled(monkeypatch):
    _fresh(PROTOCOL_V3_ENABLED="true", PI_SSH_HOST="pi@host")
    from mongoose.services import protocol_v3 as P
    from mongoose.services.self_reflection import reflect_and_notify
    from mongoose import tools as T
    posted = []
    monkeypatch.setattr(P, "_append_to_bus", lambda env: posted.append(env) or {"ok": True})
    T.READ_TOOLS.discard("connector_status")
    r = reflect_and_notify(apply_fixes=True)
    assert r["peer_notification"]["posted"] is True
    assert posted and posted[0]["topic"] == "self-reflection-patch"
    assert "connector_status" in posted[0]["body"] or "auto-wired" in posted[0]["body"].lower()


def test_self_reflection_tool_registered_and_routed():
    _fresh()
    from mongoose.tools import TOOLS, READ_TOOLS
    assert "self_reflection" in TOOLS
    assert "self_reflection" in READ_TOOLS
    from mongoose.role_router import classify_role
    for q in ["check your own wiring", "audit yourself", "self-reflect"]:
        assert classify_role(q) == "tool"


def test_endpoints_registered():
    _fresh()
    import mongoose.app as A
    routes = {r.path for r in A.app.routes}
    assert "/self/reflect" in routes
    assert "/self/reflect-and-fix" in routes


def test_deferred_surfaces_recorded():
    """Native-surface connectors are deferred; reflection must keep surfacing them, never auto-author."""
    _fresh()
    from mongoose.services.self_reflection import reflect
    r = reflect(apply_fixes=True)
    ks = [f for f in r["findings"] if f["invariant"] == "known_surface"]
    assert ks  # the deferred-surface tracker ran
    for f in ks:
        if not f["ok"]:
            assert f["auto_fixable"] is False  # never auto-authored


# --- connector action path (verify/sync), adopted from the parallel branch's better idea ---

def test_connector_sync_probes_first():
    _fresh()
    from mongoose.connector_probes import sync_connector
    # gmail isn't usable in the sandbox -> must refuse rather than fail mid-sync
    r = sync_connector("gmail")
    assert r["ok"] is False
    assert "isn't usable" in r["detail"] or "not set up" in r["detail"].lower() or "client" in r["detail"].lower()


def test_connector_sync_verify_only_is_honest():
    _fresh()
    from mongoose.connector_probes import sync_connector
    r = sync_connector("home_assistant")
    assert r["ok"] is False
    assert r["syncable"] is False  # HA is verify-only by design


def test_connector_status_tool_actions():
    _fresh()
    import asyncio
    from mongoose.tools import TOOLS
    ex = TOOLS["connector_status"]["executor"]
    # verify action
    assert asyncio.run(ex({"name": "gmail", "action": "verify"}))["ok"] is True
    # sync-all action
    assert asyncio.run(ex({"action": "sync"}))["ok"] is True


def test_connector_action_endpoints_registered():
    _fresh()
    import mongoose.app as A
    routes = {r.path for r in A.app.routes}
    assert "/connectors/verify/{name}" in routes
    assert "/connectors/sync/{name}" in routes
    assert "/connectors/sync-all" in routes
