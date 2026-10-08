"""Pi reach journey: the ssh-copy-id command Mongoose tells the user to run must be the WORKING form.
The user hit 'No identities found' because the guidance said a bare `ssh-copy-id <host>` (which looks
for a non-existent default key) and they prefixed sudo (wrong home). The command must use -i with
mongoose's own key and never sudo."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("PI_SSH_HOST", "PI_SSH_PORT"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_manual_copy_cmd_uses_i_flag_no_sudo():
    from mongoose.services.protocol_v3 import _manual_copy_cmd, _default_key_path
    cmd = _manual_copy_cmd(_default_key_path() + ".pub", "192.168.1.20", 22)
    assert "-i " in cmd  # uses mongoose's key explicitly
    assert "sudo" not in cmd
    assert "192.168.1.20" in cmd
    assert cmd.startswith("ssh-copy-id")


def test_manual_copy_cmd_with_custom_port():
    from mongoose.services.protocol_v3 import _manual_copy_cmd, _default_key_path
    cmd = _manual_copy_cmd(_default_key_path() + ".pub", "host", 2222)
    assert "-p 2222" in cmd


def test_unreachable_message_gives_correct_command(monkeypatch):
    """When the Pi is unreachable, the journey must surface the -i command and warn against sudo and
    the bare form."""
    import mongoose.services.protocol_v3 as V

    # force transport to 'configured but unreachable'
    monkeypatch.setattr(V, "transport_status",
                        lambda: {"configured": True, "reachable": False, "detail": "timeout"})

    import asyncio
    # v3_doctor may be sync or async; call the readiness/journey entrypoint
    fn = getattr(V, "handshake_test", None)
    assert fn is not None, "expected handshake_test"
    res = fn()
    if asyncio.iscoroutine(res):
        res = asyncio.run(res)
    # find the 'Reach the Pi' step
    steps = res.get("steps", [])
    reach = next((s for s in steps if s.get("step") == "Reach the Pi"), None)
    assert reach is not None
    cmd = reach.get("manual_command", "")
    assert "-i " in cmd and "sudo" not in cmd
    warning = reach.get("warning", "")
    assert "sudo" in warning.lower()  # explicitly warns against sudo
    assert "No identities found" in warning  # names the actual error
