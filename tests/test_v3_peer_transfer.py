"""v3 peer code-swap: efficient (skip-if-same) and verified (hash check after transfer) file
transfer between peers. SSH/scp layer is mocked since there's no real Pi in tests."""
import hashlib
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
    return d


def _local_file(d, name="mod.py", content="print('hi')\n"):
    p = os.path.join(d, name)
    open(p, "w").write(content)
    return p, hashlib.sha256(content.encode()).hexdigest()


def test_unconfigured_is_graceful():
    d = _fresh()
    import mongoose.services.protocol_v3 as V3
    lp, _ = _local_file(d)
    r = V3.push_file_to_peer(lp, "/config/mod.py")
    assert r["ok"] is False
    assert r["configured"] is False


def test_missing_local_file():
    _fresh()
    os.environ["PI_SSH_HOST"] = "fakehost"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.services.protocol_v3 as V3
    r = V3.push_file_to_peer("/nonexistent/x.py", "/config/x.py")
    assert r["ok"] is False
    assert "not found" in r["error"]
    os.environ.pop("PI_SSH_HOST", None)


def test_skip_if_same(monkeypatch):
    d = _fresh()
    os.environ["PI_SSH_HOST"] = "fakehost"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.services.protocol_v3 as V3
    lp, h = _local_file(d)
    # Peer reports the same hash -> no transfer
    monkeypatch.setattr(V3, "_remote_file_sha256", lambda p: h)
    r = V3.push_file_to_peer(lp, "/config/mod.py")
    assert r["ok"] is True
    assert r["unchanged"] is True
    assert r["transferred"] is False
    os.environ.pop("PI_SSH_HOST", None)


def test_transfer_and_verify_success(monkeypatch):
    d = _fresh()
    os.environ["PI_SSH_HOST"] = "fakehost"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.services.protocol_v3 as V3
    from mongoose.services.protocol_v3 import TransportResult
    lp, h = _local_file(d)
    # First hash call (skip check) returns different; after "transfer", returns matching
    state = {"n": 0}
    def fake_hash(p):
        state["n"] += 1
        return h if state["n"] > 1 else "different000"
    monkeypatch.setattr(V3, "_remote_file_sha256", fake_hash)
    monkeypatch.setattr(V3, "_run_remote", lambda c, **k: TransportResult(ok=True))

    class _Proc:
        returncode = 0; stdout = ""; stderr = ""
    monkeypatch.setattr(V3.subprocess, "run", lambda *a, **k: _Proc())
    r = V3.push_file_to_peer(lp, "/config/mod.py")
    assert r["ok"] is True
    assert r["transferred"] is True
    assert r["verified"] is True
    os.environ.pop("PI_SSH_HOST", None)


def test_corrupt_transfer_caught(monkeypatch):
    d = _fresh()
    os.environ["PI_SSH_HOST"] = "fakehost"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.services.protocol_v3 as V3
    from mongoose.services.protocol_v3 import TransportResult
    lp, h = _local_file(d)
    monkeypatch.setattr(V3, "_remote_file_sha256", lambda p: "wrong_hash_after_landing")
    monkeypatch.setattr(V3, "_run_remote", lambda c, **k: TransportResult(ok=True))

    class _Proc:
        returncode = 0; stdout = ""; stderr = ""
    monkeypatch.setattr(V3.subprocess, "run", lambda *a, **k: _Proc())
    r = V3.push_file_to_peer(lp, "/config/mod.py", skip_if_same=False)
    assert r["ok"] is False
    assert "verification failed" in r["error"]
    os.environ.pop("PI_SSH_HOST", None)


def test_batch_reports_per_file(monkeypatch):
    d = _fresh()
    os.environ["PI_SSH_HOST"] = "fakehost"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.services.protocol_v3 as V3
    lp1, h1 = _local_file(d, "a.py", "AAA\n")
    lp2, h2 = _local_file(d, "b.py", "BBB\n")
    # Both already identical on peer -> all unchanged
    def fake_hash(remote):
        # map remote path back by suffix
        if remote.endswith("a.py"): return h1
        if remote.endswith("b.py"): return h2
        return None
    monkeypatch.setattr(V3, "_remote_file_sha256", fake_hash)
    r = V3.push_files_to_peer([(lp1, "/config/a.py"), (lp2, "/config/b.py")])
    assert r["ok"] is True
    assert r["unchanged"] == 2
    assert r["transferred"] == 0
    assert r["total"] == 2
    os.environ.pop("PI_SSH_HOST", None)


def test_absurdly_long_path_does_not_crash():
    """Adversarial QA regression: a huge local_path must return a graceful error, not raise OSError."""
    _fresh()
    os.environ["PI_SSH_HOST"] = "fakehost"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.services.protocol_v3 as V3
    r = V3.push_file_to_peer("a" * 100000, "/x")
    assert r["ok"] is False
    assert "not found" in r["error"]
    os.environ.pop("PI_SSH_HOST", None)
