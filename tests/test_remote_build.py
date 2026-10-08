"""Remote Pi build node: dispatch `claude -p` to the Pi over SSH. Tested with the transport mocked so
the command construction, billing guard, readiness gating, and rate-limit detection are all verified
without a real Pi."""
import os
import tempfile



def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("PI_SSH_HOST", "ANTHROPIC_API_KEY"):
        os.environ.pop(k, None)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()
    return d


class _TR:
    """Stand-in for TransportResult."""
    def __init__(self, ok=True, stdout="", stderr="", configured=True):
        self.ok = ok; self.stdout = stdout; self.stderr = stderr
        self.configured = configured; self.detail = "" if ok else "exit 1"


def test_build_dir_default():
    _fresh()
    from mongoose.services import remote_build as RB
    assert RB.remote_build_dir() == "~/mongoose-builds"


def test_readiness_unconfigured_is_hard_stop():
    _fresh()
    from mongoose.services import remote_build as RB
    r = RB.readiness()
    assert r["ok"] is False and r["ready"] is False
    assert "reachable" in r["detail"].lower()


def test_readiness_ready_when_pi_has_claude_no_apikey(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    # Pi present, reachable, no API key
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": False})
    # mkdir -p succeeds
    monkeypatch.setattr(RB, "_run_remote",
                        lambda cmd, timeout=12: _TR(ok=True, stdout="__ok__"))
    r = RB.readiness()
    assert r["ok"] is True and r["ready"] is True


def test_readiness_api_key_is_soft_stop(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": True})
    r = RB.readiness()
    assert r["ok"] is True and r["ready"] is False
    assert "API rates" in r["detail"]
    # ...but allow_api_key overrides, given mkdir succeeds
    monkeypatch.setattr(RB, "_run_remote", lambda cmd, timeout=12: _TR(ok=True, stdout="__ok__"))
    r2 = RB.readiness(allow_api_key=True)
    assert r2["ready"] is True


def test_invoke_builds_correct_remote_command(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": False})
    captured = {}

    def fake_run_remote(cmd, timeout=12):
        # the mkdir from readiness, then the build command
        if cmd.startswith("mkdir"):
            return _TR(ok=True, stdout="__ok__")
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return _TR(ok=True, stdout='{"type":"result"} build done')

    monkeypatch.setattr(RB, "_run_remote", fake_run_remote)
    ok, out, rl = RB.invoke_remote_build("add a healthcheck endpoint", "~/mongoose-builds/app")
    assert ok is True and rl is False
    c = captured["cmd"]
    assert c.startswith("cd ") and "~/mongoose-builds/app" in c
    assert "claude -p" in c
    assert "--permission-mode dontAsk" in c
    assert "--output-format stream-json" in c
    # task is shell-quoted as one argument
    assert "'add a healthcheck endpoint'" in c
    # timeout is build_step_timeout_s (1800) + headroom
    assert captured["timeout"] >= 1800


def test_invoke_detects_rate_limit(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": False})

    def fake_run_remote(cmd, timeout=12):
        if cmd.startswith("mkdir"):
            return _TR(ok=True, stdout="__ok__")
        return _TR(ok=False, stdout="Error: usage limit reached for this account")

    monkeypatch.setattr(RB, "_run_remote", fake_run_remote)
    ok, out, rl = RB.invoke_remote_build("x", "~/b/x")
    assert ok is False and rl is True


def test_invoke_blocked_by_api_key_without_allow(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": True})
    ok, out, rl = RB.invoke_remote_build("x", "~/b/x")
    assert ok is False
    assert "API rates" in out


def test_task_with_quotes_cannot_break_shell(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": False})
    captured = {}

    def fake_run_remote(cmd, timeout=12):
        if cmd.startswith("mkdir"):
            return _TR(ok=True, stdout="__ok__")
        captured["cmd"] = cmd
        return _TR(ok=True, stdout="ok")

    monkeypatch.setattr(RB, "_run_remote", fake_run_remote)
    nasty = "build; rm -rf / # 'injection' attempt"
    RB.invoke_remote_build(nasty, "~/b/x")
    # the whole nasty string must be a single quoted argument, not interpretable shell
    assert "rm -rf /" in captured["cmd"]  # present as data
    # but wrapped in quotes so it's one arg to claude -p, not a command separator the remote shell runs
    import shlex
    assert shlex.quote(nasty) in captured["cmd"]


def test_sync_repo_missing_local_errors():
    _fresh()
    from mongoose.services import remote_build as RB
    r = RB.sync_repo_to_pi("/nonexistent/repo/path")
    assert r["ok"] is False and "not found" in r["error"]


# ── Protocol v3 coordination: builds in /config territory must hold the v3 lock ──────────────────

def test_own_namespace_build_takes_no_lock(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": False})
    acquired = {"called": False}

    def fake_acquire(target, ttl_seconds=60):
        acquired["called"] = True
        return {"ok": True, "acquired": True}

    def fake_run_remote(cmd, timeout=12):
        if cmd.startswith("mkdir"):
            return _TR(ok=True, stdout="__ok__")
        return _TR(ok=True, stdout="built")

    monkeypatch.setattr("mongoose.services.protocol_v3.acquire_lock", fake_acquire)
    monkeypatch.setattr(RB, "_run_remote", fake_run_remote)
    ok, out, rl = RB.invoke_remote_build("x", "~/mongoose-builds/app")
    assert ok is True
    assert acquired["called"] is False  # own namespace: NO lock taken


def test_config_build_acquires_and_releases_lock(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": False})
    events = []
    monkeypatch.setattr("mongoose.services.protocol_v3.acquire_lock",
                        lambda target, ttl_seconds=60: events.append(("acquire", target)) or
                        {"ok": True, "acquired": True})
    monkeypatch.setattr("mongoose.services.protocol_v3.release_lock",
                        lambda target: events.append(("release", target)) or {"ok": True})

    def fake_run_remote(cmd, timeout=12):
        if cmd.startswith("mkdir"):
            return _TR(ok=True, stdout="__ok__")
        return _TR(ok=True, stdout="built")

    monkeypatch.setattr(RB, "_run_remote", fake_run_remote)
    ok, out, rl = RB.invoke_remote_build("edit a package", "/config/packages/foo")
    assert ok is True
    # acquired the 'packages' lock, then released it
    assert ("acquire", "packages") in events
    assert ("release", "packages") in events


def test_config_build_refuses_when_locked(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": False})
    # lock held by another live agent
    monkeypatch.setattr("mongoose.services.protocol_v3.acquire_lock",
                        lambda target, ttl_seconds=60: {"ok": False, "acquired": False,
                                                        "error": "held", "holder_sid": "chat-123"})
    ran = {"build": False}

    def fake_run_remote(cmd, timeout=12):
        if cmd.startswith("mkdir"):
            return _TR(ok=True, stdout="__ok__")
        ran["build"] = True
        return _TR(ok=True, stdout="built")

    monkeypatch.setattr(RB, "_run_remote", fake_run_remote)
    ok, out, rl = RB.invoke_remote_build("edit a package", "/config/packages/foo")
    assert ok is False
    assert "locked" in out.lower() and "chat" in out.lower()
    assert ran["build"] is False  # build never ran while another agent holds the lock


def test_respect_v3_false_skips_lock(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    monkeypatch.setattr("mongoose.services.claude_code._pi_claude_code",
                        lambda: {"where": "pi", "present": True, "reachable": True,
                                 "api_key_in_env": False})
    called = {"acquire": False}
    monkeypatch.setattr("mongoose.services.protocol_v3.acquire_lock",
                        lambda target, ttl_seconds=60: called.__setitem__("acquire", True) or
                        {"ok": True, "acquired": True})

    def fake_run_remote(cmd, timeout=12):
        if cmd.startswith("mkdir"):
            return _TR(ok=True, stdout="__ok__")
        return _TR(ok=True, stdout="built")

    monkeypatch.setattr(RB, "_run_remote", fake_run_remote)
    ok, out, rl = RB.invoke_remote_build("x", "/config/packages/foo", respect_v3=False)
    assert ok is True
    assert called["acquire"] is False  # explicit override skips the lock


def test_classification_helpers():
    _fresh()
    from mongoose.services import remote_build as RB
    assert RB.touches_shared_config("/config") is True
    assert RB.touches_shared_config("/config/packages/x") is True
    assert RB.touches_shared_config("~/mongoose-builds/app") is False
    assert RB._v3_lock_target("/config/dashboards/x") == "dashboards"
    assert RB._v3_lock_target("/config") == "config"


# ── Protocol v3 coordination ──────────────────────────────────────────────────────────────────────
# A build node on a SHARED Pi (Chat/Code Claude live there too) must coordinate through v3: take a
# lock before building, back off if a live peer holds it, announce on the bus, and release its own lock.

def test_coordinated_build_backs_off_when_peer_holds_lock(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    # A live peer holds the build lock.
    monkeypatch.setattr(RB, "acquire_lock",
                        lambda target, ttl_seconds=60: {"ok": False, "error": "held",
                                                        "holder_sid": "code-claude-123"})
    released = {"n": 0}
    monkeypatch.setattr(RB, "release_lock", lambda target: released.__setitem__("n", released["n"] + 1))
    # readiness/run must NOT be reached; if they were and hit the (absent) Pi it'd still be fine, but
    # we assert we stopped at the lock stage.
    out = RB.coordinated_remote_build("do x", "/tmp/repo")
    assert out["ok"] is False
    assert out["stage"] == "lock"
    assert "holds the 'mongoose-build' lock" in out["detail"] or "Backing off" in out["detail"]
    # We never took the lock, so we must not have released anything.
    assert released["n"] == 0


def test_coordinated_build_takes_lock_announces_and_releases(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    events = []
    monkeypatch.setattr(RB, "acquire_lock",
                        lambda target, ttl_seconds=60: {"ok": True, "acquired": True, "sid": "mongoose-1",
                                                        "reclaimed": False})
    monkeypatch.setattr(RB, "release_lock",
                        lambda target: events.append(("release", target)) or {"ok": True})
    monkeypatch.setattr(RB, "_bus_announce", lambda topic, body: events.append(("bus", topic)))
    # Make the inner build succeed without a Pi.
    monkeypatch.setattr(RB, "run_remote_build",
                        lambda task, repo, **kw: {"ok": True, "stage": "build", "remote_repo": "~/b/x",
                                                  "files_synced": 1, "rate_limited": False})
    out = RB.coordinated_remote_build("add endpoint", "/tmp/repo")
    assert out["ok"] is True and out["coordinated"] is True
    topics = [t for kind, t in events if kind == "bus"]
    assert "mongoose-build-start" in topics
    assert "mongoose-build-done" in topics
    # the lock we took must be released
    assert ("release", "mongoose-build") in events


def test_coordinated_build_releases_lock_on_failure(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    events = []
    monkeypatch.setattr(RB, "acquire_lock",
                        lambda target, ttl_seconds=60: {"ok": True, "acquired": True, "sid": "m1",
                                                        "reclaimed": False})
    monkeypatch.setattr(RB, "release_lock",
                        lambda target: events.append(("release", target)) or {"ok": True})
    monkeypatch.setattr(RB, "_bus_announce", lambda topic, body: events.append(("bus", topic)))
    monkeypatch.setattr(RB, "run_remote_build",
                        lambda task, repo, **kw: {"ok": False, "stage": "build"})
    out = RB.coordinated_remote_build("x", "/tmp/repo")
    assert out["ok"] is False
    assert ("bus", "mongoose-build-failed") in events
    assert ("release", "mongoose-build") in events  # released even though the build failed


def test_coordinated_reclaim_posts_lock_reclaim(monkeypatch):
    _fresh()
    import mongoose.services.remote_build as RB
    events = []
    monkeypatch.setattr(RB, "acquire_lock",
                        lambda target, ttl_seconds=60: {"ok": True, "acquired": True, "sid": "m1",
                                                        "reclaimed": True, "previous_sid": "stale-9"})
    monkeypatch.setattr(RB, "release_lock", lambda target: {"ok": True})
    monkeypatch.setattr(RB, "_bus_announce", lambda topic, body: events.append(topic))
    monkeypatch.setattr(RB, "run_remote_build", lambda task, repo, **kw: {"ok": True, "stage": "build"})
    RB.coordinated_remote_build("x", "/tmp/repo")
    assert "lock-reclaim" in events  # protocol requires announcing a stale-lock reclaim
