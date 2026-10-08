"""Locks in the v0.55.4 container-port fixes so they cannot silently regress.

The failures these guard against were real first-run blockers:
  - "Port 8765 is already in use by a non-Mongoose process" (a profiled api container mapped host 8765)
  - "Bind for 127.0.0.1:8888 failed: port is already allocated" (stale searxng container from a prior
    install folder, because each folder became a separate compose project)
"""
from pathlib import Path
import yaml


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_compose_has_pinned_project_name():
    # A fixed top-level project name means every install (from any download folder) reuses ONE set of
    # containers instead of stacking colliding mongoose-N projects.
    d = yaml.safe_load((_root() / "docker-compose.yml").read_text())
    assert d.get("name") == "mongoose"


def test_no_default_service_binds_host_8765():
    # The native `mongoose serve` process owns host 8765. No container that the installer can bring up
    # may map host 8765, or it blocks the native server. The optional api container must use 8766.
    d = yaml.safe_load((_root() / "docker-compose.yml").read_text())
    for svc, conf in d.get("services", {}).items():
        for mapping in conf.get("ports", []):
            host_part = str(mapping).rsplit(":", 1)[0].split(":")[-1] if ":" in str(mapping) else ""
            assert host_part != "8765", f"service {svc} binds host 8765 ({mapping})"
    # and the api container explicitly maps 8766 -> 8765
    api_ports = [str(p) for p in d["services"]["mongoose-api"].get("ports", [])]
    assert any(p.endswith("8766:8765") or p == "8766:8765" for p in api_ports), api_ports


def test_bootstrap_clears_stale_container_ports_before_compose_up():
    src = (_root() / "scripts" / "bootstrap_macos.sh").read_text()
    # clears stale holders of the ntfy/searxng host ports before bringing the project up
    assert "stop_container_on_host_port" in src
    assert "stop_container_on_host_port 8090" in src
    assert "stop_container_on_host_port 8888" in src
    # and retries once (down --remove-orphans + retry) rather than giving up on first port collision
    assert "--remove-orphans" in src


def test_bootstrap_recovers_native_port_8765_holder():
    src = (_root() / "scripts" / "bootstrap_macos.sh").read_text()
    # the native-server port path diagnoses the holder and can stop a leftover container on $PORT
    assert "diagnose_port_holder" in src
    assert "free_port_of_docker_container" in src


def test_wedged_mongoose_can_be_cleared_without_health():
    """A leftover Mongoose holding the port but NOT answering /health (the reinstall trap the user hit:
    'Python ... TCP 127.0.0.1:8765 (LISTEN)' that /health-gating refused to clear), INCLUDING one
    started from a folder later moved to Trash, must be stoppable by the unified routine in BOTH the
    launcher and the bootstrap. The routine keys off the command being a `mongoose serve` (proven
    pattern via ps), not /health and not the current folder path."""
    root = Path(__file__).resolve().parent.parent
    for rel in ["Install Mongoose.command", "scripts/bootstrap_macos.sh"]:
        src = (root / rel).read_text()
        # the unified routine exists and is the thing invoked to free the port
        assert "ensure_port_free_for_mongoose" in src, rel
        # it identifies a server by command (proven pattern), independent of /health and of the folder
        assert "_cmd_is_mongoose_server" in src and "_pid_is_mongoose_serve" in src, rel
        # it is NOT lsof-dependent for finding the holder (netstat fallback), so it works without lsof
        assert "_pids_on_port" in src and "netstat" in src, rel
        # and it must NOT use the over-loose glob that could match a path containing 'mongoose'
        assert "*mongoose*serve*" not in src, rel


def test_identity_proof_uses_proven_pattern_not_loose_glob():
    """The command-line branch of the identity proof must use the project's PROVEN server pattern
    ("mongoose" + " serve" with a leading space, or uvicorn + "mongoose.app:app") , the same test
    `mongoose stop` uses , NOT the over-loose `*mongoose*serve*` glob (no space) that could match a
    path like .../mongoose-ntfy/serve.yml. The space-prefixed " serve" is what makes the path safe."""
    root = Path(__file__).resolve().parent.parent
    for rel in ["Install Mongoose.command", "scripts/bootstrap_macos.sh"]:
        src = (root / rel).read_text()
        # the dangerous no-space glob must be gone
        assert "*mongoose*serve*" not in src, f"{rel} still has the over-loose no-space glob"
        # the proven pattern is present: space-prefixed serve, and the uvicorn app target
        assert '*mongoose*" serve"*' in src, f"{rel} missing the space-prefixed serve pattern"
        assert "mongoose.app:app" in src, f"{rel} missing the uvicorn app-target pattern"
        # a helper encapsulates it
        assert "_cmd_is_mongoose_server" in src, rel


def test_cmd_is_mongoose_server_matches_real_forms_not_paths():
    """Behaviorally exercise the shell _cmd_is_mongoose_server matcher (extracted from the bootstrap):
    it must match every real server invocation form (including a folder path with a space, and the
    uvicorn form) and must NOT match a path that merely contains 'mongoose' (e.g. .../mongoose-ntfy/)."""
    import subprocess
    # the matcher exactly as shipped
    fn = r'''
    _cmd_is_mongoose_server () {
      local low; low="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
      case "$low" in *mongoose.app:app*) case "$low" in *uvicorn*) return 0;; esac ;; esac
      case "$low" in *mongoose*" serve"*) return 0 ;; esac
      return 1
    }
    '''
    def matches(cmd: str) -> bool:
        script = fn + f'\nif _cmd_is_mongoose_server {cmd!r}; then echo YES; else echo NO; fi\n'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return out.stdout.strip() == "YES"

    # real forms -> must match
    assert matches("/Users/alex/Downloads/mongoose-alpha 4/.venv/bin/python -m mongoose serve --port 8765")
    assert matches("/opt/homebrew/.../Python /Users/alex/Downloads/mongoose-alpha 4/.venv/bin/mongoose serve --port 8765")
    assert matches("mongoose serve --port 8765")
    assert matches(".venv/bin/python .venv/bin/uvicorn mongoose.app:app --port 8765")
    # path traps / unrelated -> must NOT match
    assert not matches("/usr/bin/python -m something --config /Users/alex/mongoose-ntfy/serve.yml")
    assert not matches("node /Users/alex/mongoose-dashboard/server.js")
    assert not matches("Python")  # lsof-truncated name alone is not enough
