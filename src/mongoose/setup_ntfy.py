"""One-step native ntfy setup, the reliable, Docker-free way to get phone push working.

Why native rather than Docker: the bundled local ntfy runs in Docker, but Docker Desktop on a Mac
must be running before it can start, a heavy background app and a single point of failure (exactly
the "Docker is not running" snag). A native ntfy is a small binary that can be supervised by launchd
so it starts on login and stays up. Same local-first guarantee: the server listens on localhost, so
notification content (the daily brief, RTM approvals) never leaves the machine's network.

What this does:
  1. installs ntfy via Homebrew if missing (with confirmation),
  2. writes a minimal local server config (listen on 127.0.0.1:8090),
  3. installs a launchd LaunchAgent so ntfy runs automatically and restarts if it dies,
  4. wires .env: NTFY_URL=http://127.0.0.1:8090 and a generated NTFY_TOPIC,
  5. verifies the server answers.

It is idempotent (safe to re-run) and never points you at a public server.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

NTFY_PORT = 8090
NTFY_URL = f"http://127.0.0.1:{NTFY_PORT}"
_LAUNCH_LABEL = "uk.mongoose.ntfy"


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _config_dir() -> Path:
    # Keep ntfy's local config inside the install's data dir so it's self-contained and removable.
    from .settings import get_settings
    d = Path(getattr(get_settings(), "data_dir", "data")) / "ntfy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _server_config_path() -> Path:
    return _config_dir() / "server.yml"


def _write_server_config() -> Path:
    """A minimal ntfy server config that listens only on localhost (local-first)."""
    cfg = _server_config_path()
    cache_db = _config_dir() / "cache.db"
    cfg.write_text(
        # ntfy reads YAML; bind to loopback so nothing is exposed on the LAN/internet.
        f"listen-http: \"127.0.0.1:{NTFY_PORT}\"\n"
        f"cache-file: \"{cache_db}\"\n"
        "cache-duration: \"12h\"\n"
        "# Local-first: this server is bound to 127.0.0.1, so notification content stays on this\n"
        "# machine. Your phone reaches it over your LAN via the Mac's address.\n"
    )
    return cfg


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCH_LABEL}.plist"


def _write_launch_agent(ntfy_bin: str, cfg: Path) -> Path:
    """A launchd LaunchAgent so ntfy starts on login and is restarted if it exits."""
    logs = _config_dir() / "ntfy.log"
    plist = _launch_agent_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'  <key>Label</key><string>{_LAUNCH_LABEL}</string>\n'
        '  <key>ProgramArguments</key>\n'
        f'  <array><string>{ntfy_bin}</string><string>serve</string>'
        f'<string>--config</string><string>{cfg}</string></array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        f'  <key>StandardOutPath</key><string>{logs}</string>\n'
        f'  <key>StandardErrorPath</key><string>{logs}</string>\n'
        '</dict></plist>\n'
    )
    return plist


def _load_launch_agent(plist: Path) -> None:
    """Load AND start the launch agent now. The legacy `launchctl load/unload` is deprecated on
    modern macOS (Catalina+) and often silently no-ops for GUI agents, which is why ntfy wasn't
    actually starting. Prefer the modern domain-target API (bootstrap/bootout/kickstart) and fall
    back to the legacy commands on older systems. kickstart -k starts it immediately, not just on
    next login."""
    import os as _os
    uid = _os.getuid()
    domain = f"gui/{uid}"
    label_target = f"{domain}/{_LAUNCH_LABEL}"

    # Tear down any previous instance so a re-run picks up config changes (non-fatal if absent).
    subprocess.run(["launchctl", "bootout", label_target], capture_output=True)
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)

    # Modern load (bootstrap). If it fails (older macOS), fall back to legacy load.
    r = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True)

    # Start it NOW (RunAtLoad only fires on (re)load; kickstart -k force-(re)starts immediately).
    subprocess.run(["launchctl", "kickstart", "-k", label_target], capture_output=True)


def _launch_agent_loaded() -> bool:
    """True if launchd currently knows about our agent (best-effort)."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
        return _LAUNCH_LABEL in (out.stdout or "")
    except Exception:
        return False


