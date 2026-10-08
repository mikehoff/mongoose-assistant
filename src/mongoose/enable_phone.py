"""`mongoose enable-phone`, turn phone access from a dead-end warning into a working setup in one
step. Pairing a phone needs the server to listen on the LAN, which needs a restart (uvicorn binds the
socket at startup). This writes the three settings that make it work, generates an access token if
there isn't one, and restarts the server so the change takes effect. Default stays loopback-only until
the user runs this, we never expose the server without an explicit opt-in.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from typing import Any


def ensure_phone_access_defaults() -> dict[str, Any]:
    """Idempotently write the settings a phone needs (MONGOOSE_HOST=0.0.0.0, MONGOOSE_ALLOW_REMOTE=true,
    a token) BEFORE the server binds its socket. The start/install scripts call this just before
    `mongoose serve`, so the very first QR code points at a LAN address the server is ACTUALLY listening
    on, instead of advertising a LAN URL while uvicorn is still loopback-only (the exact cause of "QR
    shows the right IP but the phone gets connection refused"). Not a restart routine: it only writes
    config + updates this process's env. Returns what changed."""
    from .setup_wizard import effective_env, write_env
    env = effective_env()
    updates: dict[str, str] = {}
    host = (env.get("MONGOOSE_HOST") or "").strip()
    allow = str(env.get("MONGOOSE_ALLOW_REMOTE") or "").strip().lower()
    token = (env.get("MONGOOSE_API_TOKEN") or "").strip()
    if host not in ("0.0.0.0", "::"):
        updates["MONGOOSE_HOST"] = "0.0.0.0"
    if allow not in ("1", "true", "yes", "on"):
        updates["MONGOOSE_ALLOW_REMOTE"] = "true"
    token_generated = False
    if not token:
        updates["MONGOOSE_API_TOKEN"] = secrets.token_urlsafe(24)
        token_generated = True
    if updates:
        write_env(updates)
        # If a caller starts uvicorn in-process after calling this, make the running process see the
        # same values (the shell path re-reads .env anyway).
        for k, v in updates.items():
            os.environ[k] = v
    return {"ok": True, "changed": bool(updates), "token_generated": token_generated,
            "updates": sorted(updates),
            "host": updates.get("MONGOOSE_HOST", host or "0.0.0.0"),
            "allow_remote": updates.get("MONGOOSE_ALLOW_REMOTE", allow or "true")}


def _primary_ip() -> str:
    try:
        from .discovery import _primary_ip as disc
        return disc() or ""
    except Exception:
        return ""


def enable_phone(yes: bool = False) -> int:
    """Enable LAN access so a phone can reach Mongoose, then restart. Returns an exit code."""
    from .setup_wizard import write_env, effective_env

    env = effective_env()
    # Resolve the ACTUAL port this install uses (MONGOOSE_PORT in env, default 8765). Hardcoding 8765
    # would restart a custom-port install onto the wrong port and print the wrong pairing address. (audit P7)
    import os as _os
    port = (env.get("MONGOOSE_PORT") or _os.getenv("MONGOOSE_PORT") or "8765").strip() or "8765"
    already = env.get("MONGOOSE_HOST", "") in ("0.0.0.0", "::") and \
        str(env.get("MONGOOSE_ALLOW_REMOTE", "")).lower() in ("1", "true", "yes")
    token = env.get("MONGOOSE_API_TOKEN", "") or ""
    if already and token:
        ip = _primary_ip() or "your-mac-ip"
        print(f"Phone access is already enabled (server listens on the LAN at {ip}:{port}).")
        print("If the phone still can't connect, make sure Mongoose has been restarted since enabling,")
        print("and that the phone is on the same Wi-Fi. Re-run with --yes to force a restart.")
        if not yes:
            return 0

    if not yes:
        ip = _primary_ip() or "your-mac-ip"
        print("This lets devices on your home network (your phone/tablet) reach Mongoose.")
        print(f"It will make the server listen on all interfaces (so the phone can connect to {ip}:{port}),")
        print("require an access token on each request, and restart the server.")
        print("Your Mongoose stays on your own network, this does not expose it to the internet.")
        ans = input("Enable phone access now? [Y/n] ").strip().lower()
        if ans in ("n", "no"):
            print("Left unchanged (still loopback-only). Re-run when ready: mongoose enable-phone")
            return 1

    updates = {"MONGOOSE_HOST": "0.0.0.0", "MONGOOSE_ALLOW_REMOTE": "true"}
    if not token:
        token = secrets.token_urlsafe(24)
        updates["MONGOOSE_API_TOKEN"] = token
        print("Generated an access token for your devices.")
    write_env(updates)
    print("Wrote phone-access settings to .env.")

    # Restart so uvicorn rebinds to 0.0.0.0 (a running server can't change its listen address).
    # CRITICAL: use the ABSOLUTE install dir as cwd and for log paths. Without cwd=, a restart
    # launched from ~/Downloads starts the server with the wrong working directory, causing every
    # relative path (data/logs, .env, data/mongoose.sqlite3) to resolve to the wrong place , exactly
    # the symptoms reported (logs 404, pairing fails, settings lost).
    print("Restarting Mongoose so the change takes effect...")
    from .service import _repo_root, _log_dir
    install_dir = _repo_root()
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([sys.executable, "-m", "mongoose.cli", "stop"],
                       capture_output=True, timeout=30, cwd=str(install_dir))
    except Exception:
        pass
    # Start detached with absolute cwd + absolute log path.
    try:
        log_file = log_dir / "app.log"
        with open(log_file, "ab") as log:
            subprocess.Popen([sys.executable, "-m", "mongoose.cli", "serve", "--port", str(port)],
                             stdout=log, stderr=log, start_new_session=True,
                             cwd=str(install_dir))
    except Exception as exc:  # noqa: BLE001
        print(f"Couldn't auto-restart ({exc}). Start it yourself: mongoose serve")
        return 1

    ip = _primary_ip() or "your-mac-ip"
    print("\nPhone access is on. Next:")
    print(f"  1. On your phone, open the cockpit Devices tab pairing link (it points at {ip}:{port}).")
    print("  2. The pairing page should now connect instead of 'connection refused'.")
    print("\nTo turn this back off later: set MONGOOSE_HOST=127.0.0.1 and MONGOOSE_ALLOW_REMOTE=false, then restart.")
    return 0
