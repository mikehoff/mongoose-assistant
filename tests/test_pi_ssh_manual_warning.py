"""Pi SSH journey: when the automated path can't run (no sshpass), the manual fallback must give the
CORRECT ssh-copy-id command (with -i, no sudo) AND an explicit warning against the two mistakes that
produce 'No identities found' — bare `ssh-copy-id <host>` and `sudo ssh-copy-id`."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["PI_SSH_HOST"] = "alex@192.168.1.20"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_manual_command_uses_dash_i_not_bare(monkeypatch):
    _fresh()
    import shutil
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(P, "ensure_ssh_key",
                        lambda: {"ok": True, "key_path": "/tmp/k", "public_key": "ssh-ed25519 AAAA", "detail": "ok"})
    orig = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: None if n in ("sshpass", "ssh-copy-id") else orig(n))
    r = P.authorize_key_on_pi(password="pw", host="alex@192.168.1.20")
    assert r["needs_manual"] is True
    cmd = r["manual_command"]
    assert "-i" in cmd and "/tmp/k.pub" in cmd  # uses mongoose's key explicitly
    assert "sudo" not in cmd                      # never sudo
    assert cmd.strip().startswith("ssh-copy-id -i")


def test_manual_warning_names_the_two_traps(monkeypatch):
    _fresh()
    import shutil
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(P, "ensure_ssh_key",
                        lambda: {"ok": True, "key_path": "/tmp/k", "public_key": "ssh-ed25519 AAAA", "detail": "ok"})
    orig = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: None if n in ("sshpass", "ssh-copy-id") else orig(n))
    r = P.authorize_key_on_pi(password="pw", host="alex@192.168.1.20")
    w = r.get("warning", "")
    assert "sudo" in w                  # warns against sudo
    assert "No identities" in w         # explains the bare-form failure
    assert "-i" in w                    # tells them why the -i form works


def test_offers_sshpass_install_when_password_given(monkeypatch):
    _fresh()
    import shutil
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(P, "ensure_ssh_key",
                        lambda: {"ok": True, "key_path": "/tmp/k", "public_key": "ssh-ed25519 AAAA", "detail": "ok"})
    orig = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: None if n in ("sshpass", "ssh-copy-id") else orig(n))
    r = P.authorize_key_on_pi(password="pw", host="alex@192.168.1.20")
    assert r.get("needs_sshpass") is True
    assert "sshpass" in r.get("install_command", "")
