"""Field bug: transport_status() always set detail to a generic "exit N" string on failure, so
`res.detail or res.stderr` could never fall through to the real SSH error -- discarding e.g.
"Permission denied (publickey)." in favor of a meaningless exit code. handshake_test() then
always showed the same "check SSH is enabled" guidance regardless of the real cause, even when
SSH was reachable and the key was specifically rejected (most commonly: authorized_keys
permissions on the Pi too open for sshd's StrictModes, or root SSH login disabled) -- which is
exactly what happened when ssh-copy-id appeared to succeed but the verification ssh still
prompted for a password."""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
import warnings

import pytest

warnings.filterwarnings("ignore")

pytestmark = pytest.mark.skipif(
    shutil.which("sshd") is None or shutil.which("ssh-keygen") is None,
    reason="sshd/ssh-keygen not available in this environment",
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def local_sshd(tmp_path):
    port = _free_port()
    host_key = tmp_path / "hostkey"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(host_key)],
                   capture_output=True, timeout=10, check=True)
    proc = subprocess.Popen(
        ["/usr/sbin/sshd", "-D", "-p", str(port), "-h", str(host_key),
         "-o", "PasswordAuthentication=no", "-o", "StrictModes=no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


def test_key_rejection_surfaces_the_real_reason_not_a_bare_exit_code(local_sshd, tmp_path, monkeypatch):
    port = local_sshd
    unauthorized_key = tmp_path / "unauthorized_key"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(unauthorized_key)],
                   capture_output=True, timeout=10, check=True)
    monkeypatch.setenv("PI_SSH_HOST", "127.0.0.1")
    monkeypatch.setenv("PI_SSH_PORT", str(port))
    monkeypatch.setenv("PI_SSH_KEY", str(unauthorized_key))

    from mongoose.services.protocol_v3 import transport_status
    t = transport_status()
    assert t["reachable"] is False
    assert "permission denied" in t["detail"].lower()
    assert "publickey" in t["detail"].lower()
    assert not t["detail"].lower().startswith("exit ")


def test_handshake_test_gives_cause_specific_guidance_for_rejected_key(local_sshd, tmp_path, monkeypatch):
    port = local_sshd
    unauthorized_key = tmp_path / "unauthorized_key2"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(unauthorized_key)],
                   capture_output=True, timeout=10, check=True)
    monkeypatch.setenv("PI_SSH_HOST", "127.0.0.1")
    monkeypatch.setenv("PI_SSH_PORT", str(port))
    monkeypatch.setenv("PI_SSH_KEY", str(unauthorized_key))

    from mongoose.services.protocol_v3 import handshake_test
    h = handshake_test()
    step = h["steps"][0]
    assert step["likely_cause"] == "authorized_keys_permissions_or_root_login_disabled"
    assert "chmod 700" in step["detail"]
    assert "authorized_keys" in step["detail"]
    assert "Three things to check" not in step["detail"]


def test_genuine_unreachability_still_gets_the_generic_three_step_guidance(monkeypatch):
    monkeypatch.setenv("PI_SSH_HOST", "127.0.0.1")
    monkeypatch.setenv("PI_SSH_PORT", "1")  # nothing listening
    monkeypatch.delenv("PI_SSH_KEY", raising=False)

    from mongoose.services.protocol_v3 import handshake_test
    h = handshake_test()
    step = h["steps"][0]
    assert "likely_cause" not in step
    assert "Three things to check" in step["detail"]
