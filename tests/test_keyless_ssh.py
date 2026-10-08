"""Keyless Pi SSH (username + password) mode."""
import os
import shutil
import subprocess
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
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


def test_keyless_builds_sshpass_command(monkeypatch):
    _fresh(PI_SSH_HOST="pi@192.168.1.20", PI_SSH_PASSWORD="secret")
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/sshpass" if n == "sshpass" else None)
    cmd = P._ssh_base_cmd()
    assert cmd[0] == "sshpass"
    assert "BatchMode=yes" not in cmd          # password auth needs the prompt
    assert "PubkeyAuthentication=no" in cmd     # force password path
    assert "secret" not in " ".join(cmd)        # password NEVER on argv


def test_key_mode_wins_when_key_present(monkeypatch):
    _fresh(PI_SSH_HOST="pi@h", PI_SSH_PASSWORD="secret", PI_SSH_KEY="/k")
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/sshpass")
    cmd = P._ssh_base_cmd()
    assert cmd[0] == "ssh"                       # a real key always preferred
    assert "BatchMode=yes" in cmd


def test_missing_sshpass_returns_sentinel(monkeypatch):
    _fresh(PI_SSH_HOST="pi@h", PI_SSH_PASSWORD="secret")
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert P._ssh_base_cmd() == ["__NEEDS_SSHPASS__"]


def test_run_remote_friendly_when_no_sshpass(monkeypatch):
    _fresh(PI_SSH_HOST="pi@h", PI_SSH_PASSWORD="secret")
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(shutil, "which", lambda n: None)
    res = P._run_remote("echo hi")
    assert "sshpass" in res.detail and "brew install" in res.detail


def test_run_remote_passes_password_via_env_not_argv(monkeypatch):
    _fresh(PI_SSH_HOST="pi@h", PI_SSH_PASSWORD="topsecret")
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/sshpass")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env") or {}

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    P._run_remote("echo hi")
    assert captured["env"].get("SSHPASS") == "topsecret"
    assert "topsecret" not in " ".join(captured["cmd"])


def test_setup_password_needs_sshpass(monkeypatch):
    _fresh()
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(shutil, "which", lambda n: None)
    r = P.setup_pi_ssh_password("pw", host="pi@192.168.1.20")
    assert r["needs_sshpass"] is True
    assert "brew install sshpass" in r["install_command"]


def test_setup_password_no_host_friendly(monkeypatch):
    _fresh()
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(P, "get_settings", lambda: type("S", (), {
        "pi_ssh_host": "", "pi_ssh_password": "", "pi_ssh_key": "", "pi_ssh_port": 22})())
    r = P.setup_pi_ssh_password("pw")
    assert r["ok"] is False
    assert "address" in r["summary"].lower()


def test_setup_password_saves_and_verifies(monkeypatch):
    _fresh()
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/sshpass")
    monkeypatch.setattr(P, "transport_status", lambda: {"configured": True, "reachable": True})
    monkeypatch.setattr(P, "post_handshake_when_open", lambda **k: {"posted": False})
    # don't write a real .env (would pollute other tests); capture the persisted creds instead
    saved = {}
    import mongoose.setup_wizard as W
    monkeypatch.setattr(W, "write_env", lambda updates, path=None: saved.update(updates))
    r = P.setup_pi_ssh_password("pw", host="pi@192.168.1.20")
    assert r["ok"] is True and r["ready"] is True and r["mode"] == "password"
    # password persisted, key cleared (via the captured write_env call + os.environ)
    assert saved.get("PI_SSH_PASSWORD") == "pw"
    assert saved.get("PI_SSH_KEY") == ""


def test_setup_password_endpoint_registered():
    _fresh()
    import mongoose.app as A
    routes = {r.path for r in A.app.routes}
    assert "/cockpit/protocol-v3/setup-ssh-password" in routes


def test_cockpit_has_keyless_option():
    _fresh()
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "setupV3SshPassword" in html
    assert "just username and password" in html.lower()