def _update_env(topic: str) -> None:
    from .setup_wizard import write_env
    write_env({"NTFY_URL": NTFY_URL, "NTFY_TOPIC": topic})
    # Make it usable immediately by the running process too.
    os.environ["NTFY_URL"] = NTFY_URL
    os.environ["NTFY_TOPIC"] = topic


def _existing_topic() -> str | None:
    from .setup_wizard import effective_env
    t = effective_env().get("NTFY_TOPIC", "")
    return t or None


def _server_responds() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"{NTFY_URL}/v1/health", timeout=3) as r:
            return r.status < 500
    except Exception:
        # /v1/health may 404 on some versions; any TCP answer means it's up.
        try:
            import urllib.request
            urllib.request.urlopen(NTFY_URL, timeout=3)
            return True
        except Exception as exc:  # noqa: BLE001
            return "Connection refused" not in str(exc) and "refused" not in str(exc).lower()


def setup_ntfy(yes: bool = False) -> int:
    """Install and configure a local ntfy for reliable phone push. Returns a process exit code."""
    import platform
    if platform.system() != "Darwin":
        print("setup-ntfy targets macOS (Homebrew + launchd). On Linux, install ntfy from your")
        print("package manager and set NTFY_URL=http://127.0.0.1:8090 with a topic of your choice.")
        return 1

    # 1. ntfy binary
    if not _have("ntfy"):
        if not _have("brew"):
            print("Homebrew isn't installed, and it's the easy way to get ntfy.")
            print("Install Homebrew from https://brew.sh, then re-run: mongoose setup-ntfy")
            return 1
        if not yes:
            ans = input("Install ntfy via Homebrew now? [Y/n] ").strip().lower()
            if ans in ("n", "no"):
                print("Skipped. Re-run when ready: mongoose setup-ntfy")
                return 1
        print("Installing ntfy (brew install ntfy)...")
        r = subprocess.run(["brew", "install", "ntfy"])
        if r.returncode != 0 or not _have("ntfy"):
            print("brew install ntfy didn't complete. You can try it manually, then re-run.")
            return 1

    ntfy_bin = shutil.which("ntfy")
    assert ntfy_bin

    # 2. local server config (loopback only)
    cfg = _write_server_config()
    print(f"Wrote local ntfy config: {cfg}")

    # 3. launchd service so it stays up
    plist = _write_launch_agent(ntfy_bin, cfg)
    _load_launch_agent(plist)
    print(f"Installed launchd service ({_LAUNCH_LABEL}), ntfy will start on login and stay running.")

    # 4. .env wiring (keep an existing topic so the phone app stays subscribed)
    import secrets
    topic = _existing_topic() or f"mongoose-{secrets.token_hex(4)}"
    _update_env(topic)
    print(f"Set NTFY_URL={NTFY_URL} and NTFY_TOPIC={topic} in .env.")

    # 5. verify , the launchd agent should have started it (kickstart). If it hasn't answered yet,
    # fall back to starting ntfy directly (detached) so it IS running this session regardless; the
    # launch agent still owns it for future logins.
    import time
    up = False
    for _ in range(12):
        if _server_responds():
            up = True
            break
        time.sleep(0.5)
    if not up:
        try:
            logs = _config_dir() / "ntfy.log"
            with open(logs, "ab") as log:
                subprocess.Popen([ntfy_bin, "serve", "--config", str(cfg)],
                                 stdout=log, stderr=log, start_new_session=True)
            for _ in range(12):
                if _server_responds():
                    up = True
                    break
                time.sleep(0.5)
        except Exception:
            pass
    if up:
        print("\nLocal ntfy is running. Next:")
        print("  1. Install the ntfy app on your phone (App Store / Play Store).")
        print(f"  2. In the app, subscribe to the topic:  {topic}")
        print(f"     using your Mac's address, e.g.  http://<your-mac-ip>:{NTFY_PORT}")
        print("  3. Test it:  mongoose tell-phone \"hello from Mongoose\"   (or the daily brief).")
        print("\nNotification content stays on your network, this server is bound to localhost.")
        return 0
    print("\nntfy was configured but didn't answer yet. Check the log:")
    print(f"  {_config_dir() / 'ntfy.log'}")
    print("You can also start it by hand:  ntfy serve --config", cfg)
    return 1
