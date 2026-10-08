"""Tests for the three coordination/trust features:
1. Device trust store (HTTPS cert generation + per-device install plan)
2. Delightful Pi SSH setup with key automation
3. Auto-handshake on connection open
"""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
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
    return d


# --- 1. Device trust store ---

def test_trust_status_reports_method_and_lan():
    _fresh()
    from mongoose.device_trust import trust_status
    st = trust_status()
    assert st["method"] in ("mkcert", "self-signed")
    assert st["lan_ip"]
    assert "cert_exists" in st


def test_device_trust_plan_self_signed_has_accept_warning_steps(monkeypatch):
    _fresh()
    from mongoose import device_trust as dt
    orig = dt.trust_status
    monkeypatch.setattr(dt, "trust_status",
                        lambda: {**orig(), "method": "self-signed", "lan_ip": "192.168.1.5", "ca_file": ""})
    plan = dt.device_trust_plan("ios")
    assert plan["method"] == "self-signed"
    assert plan["needs_ca_install"] is False
    assert any("warning" in s.lower() for s in plan["steps"])


def test_device_trust_plan_mkcert_ios_has_trust_switch_step(monkeypatch):
    _fresh()
    from mongoose import device_trust as dt
    orig = dt.trust_status
    monkeypatch.setattr(dt, "trust_status",
                        lambda: {**orig(), "method": "mkcert", "lan_ip": "192.168.1.5",
                                 "ca_file": "/fake/rootCA.pem"})
    plan = dt.device_trust_plan("ios")
    assert plan["method"] == "mkcert"
    assert plan["needs_ca_install"] is True
    assert "ca.pem" in plan["ca_url"]
    assert any("Certificate Trust Settings" in s for s in plan["steps"])


def test_ca_download_404_when_no_ca():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        r = c.get("/trust/ca.pem")
        # self-signed/no-cert in the test sandbox => no CA to serve
        assert r.status_code == 404


def test_device_trust_status_endpoint():
    _fresh()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        r = c.get("/cockpit/device-trust/status")
        assert r.status_code == 200
        assert "method" in r.json()


# --- 2. SSH setup with key automation ---

def test_ensure_ssh_key_reuses_existing(monkeypatch):
    _fresh()
    from mongoose.services import protocol_v3 as p
    key_path = p._default_key_path()
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    with open(key_path, "w") as f:
        f.write("PRIV")
    with open(key_path + ".pub", "w") as f:
        f.write("ssh-ed25519 AAAA mongoose-pi-coordination")
    os.chmod(key_path, 0o600)
    k = p.ensure_ssh_key()
    assert k["ok"] is True
    assert k["created"] is False
    assert k["public_key"].startswith("ssh-ed25519")


def test_setup_pi_ssh_no_host_is_friendly():
    _fresh()
    from mongoose.services import protocol_v3 as p
    r = p.setup_pi_ssh()
    assert r["ok"] is False
    assert "add the pi" in r["summary"].lower() or "host" in r["summary"].lower()


def test_authorize_key_manual_fallback_gives_command():
    _fresh(PI_SSH_HOST="pi@192.168.1.20")
    from mongoose.services import protocol_v3 as p
    # pre-create key so ensure_ssh_key succeeds without ssh-keygen
    key_path = p._default_key_path()
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    with open(key_path, "w") as f:
        f.write("PRIV")
    with open(key_path + ".pub", "w") as f:
        f.write("ssh-ed25519 AAAA mongoose")
    os.chmod(key_path, 0o600)
    # No password, no ssh-copy-id in sandbox -> manual fallback (never stores a password)
    import shutil
    if shutil.which("ssh-copy-id"):
        return  # environment has it; the automated path is exercised elsewhere
    a = p.authorize_key_on_pi()
    assert a.get("needs_manual") is True
    assert "ssh-copy-id" in a.get("manual_command", "")


# --- 3. Auto-handshake on connection open ---

def test_handshake_gated_off_by_default():
    _fresh()
    from mongoose.services import protocol_v3 as p
    r = p.post_handshake_when_open()
    assert r["posted"] is False
    assert r["reason"] == "gated"


def test_handshake_fires_only_on_transition(monkeypatch):
    _fresh(PROTOCOL_V3_ENABLED="true", PI_SSH_HOST="pi@192.168.1.20")
    from mongoose.services import protocol_v3 as p
    posted = []
    monkeypatch.setattr(p, "transport_status", lambda: {"reachable": False, "configured": True})
    assert p.post_handshake_when_open()["reason"] == "unreachable"
    # now it opens -> posts once
    monkeypatch.setattr(p, "transport_status", lambda: {"reachable": True, "configured": True})
    monkeypatch.setattr(p, "_append_to_bus", lambda env: posted.append(env) or {"ok": True})
    r1 = p.post_handshake_when_open()
    assert r1["posted"] is True
    assert posted[0]["topic"] == "version-handshake"
    # still open, no transition -> no re-post
    r2 = p.post_handshake_when_open()
    assert r2["posted"] is False
    assert r2["reason"] == "already_open"
    # force re-posts
    r3 = p.post_handshake_when_open(force=True)
    assert r3["posted"] is True


def test_scheduler_registers_v3_handshake_only_when_enabled():
    import inspect
    from mongoose.services import scheduler as S
    src = inspect.getsource(S._build_registry)
    assert "v3_handshake" in src
    assert "protocol_v3_enabled" in src
    # the job itself exists and is off-thread
    assert hasattr(S, "_job_v3_handshake")
