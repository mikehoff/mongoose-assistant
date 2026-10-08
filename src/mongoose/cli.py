from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib

from .db import init_db
from .services.email_index import sync_imap
from .services.file_index import index_files
from .services.weather import weather_summary


def _profile_text(text: str) -> str:
    """CLI copy and command names with the site-profile placeholders filled in. A broken profile must not
    take the whole command line down with it (doctor and serve still have to run), so on error the text
    is returned as written."""
    try:
        from .settings import render_profile_copy
        return render_profile_copy(text)
    except Exception:
        return text


def _delight_interest_examples() -> str:
    """The interest keys the delight library actually has (the site profile's own, when it has them)."""
    try:
        from .services.delight import _interests
        return ", ".join(_interests())
    except Exception:
        return "an interest from your delight library"


def _profile_blocks() -> list[str]:
    try:
        from . import profile as _profile
        return _profile.block_codes()
    except Exception:
        return ["<block>"]


class _ProfileCommands:
    """add_parser proxy for commands whose NAME carries the RTM slug ("build-plan-{{rtm.slug}}"). The
    template stays a literal in add_parser("...") (install_check and the release manifest scan for those
    literals), while argparse registers the rendered, install-specific name."""

    def __init__(self, sub) -> None:
        self._sub = sub

    def add_parser(self, name: str, **kw):
        if isinstance(kw.get("help"), str):
            kw["help"] = _profile_text(kw["help"])
        return self._sub.add_parser(_profile_text(name), **kw)


def _setup_whatsapp_capability() -> None:
    """Configure the WhatsApp (WAHA) capability: check Docker, generate an HMAC key,
    write the provider env, and print the link steps. Doesn't auto-start the
    container (the user runs docker compose) but leaves everything ready."""
    import secrets
    import shutil
    from .setup_wizard import write_env, _read_env
    print("Setting up WhatsApp via the WAHA Docker sidecar.\n")
    if shutil.which("docker") is None:
        print("  [warn] Docker not found on PATH. Install Docker Desktop / Colima first,")
        print("         then re-run this. Continuing to write config anyway.\n")
    env = _read_env()
    from . import profile as _profile
    owner_session = _profile.get("owner.whatsapp_session")
    key = env.get("WHATSAPP_HOOK_HMAC_KEY") or secrets.token_hex(32)
    updates = {
        "WHATSAPP_PROVIDER": "waha",
        "WAHA_URL": env.get("WAHA_URL", "http://127.0.0.1:3000"),
        # Dual-account defaults: the owner's inbox (read) and Mongoose's own number (send). A single-session
        # install that only had WHATSAPP_SESSION keeps working; these just name the two roles.
        "WHATSAPP_OWNER_SESSION": env.get("WHATSAPP_OWNER_SESSION", owner_session),
        "WHATSAPP_MONGOOSE_SESSION": env.get("WHATSAPP_MONGOOSE_SESSION", "mongoose"),
        "WHATSAPP_HOOK_HMAC_KEY": key,
    }
    write_env(updates)
    print(f"  Wrote WhatsApp config to .env (provider=waha, dual sessions {updates['WHATSAPP_OWNER_SESSION']}/mongoose, HMAC key generated).")
    print("\n  Next:")
    print("    1. docker compose -f docker-compose.whatsapp.yml up -d")
    print(f"    2. Link {_profile.owner_name()}'s inbox:     mongoose whatsapp connect --role owner_read")
    print("    3. Link Mongoose sender:  mongoose whatsapp connect --role mongoose_send")
    print("    4. mongoose whatsapp status   (want both WORKING)")
    print("\n  Outgoing messages always require your explicit approval.")


def _print_alert(a: dict) -> None:
    """One-line display of a time alert for the CLI list views."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(a["fire_at"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        when = dt.astimezone().strftime("%a %d %b %H:%M")
    except Exception:
        when = a.get("fire_at", "?")
    label = a.get("label") or {"alarm": "alarm", "timer": "timer", "reminder": "reminder"}.get(a["kind"], "")
    rec = f" (every {a['recurrence']})" if a.get("recurrence") else ""
    status = a.get("status", "")
    badge = "" if status in ("scheduled", "confirmed") else f" [{status}]"
    tgt = f" -> {', '.join(a['targets'])}" if a.get("targets") else ""
    print(f"  {a['id']}  {a['kind']:8} {when}{rec}  {label}{tgt}{badge}")


def _wd_install_agent() -> str:
    """Write a launchd LaunchAgent plist for the EXTERNAL server watchdog into ~/Library/LaunchAgents.
    KeepAlive keeps the watchdog itself alive; the watchdog in turn keeps the SERVER alive by probing
    /health/live and restarting it if the event loop wedges. Returns the plist path (not loaded here;
    the caller prints the bootstrap command)."""
    import sys as _sys
    from pathlib import Path as _P
    label = "com.mongoose.server-watchdog"
    agents = _P.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{label}.plist"
    root = _P.cwd()
    venv_bin = _P(_sys.executable).parent / "mongoose"
    if venv_bin.exists():
        argv = [str(venv_bin), "server-watchdog"]
    else:
        argv = [_sys.executable, "-m", "mongoose.cli", "server-watchdog"]
    # Logs must land in the canonical brain tree, not `cwd/data/logs`: the LaunchAgent can be
    # (re)installed from any cwd, and a cwd-relative StandardOut/Err path scatters the watchdog's own
    # logs into stray per-folder data trees (the split-brain class). Resolve absolute via settings.
    try:
        from .settings import get_settings as _get_settings
        logs = _P(_get_settings().data_dir) / "logs"
    except Exception:
        logs = _P.home() / ".mongoose" / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    args_xml = "\n".join(f"        <string>{a}</string>" for a in argv)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Mongoose EXTERNAL server watchdog. Probes /health/live and restarts the server if its event loop
     wedges (the failure the in-process watchdog can't recover). KeepAlive keeps THIS watchdog alive.
     Manage with: mongoose server-watchdog --install, or launchctl directly. -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>WorkingDirectory</key>
    <string>{root}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{logs / "server-watchdog.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{logs / "server-watchdog.err.log"}</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""
    plist.write_text(content)
    return str(plist)


def _command_looks_like_mongoose_server(command: str) -> bool:
    """Return True only for commands that look like a Mongoose API server.

    Used by `mongoose stop` and the installers so a stale pidfile or a random process on the default
    port cannot be killed merely because it exists, a runtime ownership claim ("restart Mongoose")
    must prove ownership before acting. Intentionally narrow: Mongoose starts either as
    `mongoose serve`, `python -m mongoose.cli serve`, or uvicorn running `mongoose.app:app`.
    """
    low = (command or "").lower()
    if "mongoose.app:app" in low and "uvicorn" in low:
        return True
    if "mongoose.cli" in low and " serve" in low:
        return True
    if "mongoose" in low and " serve" in low:
        return True
    return False


def _process_command_for_pid(pid: int) -> str:
    """Best-effort process command lookup. Empty string means unknown."""
    try:
        import subprocess as _sp
        out = _sp.run(["ps", "-p", str(pid), "-o", "command="],
                      capture_output=True, text=True, timeout=2)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _pid_listening_ports(pid: int) -> list[int]:
    """Ports this PID is listening on (TCP LISTEN sockets), via lsof. Empty list if lsof is
    unavailable or the PID holds none. Best-effort; never raises."""
    try:
        import subprocess as _sp
        out = _sp.run(["lsof", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-n", "-P"],
                      capture_output=True, text=True, timeout=3)
        ports: list[int] = []
        for line in (out.stdout or "").splitlines()[1:]:
            parts = line.rsplit(":", 1)
            if len(parts) == 2:
                tail = parts[1].split()[0]
                if tail.isdigit():
                    ports.append(int(tail))
        return sorted(set(ports))
    except Exception:
        return []


def _pid_answers_as_mongoose(pid: int) -> bool:
    """Ask the PID's OWN bound port whether it identifies as Mongoose, instead of trusting how
    `ps` renders the command line.

    Proven necessary in the field: on a Homebrew framework Python build, `ps -o command=` for a
    running Mongoose server reported the bare interpreter path (no "mongoose", no "serve"), so
    the string-match check below silently returned False for the process forever. That meant
    `mongoose stop`, the watchdog, and every installer's version-mismatch teardown could never
    identify and kill it, no matter how many updates were applied on top. A foreign process
    essentially never answers a probe with {"name": "mongoose"}, so this is safe to accept as
    proof on its own, and it does not depend on any particular interpreter's argv rendering.
    """
    for port in _pid_listening_ports(pid):
        for scheme in ("https", "http"):
            try:
                import urllib.request as _ur
                import ssl as _ssl
                ctx = _ssl._create_unverified_context() if scheme == "https" else None
                req = _ur.Request(f"{scheme}://127.0.0.1:{port}/health")
                with _ur.urlopen(req, timeout=1.5, context=ctx) as resp:
                    body = resp.read(400).decode("utf-8", "replace")
                    if '"name":"mongoose"' in body or '"name": "mongoose"' in body:
                        return True
            except Exception:
                continue
    return False


def _pid_looks_like_mongoose_server(pid: int) -> bool:
    if _command_looks_like_mongoose_server(_process_command_for_pid(pid)):
        return True
    return _pid_answers_as_mongoose(pid)


def _cmd_start_docker() -> int:
    """Launch Docker Desktop on macOS and wait for the daemon, then start the optional ntfy/SearXNG
    containers. Honest about platform: on non-macOS or if Docker isn't installed, it says what to do
    rather than pretending. Docker is OPTIONAL, this is a convenience, not a requirement."""
    import platform
    import shutil
    import subprocess
    import time

    if not shutil.which("docker"):
        print("Docker isn't installed. It's optional, for phone push without it, run: mongoose setup-ntfy")
        print("To install Docker Desktop: https://www.docker.com/products/docker-desktop/")
        return 1

    # already up?
    if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
        print("Docker is already running.")
    else:
        if platform.system() == "Darwin":
            print("Starting Docker Desktop, this can take 20-40s on first launch...")
            subprocess.run(["open", "-a", "Docker"], capture_output=True)
        else:
            print("Docker isn't running. Start the Docker daemon (e.g. `sudo systemctl start docker`) and re-run.")
            return 1
        # poll for the daemon
        ok = False
        for _ in range(60):  # up to ~60s
            if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
                ok = True
                break
            time.sleep(1)
        if not ok:
            print("Docker didn't come up in time. Open Docker Desktop manually, wait for it to say 'running', then re-run.")
            print("(Phone push also works without Docker: mongoose setup-ntfy)")
            return 1
        print("Docker is up.")

    # start the optional containers if a compose file is present
    from pathlib import Path
    if Path("docker-compose.yml").exists():
        print("Starting the optional local containers (ntfy push, SearXNG search)...")
        r = subprocess.run(["docker", "compose", "up", "-d", "ntfy", "searxng"], capture_output=True, text=True)
        if r.returncode == 0:
            print("ntfy and SearXNG are running locally.")
        else:
            print("Docker is up but the containers didn't start:")
            print((r.stderr or r.stdout or "").strip()[:400])
            return 1
    if Path("docker-compose.whatsapp.yml").exists():
        print("Starting the WhatsApp sidecar (WAHA)...")
        rw = subprocess.run(["docker", "compose", "-f", "docker-compose.whatsapp.yml", "up", "-d"],
                            capture_output=True, text=True)
        if rw.returncode == 0:
            print("WAHA is running. Link WhatsApp from the cockpit WhatsApp tab (Connect, then scan the QR).")
        else:
            print("WAHA didn't start (WhatsApp is optional):")
            print((rw.stderr or rw.stdout or "").strip()[:300])
    return 0


# Internal/plumbing top-level commands. They still WORK (scripts and the server call them), but they
# are hidden from `mongoose --help` and the bare-`mongoose` overview so a human opening a terminal is
# not buried under 100 commands with no explanation. Keep this list to genuine plumbing: process/infra
# entrypoints, env surgery, dev-build planners, low-level sync primitives, and back-compat aliases.
_HIDDEN_TOPLEVEL = {
    "init-db", "_install-step", "_install-phase", "server-watchdog", "simulate-surpriser",
    "merge-env", "dedupe-env", "bus-send", "bus", "bus-read", "bus-search", "bus-reindex",
    "module-index", "reingest", "protocol-v3", "crawler", "rehearse",
    "sync-imap", "sync-google-calendar", "sync-gmail", "sync-ms-calendar", "sync-ms-todo", "sync-ms-mail",
    "build-plan-{{rtm.slug}}", "build-plan-{{rtm.slug}}-intake", "build-plan-{{rtm.slug}}-governance",
    "build-plan-{{rtm.slug}}-management", "build-plan-{{rtm.slug}}-rtm-trust-reconciliation",
    "load-real-data", "load-real-demands", "seed-flats", "attach-demand-pdf",
    "alpha-check", "release-check", "build-recover", "pi-build", "install-certbot",
}


def _print_overview() -> None:
    """The friendly landing shown when someone runs `mongoose` with no arguments: a short, grouped,
    EXPLAINED set of the commands a person actually uses, plus where to go for the rest. Replaces
    argparse's default 'error: the following arguments are required' wall of every subcommand."""
    print(
        "Mongoose: your local, private household assistant.\n"
        "Most things happen in the Cockpit (the web app) and on your phone. This command line is\n"
        "mainly for first-time setup and for running the server.\n\n"
        "Common commands:\n"
        "  mongoose serve           Run the Mongoose server\n"
        "  mongoose status          Is the server running? (version, port, PID)\n"
        "  mongoose setup           Configure integrations (guided wizard)\n"
        "  mongoose update          Update to a new version (keeps your data and memory)\n"
        "  mongoose backup create   Back up your brain and config\n"
        "  mongoose doctor          Safe configuration and readiness checks\n"
        "  mongoose readiness       What is ready, stale, or needs you\n\n"
        "Run 'mongoose <command> --help' for one command, or 'mongoose --help' for the full list."
    )


def main() -> None:
    import sys
    # Bare `mongoose` gives the friendly overview, not a 100-command error wall.
    if len(sys.argv) == 1:
        _print_overview()
        return
    parser = argparse.ArgumentParser(
        prog="mongoose",
        description="Mongoose local household assistant. Most tasks live in the Cockpit (the web "
                    "app); this CLI is for setup and running the server.",
        epilog="Tip: run 'mongoose' with no arguments to see the common commands.")
    # metavar collapses the giant {a,b,c,...} brace list in usage/help into a clean placeholder.
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    sub.add_parser("init-db")

    sub.add_parser("checkup", help="Configuration checkup: can the assistant run, what's configured, what's not. NOT release QA (that's scripts/qa_repo.sh).")
    sub.add_parser("release-check", help="Alias for checkup (the old name over-promised: it reads like release QA but reports runtime configuration)")
    sub.add_parser("alpha-check", help="Alias for checkup (back-compat)")  # kept so existing scripts don't break
    sub.add_parser("connectors", help="Live-probe every connector (Gmail/Calendar/Drive/Microsoft/IMAP/HA/Ollama) to prove it actually works now, not just that it's configured")
    cc_p = sub.add_parser("claude-code", help="Claude Code integration: discover local + Pi instances, billing posture, and cost-aware routing (free local Gemma vs Claude Code)")
    cc_p.add_argument("cc_cmd", nargs="?", default="status", choices=["status", "discover", "route"],
                      help="status=full report incl. billing traps; discover=where Claude Code lives; route=cost-aware advice")
    cc_p.add_argument("--task", default="general", help="with route: task kind (general/household/search/code_change/build)")
    doctor_p = sub.add_parser("doctor", help="Show safe local configuration and readiness checks")
    doctor_p.add_argument("--capabilities", action="store_true", help="Show only the capability/dependency report")
    doctor_p.add_argument("--install-root", action="store_true", help="Check install/source-tree coherence (catches stale or mixed installs)")
    doctor_p.add_argument("--models", action="store_true", help="Report the conversation-owner/research-worker architecture and whether the pair is co-resident now (probes Ollama /api/ps; reports unknown if unreachable)")

    setup = sub.add_parser("setup", help="Interactive wizard to configure integrations")
    setup.add_argument("--status", action="store_true", help="Print integration status as JSON and exit")
    setup.add_argument("--capability", metavar="NAME", help="Install dependencies for one capability (e.g. google, voice) and exit")
    setup.add_argument("--all", action="store_true", help="Install dependencies for ALL capabilities and exit")
    setup.add_argument("--yes", action="store_true", help="Don't prompt; assume yes (for --capability/--all)")

    im = sub.add_parser("install-missing", help="Install missing optional libraries for all capabilities (into the active venv)")
    im.add_argument("--yes", action="store_true", help="Don't prompt; install without confirmation")
    im.add_argument("--dry-run", action="store_true", help="Show what would be installed, install nothing")

    sub.add_parser("setup-embeddings",
        help="Download and install the multilingual embeddings model (118M, Apache-2.0) for semantic search and cross-language understanding")

    vd = sub.add_parser("verify-data", help="Check row-level integrity of the local corpus (read-only)")
    vd.add_argument("--json", action="store_true", help="Output full findings as JSON")

    rd = sub.add_parser("repair-data", help="Apply a narrow, explicit data repair (dry-run by default)")
    rd.add_argument("action", help="Repair action, e.g. dedupe-memory, dedupe-files, dedupe-email")
    rd.add_argument("--apply", action="store_true", help="Actually apply (default is dry-run)")

    # --- social / ritual layer ---
    rou = sub.add_parser("routine", help="Named multi-step routines for the local agentic loop (e.g. 'wind down', 'morning')")
    rou_sub = rou.add_subparsers(dest="routine_cmd")
    rou_sub.add_parser("list", help="List saved routines")
    rsave = rou_sub.add_parser("save", help="Create or update a routine")
    rsave.add_argument("name")
    rsave.add_argument("recipe", help="What the routine should do (natural language)")
    rsave.add_argument("--trigger", help="Optional trigger phrase that runs it")
    rrun = rou_sub.add_parser("run", help="Run a routine now")
    rrun.add_argument("name")
    rdel = rou_sub.add_parser("delete", help="Delete a routine")
    rdel.add_argument("name")

    rit = sub.add_parser("ritual", help="Run a useful micro-practice (physio, breathing, journal, etc.)")
    rit.add_argument("name", nargs="?", help="ritual name or phrase; omit to list")
    rit.add_argument("--done", type=int, metavar="LOG_ID", help="mark a started ritual complete")
    rit.add_argument("--stats", action="store_true", help="show completion stats")

    wc = sub.add_parser("what-changed", help="Cross-domain delta since you last looked")
    wc.add_argument("--peek", action="store_true", help="don't reset the checkpoints (just look)")

    diary = sub.add_parser("diary", help="Mongoose's local activity log")
    diary.add_argument("--week", action="store_true", help="weekly roll-up instead of today")

    wa_p = sub.add_parser("whatsapp", help="WhatsApp: search, draft (approval-gated), list/approve drafts")
    wa_sub = wa_p.add_subparsers(dest="wa_command")
    s_search = wa_sub.add_parser("search", help="search indexed WhatsApp messages")
    s_search.add_argument("query")
    wa_sub.add_parser("status", help="WhatsApp session state (WORKING / SCAN_QR_CODE / ...)")
    wa_connect = wa_sub.add_parser("connect", help="Start a WhatsApp account session so it begins authenticating")
    wa_connect.add_argument("--role", default="owner_read", choices=["owner_read", "mongoose_send"],
                            help=_profile_text("owner_read = {{owner.name}}'s inbox; mongoose_send = Mongoose's notification number"))
    wa_test = wa_sub.add_parser("test", help="Send a test message to your own number (via the approved outbox)")
    wa_test.add_argument("--to", default="", help="your number in international format, e.g. 447700900000")
    wa_sub.add_parser("link", help="show how to link your WhatsApp (QR via WAHA)")
    s_from = wa_sub.add_parser("from", help="recent messages from a contact")
    s_from.add_argument("contact")
    wa_sub.add_parser("waiting", help="who you haven't replied to")
    s_draft = wa_sub.add_parser("draft", help="draft a message (does NOT send)")
    s_draft.add_argument("contact")
    s_draft.add_argument("body")
    wa_sub.add_parser("drafts", help="list pending drafts")
    s_approve = wa_sub.add_parser("approve", help="approve and send a draft (requires the draft id)")
    s_approve.add_argument("draft_id", type=int)
    s_cancel = wa_sub.add_parser("cancel", help="cancel a pending draft")
    s_cancel.add_argument("draft_id", type=int)

    upd = sub.add_parser("update", help="Update to a new version, preserving your config/data/memory")
    upd.add_argument("source", nargs="?", help="Path to the new version folder or .zip")
    upd.add_argument("--rollback", action="store_true", help="restore the most recent backup")
    upd.add_argument("--list-backups", action="store_true", help="list available backups")
    upd.add_argument("--raw", action="store_true", help="run the bare update script without the guided preflight/postflight")
    upd.add_argument("--install-missing", action="store_true", help="auto-install any new capability libraries without prompting")
    upd.add_argument("--no-install", action="store_true", help="never install libraries during update (just report what's missing)")
    upd.add_argument("--stage", action="store_true", help="stage the source into data/update_stages; do not apply it")
    upd.add_argument("--stage-asset", metavar="ASSET_ID", help="stage a build uploaded from the phone (by asset id), no re-upload needed")
    upd.add_argument("--fetch", action="store_true", help="self-update: fetch the next update from the configured source (MONGOOSE_UPDATE_URL / MONGOOSE_UPDATE_GITHUB_REPO) and stage it")
    upd.add_argument("--stages", action="store_true", help="list staged update candidates")
    upd.add_argument("--check-stage", metavar="ID", help="run safe static checks against a staged candidate")
    upd.add_argument("--runtime-smoke", action="store_true", help="with --check-stage: also boot the candidate on a temp port and verify it serves correct identity before applying")
    upd.add_argument("--rollback-drill", metavar="ID", help="dry-run the rollback path for a staged candidate (verifies backup + script + checks); applies nothing")
    upd.add_argument("--approve-stage", metavar="ID", help="approve a staged candidate after review")
    upd.add_argument("--apply-stage", metavar="ID", help="apply an approved, checked staged candidate")
    upd.add_argument("--yes", action="store_true", help="confirm apply-stage without prompting")
    sim = sub.add_parser("simulate-surpriser",
                         help="Replay N days through the surpriser (safe: temp DB, no real pushes) to gate the 'not noisy' promise")
    sim.add_argument("--days", type=int, default=14, help="how many days to simulate")
    sim.add_argument("--no-seed", action="store_true", help="don't inject the recurring-pattern seed (test true silence)")
    sim.add_argument("--source-db", default="", help="copy an existing DB (e.g. your real data/mongoose.sqlite3) as the starting point; never mutated")
    sim.add_argument("--hour", type=int, default=None, help="pin the simulated time-of-day (0-23); seeded runs default to 14:00 so they test shape suppression, not quiet hours")
    sim.add_argument("--scheduler-path", action="store_true", help="drive each day through the real scheduler (run_due_jobs) instead of calling the picker directly")
    sub.add_parser("liveness", help="Is the system actually alive or quietly rotting? Flags stale sync, dead jobs, dropped sessions, aging tokens, with the fix for each")
    vh = sub.add_parser("validate-home", help="Live-test the home control path against a SAFE test entity (HA-first, Alexa fallback, outcome verification). For the on-Mac/Pi validation pass.")
    vh.add_argument("entity_id", help="a SAFE test entity to actuate, e.g. light.spare_room")
    vh.add_argument("--action", default="toggle", help="action to perform (default: toggle, the least disruptive)")
    istep = sub.add_parser("_install-step", help="(internal) record an install-progress step for the /install screen")
    istep.add_argument("key")
    istep.add_argument("status")
    istep.add_argument("--detail", default="")
    istep.add_argument("--begin", action="store_true", help="reset progress before recording")
    iphase = sub.add_parser("_install-phase", help="(internal) print the install-phase state machine for the wrapper to branch on")
    iphase.add_argument("--field", default="", help="print just one field (phase/ready_proven/complete/watchdog_ok/watchdog_detail) for easy shell capture")
    rdy = sub.add_parser("readiness", help="What can I use right now? Evidence-grounded status of every source, surface and service, what's ready, stale, broken, or needs you")
    rdy.add_argument("--voice", action="store_true", help="short spoken-style answer (summary + top gaps + one action) instead of the full list")
    rdy.add_argument("--needs-me", action="store_true", help="just the things that need you, ranked")
    rdy.add_argument("--refresh", action="store_true", help="force a fresh check now (don't use the cached snapshot)")

    # Time Alerts: alarms (wall-clock), timers (countdown), reminders (wall-clock + text).
    alarm_p = sub.add_parser("alarm", help="Set, list or cancel alarms (Mongoose-owned, delivered to your phone; speaker announce when enabled)")
    alarm_sub = alarm_p.add_subparsers(dest="alarm_cmd")
    al_set = alarm_sub.add_parser("set", help="Set an alarm, e.g. 'at 7am tomorrow' or '6:45 on weekdays'")
    al_set.add_argument("when", nargs="+", help="the time phrase (and optional label)")
    al_set.add_argument("--on", dest="targets", action="append", help="speaker/group to also announce on (repeatable)")
    alarm_sub.add_parser("list", help="List upcoming alarms")
    al_cancel = alarm_sub.add_parser("cancel", help="Cancel an alarm by id (or 'all')")
    al_cancel.add_argument("id", help="alarm id, or 'all' to cancel every alarm (asks to confirm)")
    al_cancel.add_argument("--yes", action="store_true", help="confirm cancel-all without prompting")

    timer_p = sub.add_parser("timer", help="Set, list or cancel countdown timers")
    timer_sub = timer_p.add_subparsers(dest="timer_cmd")
    ti_set = timer_sub.add_parser("set", help="Set a timer, e.g. '20 minutes' or 'in 1h30'")
    ti_set.add_argument("when", nargs="+", help="the duration phrase (and optional label)")
    ti_set.add_argument("--on", dest="targets", action="append", help="speaker/group to also announce on")
    timer_sub.add_parser("list", help="List active timers")
    ti_cancel = timer_sub.add_parser("cancel", help="Cancel a timer by id (or 'all')")
    ti_cancel.add_argument("id", help="timer id, or 'all'")
    ti_cancel.add_argument("--yes", action="store_true", help="confirm cancel-all without prompting")

    rem_p = sub.add_parser("reminder", help="Set, list or cancel reminders (carry a text label)")
    rem_sub = rem_p.add_subparsers(dest="reminder_cmd")
    re_set = rem_sub.add_parser("set", help="Set a reminder, e.g. 'to call mum at 3pm'")
    re_set.add_argument("when", nargs="+", help="the reminder text + time phrase")
    re_set.add_argument("--on", dest="targets", action="append", help="speaker/group to also announce on")
    rem_sub.add_parser("list", help="List upcoming reminders")
    re_cancel = rem_sub.add_parser("cancel", help="Cancel a reminder by id (or 'all')")
    re_cancel.add_argument("id", help="reminder id, or 'all'")
    re_cancel.add_argument("--yes", action="store_true", help="confirm cancel-all without prompting")

    sub.add_parser("alerts", help="List all upcoming alarms, timers and reminders together")

    intro_p = sub.add_parser("intro", help="Mongoose introduces itself and what it can do (adapts to what's connected)")

    sub.add_parser("rehearse", help="Dress rehearsal: exercise every module end-to-end on synthetic data and report what's ready, what needs your Mac, and what's intentionally off")

    diag_p = sub.add_parser("diagnostics", help="Two-lane diagnostics: status of accumulated events, and export a report bundle (.md + .json) to upload to Claude")
    diag_sub = diag_p.add_subparsers(dest="diag_cmd")
    diag_sub.add_parser("status", help="Is a report ready? Show accumulated weight, age, lane counts")
    dexp = diag_sub.add_parser("export", help="Export the report bundle (.md + .json) and reset the accumulator")
    dexp.add_argument("--no-reset", action="store_true", help="export without marking events reported")
    drec = diag_sub.add_parser("recent", help="Show recent events")
    drec.add_argument("--limit", type=int, default=30)
    drec.add_argument("--lane", choices=["fast", "slow"], help="filter by lane")

    unstuck_p = sub.add_parser("unstuck", help="When you're stuck or grinding: names it and offers one active, playful thing (never 'rest')")

    delight_p = sub.add_parser("delight", help="A light moment: a fun fact or joke tied to your interests")
    delight_p.add_argument("kind", nargs="?", default="auto", choices=["auto", "fact", "joke"])
    delight_p.add_argument("--interest", default="", help="e.g. " + _delight_interest_examples())
    delight_p.add_argument("--no-model", action="store_true", help="use only shipped nuggets, skip the model tier")

    rtm_p = sub.add_parser("rtm", help=_profile_text("{{rtm.name}} RTM intake: classify an email, draft a reply (draft-only), manage drafts. Live mailbox/send stay disabled until enabled on the Mac."))
    rtm_sub = rtm_p.add_subparsers(dest="rtm_cmd")
    rtm_sub.add_parser("status", help="Operational status, including whether live mailbox/send are on")
    rtm_sub.add_parser("cases", help="List open RTM cases")
    rtm_sub.add_parser("drafts", help="List pending draft replies")
    rivm = rtm_sub.add_parser("import-voicemail", help="Import HA voicemail add-on JSON/TXT transcripts as cases + drafts (reads files only, never a mailbox)")
    rivm.add_argument("root", nargs="?", default=_profile_text("{{voicemail.ha_share_dir}}"), help="voicemail dir")
    rsearch = rtm_sub.add_parser("search", help="Full-text search across RTM email + voicemail intake")
    rsearch.add_argument("query")
    rsearch.add_argument("--limit", type=int, default=20)
    rlist = rtm_sub.add_parser("list", help="List recent intake items across all cases")
    rlist.add_argument("--limit", type=int, default=50)

    obl = rtm_sub.add_parser("obligations", help="Statutory obligations calendar: forward legal deadlines (acquisition, accounts, insurance, s.20) with countdowns")
    obl.add_argument("--s20-noi", help="Compute the s.20 consultation timeline from a Notice of Intention date (YYYY-MM-DD)")
    sec = rtm_sub.add_parser("secretary", help="Virtual company secretary: compliance deadlines, reminders, guidance and artefacts")
    sec_sub = sec.add_subparsers(dest="secretary_cmd")
    sec_sub.add_parser("status", help="Show the current compliance tracker")
    sec_sub.add_parser("discover", help="Compute/update deadline reminders from the company profile")
    scfg = sec_sub.add_parser("configure", help="Set RTM company facts used for deadline computation")
    scfg.add_argument("--company-name")
    scfg.add_argument("--company-number")
    scfg.add_argument("--incorporation-date")
    scfg.add_argument("--accounting-reference-date", help="Accounting reference date / financial year end, YYYY-MM-DD")
    scfg.add_argument("--confirmation-statement-date", help="Last/current confirmation statement date, YYYY-MM-DD")
    scfg.add_argument("--corporation-tax-period-end", help="Accounting period end for Corporation Tax, YYYY-MM-DD")
    scfg.add_argument("--dormant-for-corporation-tax", action="store_true")
    scfg.add_argument("--vat-registered", action="store_true")
    scfg.add_argument("--payroll-registered", action="store_true")
    scfg.add_argument("--notes", default="")
    sdone = sec_sub.add_parser("done", help="Mark a compliance item done")
    sdone.add_argument("item_id")
    sdone.add_argument("--note", default="")
    sg = sec_sub.add_parser("guidance", help="Step-by-step guidance for one item")
    sg.add_argument("item_id")
    scal = sec_sub.add_parser("calendar", help="Export compliance reminders as CSV")
    scal.add_argument("path")
    spart = sec_sub.add_parser("artefacts", help="Write markdown preparation artefacts for all current items")
    spart.add_argument("dir")

    portal = rtm_sub.add_parser("portal", help=(
        "Leaseholder portal: readiness, snapshot, publish, claim processing, issue sync, PDF. "
        "Does NOT move money, confirm payment, send demands, or alter bank records."))
    portal_sub = portal.add_subparsers(dest="portal_cmd")
    portal_sub.add_parser("readiness",        help="Full portal readiness report")
    portal_sub.add_parser("snapshot",         help="Build minimised portal snapshot (preview, does not publish)")
    portal_sub.add_parser("publish",          help="Build and push snapshot to WordPress")
    portal_sub.add_parser("pull-claims",      help="Pull encrypted pending claims from WordPress")
    portal_sub.add_parser("process-claims",   help="Decrypt, validate and push claim decisions")
    portal_sub.add_parser("pull-issues",      help="Pull leaseholder issues into RTM intake")
    portal_sub.add_parser("generate-keypair", help="Generate NaCl claim encryption keypair")
    portal_sub.add_parser("public-key",       help="Show public key to publish to WordPress")
    portal_sub.add_parser("generate-signing-key",  help="Generate Ed25519 snapshot signing key (private stays on the Mac)")
    portal_sub.add_parser("signing-public-key",    help="Show Ed25519 public key to paste into the WordPress RTM Settings")
    portal_sub.add_parser("check-drift",           help="Compare WordPress snapshot hash against the last one Mongoose pushed")
    portal_sub.add_parser("seed-flats",       help=_profile_text(f"Seed all {9 * len(_profile_blocks())} {{{{rtm.name}}}} flats in the database"))
    _pdf_ap = portal_sub.add_parser("attach-demand-pdf", help="Attach a demand PDF to a portal demand record")
    _pdf_ap.add_argument("--document-number", required=True)
    _pdf_ap.add_argument("--path",            required=True)
    books = rtm_sub.add_parser("books", help="Bookkeeper: service charge / reserve fund trust ledger and reconciliation")
    book_sub = books.add_subparsers(dest="books_cmd")
    book_sub.add_parser("summary", help="Show trust fund balances, reconciliation status and weekly questions")
    book_sub.add_parser("weekly", help="Ask the weekly bookkeeping questions")
    bop = book_sub.add_parser("opening", help="Record an opening ledger balance")
    bop.add_argument("fund")
    bop.add_argument("amount")
    bop.add_argument("--date")
    bop.add_argument("--notes", default="")
    bbal = book_sub.add_parser("balance", help="Record weekly bank balance for a fund")
    bbal.add_argument("fund")
    bbal.add_argument("amount")
    bbal.add_argument("--date")
    bbal.add_argument("--account-name", default="")
    bbal.add_argument("--notes", default="")
    btx = book_sub.add_parser("tx", help="Record an income/expense transaction")
    btx.add_argument("fund")
    btx.add_argument("direction", choices=["in", "out"])
    btx.add_argument("amount")
    btx.add_argument("description", nargs="+")
    btx.add_argument("--date")
    btx.add_argument("--category", default="uncategorised")
    btx.add_argument("--counterparty", default="")
    btx.add_argument("--reference", default="")
    btx.add_argument("--notes", default="")
    btr = book_sub.add_parser("transfer", help="Record a transfer between service charge and reserve funds")
    btr.add_argument("from_fund")
    btr.add_argument("to_fund")
    btr.add_argument("amount")
    btr.add_argument("--date")
    btr.add_argument("--description", default="Transfer between trust funds")
    btr.add_argument("--reference", default="")
    btr.add_argument("--notes", default="")
    book_sub.add_parser("reconcile", help="Compare ledger balances with latest weekly bank balances")
    book_sub.add_parser("chart", help="Text visualisation of fund balances and reconciliation")
    bex = book_sub.add_parser("export", help="Export ledger as bookkeeper-ready CSV")
    bex.add_argument("path")
    bex.add_argument("--fund", default="")

    # Receipts and trust reconciliation (turnstile model). Records and checks only; never moves money.
    trust = rtm_sub.add_parser("trust", help=("Receipts + turnstile trust reconciliation across the "
                                              "running and two trust accounts. Records and checks only; never moves money."))
    trust_sub = trust.add_subparsers(dest="trust_cmd")
    tr_rb = trust_sub.add_parser("receipt-build", help="Build a receipt for a fully paid demand (records only; never moves money)")
    tr_rb.add_argument("--flat", required=True)
    tr_rb.add_argument("--document", required=True)
    tr_rb.add_argument("--payment-ref", default="")
    tr_rb.add_argument("--amount", required=True, help="Amount received, in pounds")
    tr_rb.add_argument("--received", required=True, help="Date received, YYYY-MM-DD")
    tr_rb.add_argument("--method", default="Bank transfer")
    tr_rr = trust_sub.add_parser("receipt-render", help="Render a stored receipt to PDF (records only; never moves money)")
    tr_rr.add_argument("--flat", required=True)
    tr_rr.add_argument("--document", required=True)
    tr_rr.add_argument("--attach-demand", action="store_true")
    trust_sub.add_parser("reserve-position", help="Reserve collected/transferred/in-transit and trust balance (records only; never moves money)")
    tr_rec = trust_sub.add_parser("reconcile-weekly", help="Weekly reconciliation across the three accounts (records only; never moves money)")
    tr_rec.add_argument("--as-of", default="")
    tr_ts = trust_sub.add_parser("transfers-status", help="Sweep obligations and exposure flags (records only; never moves money)")
    tr_ts.add_argument("--as-of", default="")

    mgmt = rtm_sub.add_parser("manage", help="RTM management: demands, Metro reconciliation, arrears, payables, budget and evidence")
    mg = mgmt.add_subparsers(dest="manage_cmd")
    mg.add_parser("status", help="Show management status: demands, arrears, cashflow")
    mg.add_parser("dashboard", help="Director dashboard: next actions, review queue, arrears, evidence gaps")
    mg.add_parser("seed-flats", help=f"Create placeholder {'/'.join(_profile_blocks())} flat reference rows (no invented leaseholder names)")
    mg.add_parser("flats", help="List flat reference rows")
    mif = mg.add_parser("import-flats", help="Import/update flat reference rows from CSV")
    mif.add_argument("path")
    mid = mg.add_parser("import-demands", help="Import demand register rows from CSV; existing document numbers are skipped")
    mid.add_argument("path")
    mxp = mg.add_parser("export-pack", help="Export management CSV/JSON/TXT pack for director/bookkeeper/s.22/FTT prep")
    mxp.add_argument("dir")
    mxp.add_argument("--year", default="")
    mf = mg.add_parser("flat", help="Create/update one flat reference row")
    mf.add_argument("block")
    mf.add_argument("number")
    mf.add_argument("--name", default="")
    mf.add_argument("--address", default="")
    mf.add_argument("--apportionment", default="")
    mf.add_argument("--payment-reference", default="")
    mf.add_argument("--notes", default="")
    md = mg.add_parser("demand", help="Add a demand register row and immutable ledger debit")
    md.add_argument("document_number")
    md.add_argument("flat", help=f"e.g. {_profile_blocks()[0]}F1")
    md.add_argument("period")
    md.add_argument("amount")
    md.add_argument("--issue-date")
    md.add_argument("--due-date")
    md.add_argument("--delivery-method", default="")
    md.add_argument("--revision", type=int, default=0)
    md.add_argument("--supersedes", default="")
    md.add_argument("--schedule", default="sch1")
    md.add_argument("--notes", default="")
    ml = mg.add_parser("demands", help="List demands")
    ml.add_argument("--status", default="")
    ml.add_argument("--flat", default="")
    ml.add_argument("--limit", type=int, default=50)
    ms = mg.add_parser("statement", help="Per-flat running statement")
    ms.add_argument("flat")
    mb = mg.add_parser("metro-import", help="Import Metro Bank CSV and auto-allocate exact matches")
    mb.add_argument("path")
    mg.add_parser("review-payments", help="Show partial/unmatched/duplicate bank credits for review")
    ma = mg.add_parser("allocate", help="Manually allocate a bank transaction to one or more demands")
    ma.add_argument("bank_transaction_id")
    ma.add_argument("allocations", nargs="+", help=f"DOC:AMOUNT, e.g. 26-TO-DEC/{_profile_blocks()[0]}F1R0:901.23")
    mg.add_parser("arrears", help="Show overdue accounts and next recovery action")
    mr = mg.add_parser("reminder", help="Generate a draft reminder/recovery document for a demand")
    mr.add_argument("document_number")
    mr.add_argument("--type", default="")
    mrel = mg.add_parser("release-reminder", help="Log that a reminder was reviewed and released")
    mrel.add_argument("action_id")
    mrel.add_argument("method")
    mref = mg.add_parser("referral-pack", help="Assemble a debt-referral/FTT evidence scaffold")
    mref.add_argument("--flat", default="")
    mref.add_argument("--document-number", default="")
    mpay = mg.add_parser("payable", help="Add an outgoing payable")
    mpay.add_argument("supplier")
    mpay.add_argument("description")
    mpay.add_argument("schedule")
    mpay.add_argument("amount")
    mpay.add_argument("--invoice-date")
    mpay.add_argument("--due-date")
    mpay.add_argument("--budget-line", default="")
    mpay.add_argument("--vat-rate", type=float, default=0.0)
    mpay.add_argument("--vat-treatment", default="")
    mpay.add_argument("--cadence", default="")
    mpay.add_argument("--notes", default="")
    mpl = mg.add_parser("payables", help="List outgoing payables")
    mpl.add_argument("--status", default="")
    mpa = mg.add_parser("approve-payable", help="Approve a scheduled payable")
    mpa.add_argument("payable_id")
    mpp = mg.add_parser("pay-payable", help="Mark a payable paid and post it to actuals")
    mpp.add_argument("payable_id")
    mpp.add_argument("--amount")
    mpp.add_argument("--date")
    mpp.add_argument("--bank-reference", default="")
    mg.add_parser("cashflow", help="Expected income vs scheduled outgoings over 1/3/6 months")
    mbl = mg.add_parser("budget", help="Set/update a budget line")
    mbl.add_argument("year")
    mbl.add_argument("schedule")
    mbl.add_argument("line")
    mbl.add_argument("amount")
    mbl.add_argument("--notes", default="")
    mba = mg.add_parser("budget-actuals", help="Show budget/spent/committed/variance")
    mba.add_argument("--year", default="")
    mev = mg.add_parser("evidence", help="Attach evidence metadata to a demand, budget line, payable, supplier or flat")
    mev.add_argument("target_type")
    mev.add_argument("target_id")
    mev.add_argument("document_kind")
    mev.add_argument("title")
    mev.add_argument("--path", default="")
    mev.add_argument("--notes", default="")
    mel = mg.add_parser("evidence-list", help="List evidence metadata")
    mel.add_argument("--target-type", default="")
    mel.add_argument("--target-id", default="")

    # RTM capability + build-candidate state, ad-hoc build, sync checkpoints, real-data load
    rtm_sub.add_parser("capability", help="Show RTM capability state (what R1-R7 can do now) + build candidates + lock")
    rcand = rtm_sub.add_parser("candidates", help="List/manage RTM build candidates")
    rcand.add_argument("action", nargs="?", default="list", choices=["list", "new", "promote", "prune"])
    rcand.add_argument("--goal", default="")
    rcand.add_argument("--approach", default="")
    rcand.add_argument("--id", default="")
    rbuild = rtm_sub.add_parser("build", help="Advance the RTM product build ad-hoc now (takes the build lock; defers if a job holds it)")
    rbuild.add_argument("--steps", type=int, default=3)
    rsync = rtm_sub.add_parser("sync", help="Run a sync checkpoint now, or show channels/history")
    rsync.add_argument("action", nargs="?", default="now", choices=["now", "channels", "history"])
    rtm_sub.add_parser("load-real-data", help=_profile_text("Load the real {{rtm.name}} flats/budget into the management module"))
    rldd = rtm_sub.add_parser("load-real-demands", help=_profile_text("Load the real {{rtm.name}} half-year demands (not marked served)"))
    rldd.add_argument("--confirm", action="store_true", help="actually create the 18 demands")
    rtm_sub.add_parser("real-data-knowledge", help=_profile_text("Show the {{rtm.name}} operating facts + caveats"))
    rin = rtm_sub.add_parser("intake", help="Run a (synthetic/test) email through intake + draft")
    rin.add_argument("--from", dest="sender", required=True, help="sender, e.g. 'Jane <jane@x.com>'")
    rin.add_argument("--subject", required=True)
    rin.add_argument("--body", required=True)
    rtm_sub.add_parser("poll", help="Poll the live mailbox once (only acts if RTM_LIVE_MAILBOX is on)")
    rapp = rtm_sub.add_parser("approve", help="Approve a draft (sends only if RTM_LIVE_SEND is on)")
    rapp.add_argument("draft_id")
    rcan = rtm_sub.add_parser("cancel", help="Cancel a draft")
    rcan.add_argument("draft_id")
    unstuck_p.add_argument("--what-helps", action="store_true", help="instead, show what's tended to actually unstick you")
    unstuck_p.add_argument("--did", help="record that you did a suggested thing (water/walk/stretch/cook/music/make)")
    unstuck_p.add_argument("--helped", action="store_true", help="with --did: it helped")
    intro_p.add_argument("--cool", action="store_true", help="instead of the intro, do something cool (a real demo, or a clearly-labelled example)")

    # Body State: the local body/movement memory + honest load estimate.
    body_p = sub.add_parser("body", help="Body State: log workouts/swims/physio/body-state, and ask how loaded/recovered you are")
    body_sub = body_p.add_subparsers(dest="body_cmd")
    bl = body_sub.add_parser("log", help="Log a workout, swim, physio, weight or body-state note in natural language")
    bl.add_argument("text", nargs="+", help="e.g. 'walk 30 min felt good after' or 'stretching helped'")
    body_sub.add_parser("readiness", help="Your body load/recovery estimate (personalised once it knows your baseline; honest hypotheses before then)")
    body_sub.add_parser("today", help="What you've logged today")
    ci = body_sub.add_parser("checkin", help="A quick calibration check-in (how loaded you feel, 0-5) so it learns your patterns")
    ci.add_argument("load", type=int, help="how loaded you feel right now, 0-5")
    hist = body_sub.add_parser("history", help="Recent body-state log")
    hist.add_argument("--days", type=int, default=14, help="how many days back (default 14)")
    imp = body_sub.add_parser("import", help="Import an Alpha Progression export (sessions/sets/body-comp CSV) so progression questions become real")
    imp.add_argument("--sessions", help="path to the sessions CSV")
    imp.add_argument("--sets", help="path to the sets CSV")
    imp.add_argument("--body-comp", dest="body_comp", help="path to the body composition CSV")
    ci2 = body_sub.add_parser("checkin-flow", help="Start a guided body-literacy check-in (interactive)")
    ci2.add_argument("reply", nargs="*", help="(internal) continue an active flow with this reply")
    rst = body_sub.add_parser("reset", help="A proprioception/grounding reset (60s/3min/5min/primer)")
    rst.add_argument("kind", nargs="?", default="", help="60s, 3min, 5min, or primer")
    jnl = body_sub.add_parser("journal", help="Start an emotional-literacy journal entry")
    jnl.add_argument("kind", nargs="?", default="", help="naming, bridge, difficult, or freeform")
    sub.add_parser("system-map", help="What Mongoose is: what's connected, where your data lives, and what leaves this machine")
    rcp = sub.add_parser("receipts", help="What Mongoose did: a unified ledger of actions across subsystems")
    rcp.add_argument("--source", default="", choices=["", "devices", "surpriser", "scheduler", "whatsapp"],
                     help="filter to one subsystem")
    rcp.add_argument("--limit", type=int, default=50, help="how many recent receipts")
    sug = sub.add_parser("suggest", help="Ask Mongoose for a useful unprompted suggestion right now (the surpriser)")
    sug.add_argument("--feedback", default="", help="give feedback on the last suggestion: useful / not_useful")
    sug.add_argument("--signature", default="", help="(with --feedback) the suggestion signature")
    sub.add_parser("daily", help="The Daily Operating Picture: today's shape across every domain")
    prep = sub.add_parser("prepare", help="Prepare-me card for an event/person/project/task/place/situation")
    prep.add_argument("topic", nargs="*", help="what to prepare for, e.g. project meeting or tomorrow morning")
    dev = sub.add_parser("devices", help="Paired phones/tablets: list, pair (mint a code), or revoke")
    dev.add_argument("action", choices=["list", "pair", "revoke"])
    dev.add_argument("--profile", default="primary_phone",
                     choices=["primary_phone", "home_tablet"], help="for pair: device profile")
    dev.add_argument("--id", default="", help="for revoke: device id")
    mail = sub.add_parser("mail", help="Mail sync: see per-provider status/freshness or sync now")
    mail.add_argument("action", choices=["status", "sync"],
                      help="status: per-provider authorised + freshness; sync: run one sync cycle now and report")
    auth = sub.add_parser("authorize", help="Run OAuth consent for Google or Microsoft now")
    auth.add_argument("provider", choices=["google", "microsoft"])
    auth.add_argument("--todo-only", action="store_true",
                      help="Microsoft only: request just Tasks.Read (minimal consent for To Do, no app registration needed)")
    v3 = sub.add_parser("protocol-v3", help="Three-agent HA coordination (v3): show capability/handshake or check the Pi transport")
    v3.add_argument("action", nargs="?", default="status",
                    choices=["status", "handshake", "transport", "domains", "push-file"],
                    help="status=full capability report; handshake=the version-handshake message; transport=probe the Pi over SSH; domains=proposed functional domains; push-file=verifiably copy a file to the peer (skips if identical)")
    v3.add_argument("--local", help="with push-file: local file path")
    v3.add_argument("--remote", help="with push-file: remote (peer) destination path")
    sub.add_parser("merge-env", help="Add new keys from .env.example into .env without overwriting")
    sub.add_parser("dedupe-env", help="Collapse duplicate keys in .env, keeping each key's last value")

    bus_send = sub.add_parser("bus-send", help="Send a message to the shared bus")
    bus_send.add_argument("--from", dest="sender", required=True)
    bus_send.add_argument("--to", required=True)
    bus_send.add_argument("--subject", required=True)
    bus_send.add_argument("--type", default="note", choices=["brief", "task", "alert", "reply", "note"])
    bus_send.add_argument("--body", default="")
    bus_send.add_argument("--re", dest="re_id", default=None)
    bus_list = sub.add_parser("bus", help="List bus messages (default: unread)")
    bus_list.add_argument("--to", default=None)
    bus_list.add_argument("--from", dest="sender", default=None)
    bus_list.add_argument("--type", default=None)
    bus_list.add_argument("--all", action="store_true", help="include read messages")
    bus_read = sub.add_parser("bus-read", help="Read a message by id (archives it)")
    bus_read.add_argument("id")
    bus_read.add_argument("--keep", action="store_true", help="do not archive")
    bus_search = sub.add_parser("bus-search", help="Search bus messages")
    bus_search.add_argument("query")
    sub.add_parser("bus-reindex", help="Rebuild the bus index from files on disk")

    bench = sub.add_parser("benchmark", help="Benchmark models on YOUR machine against your real tasks (latency + usefulness)")
    bench.add_argument("models", nargs="*", help="model tags to test (default: the built-in shortlist)")
    bench.add_argument("--no-cold", action="store_true", help="skip the cold-load measurement (faster)")
    bench.add_argument("--no-pull", action="store_true", help="do NOT auto-download missing models (error on them instead)")
    bench.add_argument("--rounds", type=int, default=1, help="run this many interleaved rounds for thermal/noise fairness (>=2 recommended for a real decision)")
    bench.add_argument("--repeats", type=int, default=3, help="repeat each timed measurement this many times (median + spread)")
    bench.add_argument("--cooldown", type=float, default=5.0, help="seconds to cool down between models (thermal fairness)")
    bench.add_argument("--candidates", action="store_true", help="also benchmark the unmeasured candidate models (research/long-context hypotheses)")
    bench.add_argument("--results", action="store_true", help="render the table from results streamed so far (a run in progress or interrupted) and exit")
    bench.add_argument("--recommend", action="store_true", help="from the benchmarked results, recommend the best model per role AND whether a single model or a per-role combo is the better choice on this machine, then exit")
    bench.add_argument("--voice", metavar="CLIP.wav", help="run the VOICE benchmark on a spoken-audio clip: audio-native model vs Whisper+resident pipeline, then exit")
    bench.add_argument("--audio-model", default="gemma4:e2b", help="audio-native model for the voice benchmark (default gemma4:e2b)")

    tune = sub.add_parser("tune", help="Lane 2: once Mongoose has been used and benchmarked enough, show (or apply) the best models for how you actually use it")
    tune.add_argument("--apply", action="store_true", help="apply the recommended models now (reversible)")
    tune.add_argument("--revert", action="store_true", help="undo a tuning switch, back to the configured base models")
    tune.add_argument("--dismiss", action="store_true", help="dismiss the current suggestion until the advice changes")

    brief_p = sub.add_parser("brief", help="Generate a cross-domain daily brief (the thing only Mongoose can do)")
    brief_p.add_argument("--post", action="store_true", help="also drop it on the bus as a message to you")

    _psub = _ProfileCommands(sub)  # RTM build-plan commands are named after this install's RTM slug
    _psub.add_parser("build-plan-{{rtm.slug}}", help="Create (not run) the {{rtm.name}} triage build plan")
    _psub.add_parser("build-plan-{{rtm.slug}}-intake", help="Create (not run) the deeper {{rtm.name}} Intake MVP plan (WordPress + embeddings/classifier, live adapters disabled)")
    _psub.add_parser("build-plan-{{rtm.slug}}-governance", help="Create (not run) the {{rtm.name}} governance/bookkeeping plan (company secretary + trust ledgers, live filing/banking disabled)")
    _psub.add_parser("build-plan-{{rtm.slug}}-management", help="Create (not run) the {{rtm.name}} management plan (demands, Metro reconciliation, arrears, payables, budget, evidence; WordPress admin scaffold)")
    _psub.add_parser("build-plan-{{rtm.slug}}-rtm-trust-reconciliation", help="Create (not run) the {{rtm.name}} receipts + turnstile trust reconciliation plan (receipts, reserve maths, weekly reconcile, transfer monitoring; records and checks only, never moves money)")

    bnew = sub.add_parser("build-new", help="Plan AND build a project from a description (local plans, Code reviews)")
    bnew.add_argument("description", help="what to build, in a sentence or two")
    bnew.add_argument("--name", default=None, help="short name for the build")
    bnew.add_argument("--auto-approve", action="store_true", help="run unattended with no approval gate")
    bnew.add_argument("--code-budget", type=int, default=None, help="max Claude Code calls this run")
    bnew.add_argument("--max-hours", type=float, default=None, help="wall-clock cap in hours (default ~12h) when auto-approving")
    bnew.add_argument("--no-bus", action="store_true", help="don't post progress to the bus")
    bnew.add_argument("--dangerously-allow-shell-checks", action="store_true",
                      help="permit model-generated checks that use shell features (arbitrary execution risk)")
    sub.add_parser("builds", help="List build plans and their progress")
    bshow = sub.add_parser("build-show", help="Show a build plan's steps and status")
    bshow.add_argument("plan_id")
    bappr = sub.add_parser("build-approve", help="Approve a plan so it can run (required human gate)")
    bappr.add_argument("plan_id")
    brun = sub.add_parser("build-run", help="Run an approved plan's ready steps (overnight loop)")
    brun.add_argument("plan_id")
    brun.add_argument("--code-budget", type=int, default=None, help="max Claude Code calls this run")
    brun.add_argument("--max-hours", type=float, default=None, help="wall-clock cap in hours (default ~12h)")
    brun.add_argument("--no-bus", action="store_true", help="don't post progress to the bus")
    brun.add_argument("--dangerously-allow-shell-checks", action="store_true",
                      help="permit model-generated checks that use shell features (arbitrary execution risk)")
    brun.add_argument("--where", choices=["local", "pi"], default="local",
                      help="run Claude Code steps on the Mac (local) or offload to the Pi build node (pi)")
    brun.add_argument("--allow-pi-api-key", action="store_true",
                      help="proceed with a Pi build even if ANTHROPIC_API_KEY is set there (bills at API rates)")

    pibuild = sub.add_parser("pi-build", help="Use the Pi as a remote build node: check readiness or run a one-shot build there")
    pibuild.add_argument("action", nargs="?", default="status", choices=["status", "run"],
                         help="status=is the Pi a usable build node + how it's billed; run=sync a local repo to the Pi and run a one-shot claude -p build there")
    pibuild.add_argument("--repo", help="with run: local repo path to sync to the Pi")
    pibuild.add_argument("--task", help="with run: the build instruction for claude -p")
    pibuild.add_argument("--allow-api-key", action="store_true", help="proceed even if ANTHROPIC_API_KEY is set on the Pi (bills at API rates)")

    brec = sub.add_parser("build-recover", help="Reset steps stuck in 'running' (after a crash) so they retry")
    brec.add_argument("plan_id")
    sub.add_parser("version", help="Print the current Mongoose version")

    serve = sub.add_parser("serve", help="Run the Mongoose API server")
    sub.add_parser("stop", help="Stop a running Mongoose API server (by pidfile, then by port)")
    sub.add_parser("status", help="Is the Mongoose server running? Show version, port and PID.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--tls", action="store_true", help="force HTTPS using MONGOOSE_TLS_CERTFILE/KEYFILE (or an auto-generated self-signed cert)")
    serve.add_argument("--no-tls", action="store_true", help="serve plain http (opt out of the default HTTPS, e.g. behind a TLS-terminating reverse proxy)")

    svc = sub.add_parser("service", help="Run the scheduler unattended via launchd (macOS): generate/install/status/uninstall")
    svc.add_argument("action", choices=["generate", "install", "status", "uninstall"],
                     help="generate: write the plist; install: write + load (asks to confirm); status: show install/loaded state; uninstall: unload + remove")
    svc.add_argument("--yes", action="store_true", help="skip the confirmation prompt for install")
    sched = sub.add_parser("scheduler", help="Run Mongoose's background scheduler (daily brief, periodic email sync, file reindex)")
    sched.add_argument("--once", action="store_true", help="Run any due jobs once and exit (for cron/testing)")
    sched.add_argument("--tick", type=int, default=60, help="Seconds between checks in loop mode")
    sched.add_argument("--status", action="store_true", help="Show each job's schedule and last run, then exit")
    sched.add_argument("--disable", nargs="*", help="Pause named job(s), comma-separated OK; durable across restarts")
    sched.add_argument("--enable", nargs="*", help="Resume named job(s), comma-separated OK")
    sched.add_argument("--free-memory", action="store_true", help="Pause deferrable model/IO-heavy jobs while leaving safety jobs running")
    sched.add_argument("--reason", default="", help="Reason recorded with --disable/--free-memory")

    wd = sub.add_parser("server-watchdog",
                        help="Run the EXTERNAL server watchdog: probes /health/live and restarts the "
                             "server if its event loop wedges (the one failure the in-process watchdog "
                             "can't recover, since the loop is what's stuck). Run by launchd with KeepAlive.")
    wd.add_argument("--once", action="store_true", help="Run a single probe/decision tick and exit")
    wd.add_argument("--interval", type=int, default=15, help="Seconds between probes in loop mode")
    wd.add_argument("--fail-threshold", type=int, default=3,
                    help="Consecutive failed probes before restarting (avoids bouncing on a blip)")
    wd.add_argument("--cooldown", type=int, default=60,
                    help="Minimum seconds between restarts (prevents a tight kill/respawn loop)")
    wd.add_argument("--install", action="store_true",
                    help="Install the watchdog as a launchd agent (KeepAlive) and print next steps")
    wd.add_argument("--uninstall", action="store_true",
                    help="Stop and remove the watchdog launchd agent (so it no longer respawns the server)")

    crawl = sub.add_parser("crawler", help="Inspect and drive the corpus crawler (backfill + freshness per source)")
    crawl.add_argument("--status", action="store_true", help="Show per-source crawl progress and cursors")
    crawl.add_argument("--freshen", metavar="SOURCE", help="Run a freshness pass: 'imap' or a files root path")
    crawl.add_argument("--backfill", metavar="SOURCE", help="Run a backfill chunk: a files root path")
    crawl.add_argument("--device", default="local", help="Device tag for file crawls")

    chat = sub.add_parser("chat", help="Send one prompt through the local assistant")
    chat.add_argument("prompt")
    chat.add_argument("--role", default="auto", help="auto/chat/tool/code")
    chat.add_argument("--mode", default=None, help="persona mode: default/adult/admin/weather")
    chat.add_argument("--conversation", default=None, help="conversation id (default: a persistent CLI thread)")
    chat.add_argument("--no-memory", action="store_true", help="disable conversation memory for this turn")

    voice = sub.add_parser("voice", help="Talk to Mongoose: speaks replies aloud, optional microphone input")
    voice.add_argument("--mic", action="store_true", help="listen via microphone (needs sox/rec + whisper; falls back to typing)")
    voice.add_argument("--session", action="store_true", help="interactive mic session; asks which listening mode to use up front")
    voice.add_argument("--mode", default=None, help="persona mode: default/adult/admin/weather")
    voice.add_argument("--conversation", default=None, help="conversation id (default: the one permanent Mongoose thread, shared with chat/phone/HA)")
    voice.add_argument("--say", dest="once", default=None, help="speak a single reply to this prompt and exit")

    svoice = sub.add_parser("setup-voice", help="Detect and install the mic toolchain (sox, ffmpeg, whisper) with confirmation")
    svoice.add_argument("--yes", action="store_true", help="install without the confirmation prompt")

    sntfy = sub.add_parser("setup-ntfy", help="Install a local ntfy (native, no Docker) for reliable phone push, with a launchd service")
    sntfy.add_argument("--yes", action="store_true", help="install without the confirmation prompt")

    sub.add_parser("start-docker", help="Launch Docker Desktop (macOS) and wait for it, then start the optional ntfy/SearXNG containers")

    seph = sub.add_parser("enable-phone", help="Let your phone reach Mongoose over the LAN (writes host/token settings and restarts)")
    seph.add_argument("--yes", action="store_true", help="enable without the confirmation prompt")

    stls = sub.add_parser("tls", help="Real HTTPS for guests via Let's Encrypt (DNS-01 over DuckDNS): trusted certs, no browser warnings")
    stls_sub = stls.add_subparsers(dest="tls_cmd")
    stls_sub.add_parser("status", help="Show TLS readiness and current certificate expiry")
    stls_sub.add_parser("preflight", help="Check prerequisites for obtaining a certificate")
    sobtain = stls_sub.add_parser("obtain", help="Obtain (or renew) a Let's Encrypt certificate")
    sobtain.add_argument("--staging", action="store_true", help="Use the staging CA for a dry run (untrusted, no rate limits)")
    stls_sub.add_parser("install-certbot", help="pip-install certbot into this environment")

    sddns = sub.add_parser("ddns", help="Stay reachable by a stable name when your home IP changes (Tailscale, DuckDNS, Dynu, No-IP, Cloudflare)")
    stunnel = sub.add_parser("tunnel", help="Cloudflare Tunnel: a guest-friendly public https "
                                            "address that works behind CGNAT (no port-forward, "
                                            "real trusted certificate)")
    tunnel_sub = stunnel.add_subparsers(dest="tunnel_command")
    tunnel_sub.add_parser("status", help="Where the tunnel setup currently stands, step by step")
    tunnel_sub.add_parser("setup", help="Guided setup: shows exactly the next command to run")
    tunnel_sub.add_parser("diagnose", help="Prove the public hostname end-to-end and name what's missing if it fails")
    p_sethost = tunnel_sub.add_parser("set-hostname", help="Tell Mongoose its tunnel hostname (also sets the public URL)")
    p_sethost.add_argument("hostname", help="e.g. mongoose.example.com")
    ddns_sub = sddns.add_subparsers(dest="ddns_command")
    dset = ddns_sub.add_parser("setup", help="Choose and configure a remote-reach provider")
    dset.add_argument("--provider", choices=["tailscale", "duckdns", "dynu", "noip", "cloudflare"],
                      help="pick a provider non-interactively")
    dset.add_argument("--yes", action="store_true", help="skip confirmation prompts where possible")
    dupd = ddns_sub.add_parser("update", help="Update the DNS record now (auto-runs on a schedule too)")
    dupd.add_argument("--force", action="store_true", help="update even if the IP looks unchanged")
    ddns_sub.add_parser("status", help="Show the current DDNS provider and last-known IP")
    ddns_sub.add_parser("diagnose", help="Why a DDNS address might be refusing connections: "
                                         "distinguishes a Mongoose-side listener problem from "
                                         "a genuine router-side one")

    rem = sub.add_parser("remember", help="Store a long-term fact")
    rem.add_argument("fact")
    rf = sub.add_parser("remember-from", help="Let the model propose facts from text, then confirm each")
    rf.add_argument("text")
    rf.add_argument("--yes", action="store_true", help="store all proposed facts without confirming")
    sub.add_parser("memory", help="List stored long-term facts")
    forget = sub.add_parser("forget", help="Delete a stored fact (exact text)")
    forget.add_argument("fact")
    sub.add_parser("new-conversation", help="Clear the default CLI conversation history")

    backup_p = sub.add_parser("backup", help="Back up the irreplaceable part (brain + config); optionally to Google Drive")
    backup_sub = backup_p.add_subparsers(dest="backup_cmd")
    bc_create = backup_sub.add_parser("create", help="Create a brain+config backup archive now")
    bc_create.add_argument("--label", default="manual")
    backup_sub.add_parser("list", help="List local backup archives")
    bc_restore = backup_sub.add_parser("restore", help="Restore brain (+config) from a backup archive")
    bc_restore.add_argument("archive")
    bc_restore.add_argument("--no-config", action="store_true", help="restore the brain only, not .env/config")
    backup_sub.add_parser("connect-drive", help="Connect Google Drive for off-machine backups (drive.file scope)")
    backup_sub.add_parser("to-drive", help="Create a backup and upload it to Google Drive now (applies the rota)")
    backup_sub.add_parser("drive-list", help="List backups currently in the Google Drive folder")
    backup_sub.add_parser("status", help="Show backup lane health: last success, last error, Drive connection")
    backup_sub.add_parser("drive-health", help="Actively check Drive is usable (token valid + a live call works)")
    backup_sub.add_parser("prune", help="Apply the local grandfather-father-son rota now")

    brain_p = sub.add_parser("brain", help="Preserve the brain (conversation + memory + identity) across updates: snapshot, verify, restore")
    brain_sub = brain_p.add_subparsers(dest="brain_cmd")
    brain_sub.add_parser("verify", help="Integrity-check the brain (is conversation/memory intact?)")
    bwake = brain_sub.add_parser("wakeup", help="Run the wake-up self-check (checkpoint WAL, recreate missing tables, rebuild desynced memory search)")
    bwake.add_argument("--check-only", action="store_true", help="report problems without repairing")
    bsnap = brain_sub.add_parser("snapshot", help="Take a consistent snapshot of the brain now (safe while running)")
    bsnap.add_argument("--label", default="manual")
    brain_sub.add_parser("list", help="List brain snapshots, newest first")
    brest = brain_sub.add_parser("restore", help="Restore the brain from a snapshot directory (independent of code)")
    brest.add_argument("snapshot_dir")
    bprune = brain_sub.add_parser("prune", help="Keep the newest N brain snapshots, delete older")
    bprune.add_argument("--keep", type=int, default=10)
    broll = brain_sub.add_parser("rollover", help="Archive the ENTIRE current main conversation "
                                 "under a dated id and start the shared thread fresh (nothing is "
                                 "deleted). The remedy when a mis-paired device contaminated the "
                                 "main thread before message attribution existed.")
    broll.add_argument("--reason", default="", help="optional note recorded on the archive")

    harden_p = sub.add_parser("harden", help="Self-harden: turn failures into claim/invariant observations, packets and build-plan candidates (evidence only, never changes code)")
    harden_sub = harden_p.add_subparsers(dest="harden_cmd")
    harden_sub.add_parser("status", help="Show open hardening observations and last scan")
    ho = harden_sub.add_parser("observe", help="Record a hardening observation manually")
    ho.add_argument("summary")
    ho.add_argument("--source", default="user")
    ho.add_argument("--kind", default="issue")
    ho.add_argument("--scope", default="manual")
    ho.add_argument("--severity", default="P1", choices=["P0", "P1", "P2"])
    ho.add_argument("--claim", default="")
    hs = harden_sub.add_parser("scan", help="Run a cheap local self-harden scan now")
    hs.add_argument("--json", action="store_true")
    hcons = harden_sub.add_parser("consistency", help="Check that subsystems making the same claim use the same logic (find claim divergence, the recurring bug class)")
    hcons.add_argument("--json", action="store_true")
    hr = harden_sub.add_parser("recent", help="Show recent hardening observations")
    hr.add_argument("--limit", type=int, default=30)
    hr.add_argument("--status", default="")
    harden_sub.add_parser("claims", help="List the built-in claim/invariant registry")
    mi = harden_sub.add_parser("module-index", help="Rebuild or show the code module index")
    mi.add_argument("--rebuild", action="store_true")
    mi.add_argument("--query", default="")
    mi.add_argument("--limit", type=int, default=40)
    hre = harden_sub.add_parser("reingest", help="Rebuild the code module/claim index and export a code-reingest packet")
    hre.add_argument("path", nargs="?", default="")
    hp = harden_sub.add_parser("export", help="Export a hardening packet for Claude/OpenAI review")
    hp.add_argument("path", nargs="?", default="")
    hp.add_argument("--no-modules", action="store_true")
    hrev = harden_sub.add_parser("review-packet", help="Create an AI-review packet for one observation; writes files only")
    hrev.add_argument("observation_id", nargs="?", default="")
    hrev.add_argument("--path", default="")
    hrev.add_argument("--reviewer", default="claude", choices=["claude", "openai", "local"])
    hpl = harden_sub.add_parser("plan", help="Create an approval-gated hardening build plan for an observation")
    hpl.add_argument("observation_id", nargs="?", default="")
    hpl.add_argument("--title", default="")

    weather = sub.add_parser("weather", help="Local weather now and the day ahead (from your configured location)")
    weather.add_argument("--force", action="store_true")

    imap = sub.add_parser("sync-imap")
    imap.add_argument("--mailbox", default="INBOX")
    imap.add_argument("--limit", type=int, default=50)

    files = sub.add_parser("index-files", help="Index a folder's text files (use --device to tag the source machine, e.g. a Syncthing-synced phone folder)")
    files.add_argument("root")
    files.add_argument("--limit", type=int, default=1000)
    files.add_argument("--device", default="local", help="Label for the device this folder belongs to, e.g. macbook, phone")

    sfiles = sub.add_parser("search-files", help="Search indexed files across all devices")
    sfiles.add_argument("query")
    sfiles.add_argument("--limit", type=int, default=10)

    satt = sub.add_parser("search-attachments", help="Find email attachments by filename, subject, sender, or content")
    satt.add_argument("query")
    satt.add_argument("--limit", type=int, default=20)

    sub.add_parser("ha-prompts", help="List the named Home Assistant assistant prompts (Automation review, Energy report, etc.)")
    hap = sub.add_parser("ha-prompt", help="Run a named HA assistant prompt through Mongoose's brain")
    hap.add_argument("key", help="Prompt key, e.g. automation_review (see ha-prompts)")
    hap.add_argument("--extra", default="", help="Your specifics, e.g. which device or change")

    push = sub.add_parser("push", help="Send a push notification to your phone (via ntfy)")
    push.add_argument("title")
    push.add_argument("--body", default="")
    push.add_argument("--priority", default="default", choices=["max", "high", "default", "low", "min"])
    push.add_argument("--tags", default="", help="Comma-separated ntfy tags, e.g. warning,calendar")
    push.add_argument("--click", default="", help="URL to open when tapped")

    findp = sub.add_parser("find", help="Search EVERYTHING at once: files (all devices), email attachments, and email text")
    findp.add_argument("query")
    findp.add_argument("--limit", type=int, default=10)
    findp.add_argument("--kinds", default="", help="Restrict to a subset, comma-separated: file,attachment,email,voicemail,notification")
    findp.add_argument("--export", choices=["markdown", "json"], help="Export results as a portable document instead of printing hits")
    findp.add_argument("--out", help="With --export: write to this file (otherwise prints to stdout)")

    google_cal = sub.add_parser("sync-google-calendar")
    google_cal.add_argument("--days-ahead", type=int, default=14)
    google_cal.add_argument("--calendar-id", default="primary")

    gmail = sub.add_parser("sync-gmail")
    gmail.add_argument("--query", default="newer_than:30d")
    gmail.add_argument("--limit", type=int, default=100)

    ms_cal = sub.add_parser("sync-ms-calendar")
    ms_cal.add_argument("--days-ahead", type=int, default=14)

    sub.add_parser("sync-ms-todo")

    ms_mail = sub.add_parser("sync-ms-mail")
    ms_mail.add_argument("--folder", default="inbox")
    ms_mail.add_argument("--limit", type=int, default=100)

    kn = sub.add_parser("knowledge", help="Imported-knowledge graph: import dumps, suggest merges, synthesize")
    kn_sub = kn.add_subparsers(dest="knowledge_cmd", required=True)
    kn_imp = kn_sub.add_parser("import", help="Import a thread-dump zip into the knowledge graph")
    kn_imp.add_argument("path")
    kn_ing = kn_sub.add_parser("ingest", help="Tolerantly ingest any dump (prose, renamed JSONL, subfolders, YAML sidecars); extracted records are review-gated")
    kn_ing.add_argument("path")
    kn_sf = kn_sub.add_parser("suggest-fusions", help="Create reviewable entity-fusion suggestions (never auto-applied)")
    kn_sf.add_argument("--limit", type=int, default=40)
    kn_sf.add_argument("--llm", action="store_true", help="Also ask the local model (opt-in)")
    kn_rf = kn_sub.add_parser("resolve-fusion", help="Approve/reject a suggested merge")
    kn_rf.add_argument("suggestion_id")
    kn_rf.add_argument("action", choices=["approve", "reject", "keep_open"])
    kn_sy = kn_sub.add_parser("synthesize", help="Run the multi-claim synthesis cycle")
    kn_sy.add_argument("--limit", type=int, default=30)
    kn_sy.add_argument("--llm", action="store_true", help="Also ask the local model (opt-in)")
    kn_sc = kn_sub.add_parser("synthesis", help="List synthesis cards")
    kn_sc.add_argument("--limit", type=int, default=20)
    kn_sub.add_parser("dashboard", help="Knowledge graph counts + recent items")
    kn_ai = kn_sub.add_parser("archive-items", help="How a recursive archive import classified child items (dumps vs build artifacts)")
    kn_ai.add_argument("--archive-id", dest="archive_id", default=None)
    kn_ai.add_argument("--limit", type=int, default=100)

    # Hide internal/plumbing commands from `mongoose --help` without disabling them: they stay in the
    # parser map (scripts and the server still call them), we only drop them from the help listing.
    # Guarded: if argparse internals ever change, help just shows everything rather than crashing.
    try:
        _hidden = {_profile_text(n) for n in _HIDDEN_TOPLEVEL}
        sub._choices_actions = [a for a in sub._choices_actions
                                if getattr(a, "dest", None) not in _hidden]
    except Exception:
        pass

    args = parser.parse_args()

    if args.command == "init-db":
        init_db()
        print("Database initialised")
    elif args.command == "knowledge":
        import json as _json
        kc = args.knowledge_cmd
        if kc == "import":
            from .knowledge_import import import_bulk_dump
            rep = import_bulk_dump(args.path, apply_accepted_memory=False)
            d = rep.model_dump() if hasattr(rep, "model_dump") else rep
            print(f"imported: {d.get('records_imported')} records, {d.get('claims_created')} claims, "
                  f"{d.get('records_needing_review')} to review")
        elif kc == "ingest":
            from .knowledge_import import import_bulk_dump_bytes
            p = pathlib.Path(args.path)
            rep = import_bulk_dump_bytes(p.read_bytes(), source_name=p.name)
            d = rep.model_dump() if hasattr(rep, "model_dump") else dict(rep)
            note = " (recursive archive)" if d.get("package_type") == "knowledge_archive_bundle" else ""
            print(f"ingested{note}: {d.get('records_imported')} records, {d.get('records_needing_review')} to review")
        elif kc == "archive-items":
            from .knowledge_import.store import list_archive_items
            print(_json.dumps(list_archive_items(args.archive_id, limit=args.limit), indent=2, default=str))
        elif kc == "suggest-fusions":
            from .knowledge_import.entity_suggestions import suggest_entity_fusions
            print(_json.dumps(suggest_entity_fusions(limit=args.limit, use_llm=args.llm), indent=2, default=str))
        elif kc == "resolve-fusion":
            from .knowledge_import.store import resolve_fusion_suggestion
            print(_json.dumps(resolve_fusion_suggestion(args.suggestion_id, args.action), indent=2, default=str))
        elif kc == "synthesize":
            from .knowledge_import.synthesis import run_synthesis_cycle
            print(_json.dumps(run_synthesis_cycle(limit=args.limit, use_llm=args.llm), indent=2, default=str))
        elif kc == "synthesis":
            from .knowledge_import.store import list_synthesis_cards
            cards = list_synthesis_cards(limit=args.limit)
            print(f"synthesis cards: {len(cards)}")
            for c in cards:
                print(f"  [{c.get('domain')}] {c.get('title')}  (confidence {c.get('confidence')})")
        elif kc == "dashboard":
            from .knowledge_import.store import dashboard
            print(_json.dumps(dashboard(), indent=2, default=str))
        return 0
    elif args.command in ("checkup", "release-check", "alpha-check"):
        from .release_check import release_check, format_release_check
        report = asyncio.run(release_check())
        print(format_release_check(report))
        return 0 if report["verdict"] == "ready" else 1
    elif args.command == "connectors":
        from .connector_probes import probe_all
        agg = probe_all()
        sm = agg["summary"]
        print(f"Connector health, {sm['usable']}/{sm['configured']} configured connectors working "
              f"({sm['broken']} broken, {sm['unproven']} unproven):\n")
        for c in agg["connectors"]:
            if not c["configured"]:
                mark = "  ·"
                state = "not set up"
            elif c["usable"] is True:
                mark = "  \u2713"
                state = "working"
            elif c["usable"] is False:
                mark = "  \u2717"
                state = "NOT WORKING"
            else:
                mark = "  \u25cb"
                state = "set up (not live-tested)"
            lat = f" [{c['latency_ms']}ms]" if c.get("latency_ms") is not None else ""
            print(f"{mark} {c['name']:<16} {state:<26} {c['detail']}{lat}")
        if sm["broken"]:
            print(f"\n\u26a0 {sm['broken']} connector(s) are configured but not working: "
                  f"{', '.join(sm['broken_names'])}")
            return 1
        return 0
    elif args.command == "claude-code":
        from .services import claude_code as CC
        sub_cmd = getattr(args, "cc_cmd", "status")
        if sub_cmd == "discover":
            print(json.dumps(CC.discover(), indent=2))
        elif sub_cmd == "route":
            print(json.dumps(CC.recommend_route(getattr(args, "task", "general")), indent=2))
        else:
            rep = CC.status_report()
            disc = rep["discovery"]
            print("Claude Code integration")
            print(f"  Local (Mac): {'present' if disc['local']['present'] else 'not found'}"
                  + (f" at {disc['local']['resolved_path']}" if disc['local'].get('resolved_path') else ""))
            pi = disc["pi"]
            if pi.get("reachable"):
                print(f"  Pi: {'present' if pi['present'] else 'not found'}"
                      + (f" at {pi['resolved_path']}" if pi.get("resolved_path") else ""))
            else:
                print(f"  Pi: {pi.get('detail', 'not reachable')}")
            if rep["billing_traps"]:
                print("\n  BILLING WARNINGS:")
                for t in rep["billing_traps"]:
                    print(f"    ! {t}")
            print(f"\n  {rep['headline']}")
        return 0
    elif args.command == "doctor":
        from .doctor import run_doctor
        if getattr(args, "models", False):
            from .model_residency import check_residency, format_models_report
            r = asyncio.run(check_residency())
            print(format_models_report(r))
            return 0
        if getattr(args, "install_root", False):
            from .services.install_check import check_install_coherence
            r = check_install_coherence()
            print(f"Install coherence: {'OK' if r['ok'] else 'INCOHERENT'}")
            print(f"  running version: {r['running_version']}")
            print(f"  pyproject version: {r['pyproject_version']}")
            print(f"  project root: {r['project_root']}")
            print(f"  CLI module: {r['cli_module']}")
            print(f"  python: {r['python_executable']}")
            print(f"  commands: {r['command_count']} ({len(r['missing_commands'])} expected missing)")
            if not r["ok"]:
                for p in r["problems"]:
                    print(f"  PROBLEM: {p}")
                print(f"\n  {r['hint']}")
            return 0 if r["ok"] else 1
        if getattr(args, "capabilities", False):
            from .services.capabilities import capabilities_report
            rep = capabilities_report()
            print("Ready: " + (", ".join(rep["ready"]) or "(none)"))
            if rep["missing"]:
                print("\nMissing optional libraries:")
                for m in rep["missing"]:
                    print(f"  {m['capability']}: {m['missing']}")
                    print(f"    fix: {m['fix']}")
            else:
                print("\nAll capabilities have their dependencies installed.")
            try:
                from .services.whatsapp_journey import connection_state
                ws = connection_state()
                label = {"connected": "linked", "scan_qr": "waiting for QR scan", "connecting": "pairing",
                         "disconnected": "service up, not linked", "not_configured": "not started",
                         "error": "unreachable"}.get(str(ws.get("state")), str(ws.get("state")))
                print(f"\nWhatsApp (WAHA sidecar): {label}")
                if ws.get("state") in ("not_configured", "error"):
                    print("  start it: mongoose start-docker  (or the Start button in the cockpit WhatsApp tab)")
            except Exception:
                pass
        else:
            print(json.dumps(run_doctor(), indent=2))
    elif args.command == "routine":
        from .services import routines as RT
        rc = getattr(args, "routine_cmd", None)
        if rc == "list":
            rows = RT.list_routines()
            if not rows:
                print("No routines yet. Create one: mongoose routine save <name> \"<what it does>\"")
            for r in rows:
                trig = f"  (trigger: \"{r['trigger_phrase']}\")" if r.get("trigger_phrase") else ""
                print(f"  {r['name']}{trig}  [run {r.get('run_count', 0)}x]")
                print(f"     {r['recipe']}")
            return 0
        elif rc == "save":
            r = RT.save_routine(args.name, args.recipe, trigger_phrase=getattr(args, "trigger", None))
            print(f"Routine {r['action']}: {r['name']}" if r.get("ok") else f"Error: {r.get('error')}")
            return 0 if r.get("ok") else 1
        elif rc == "run":
            r = asyncio.run(RT.run_routine(args.name))
            if not r.get("ok"):
                print(f"Error: {r.get('error')}")
                return 1
            print(r.get("reply") or "Done.")
            steps = r.get("steps", [])
            if steps:
                print(f"  ({len(steps)} step(s): {', '.join(s.get('tool') or '?' for s in steps)})")
            return 0
        elif rc == "delete":
            r = RT.delete_routine(args.name)
            print(f"Deleted {args.name}." if r.get("ok") else f"No routine called '{args.name}'.")
            return 0 if r.get("ok") else 1
        else:
            print("Use: mongoose routine [list|save|run|delete]")
            return 1
    elif args.command == "ritual":
        from .services import rituals as R
        from .services.journal_layer import log_diary
        if args.stats:
            print(json.dumps(R.ritual_stats(), indent=2))
        elif args.done is not None:
            print(json.dumps(R.complete_ritual(args.done), indent=2))
        elif not args.name:
            print("Useful micro-practices:")
            for r in R.list_rituals():
                print(f"  {r['name']:16} \"{r['trigger']}\"  ({r['duration_s']}s)")
        else:
            res = R.start_ritual(args.name)
            if not res.get("ok"):
                print(f"Unknown ritual. Available: {', '.join(res.get('available', []))}")
            else:
                if res.get("recently_done"):
                    print(f"(note: {res['recently_done']})")
                print(f"\n{res['script']}\n")
                print(f"When you're done: mongoose ritual --done {res['log_id']}")
                log_diary("ritual", f"started {res['ritual']}")
    elif args.command == "what-changed":
        from .services.journal_layer import what_changed
        res = what_changed(acknowledge=not args.peek)
        print(res["summary"])
    elif args.command == "diary":
        from .services.journal_layer import render_diary_today, diary_week
        if args.week:
            print(json.dumps(diary_week(), indent=2))
        else:
            print(render_diary_today())
    elif args.command == "whatsapp":
        from .services import whatsapp as wa
        cmd = getattr(args, "wa_command", None)
        if cmd == "search":
            for r in wa.search_messages(args.query):
                print(f"[{r['direction']}] {r['contact']}: {r['snippet']}")
        elif cmd == "status":
            from .services.whatsapp_journey import accounts_state
            print(json.dumps(accounts_state(), indent=2, default=str))
        elif cmd == "connect":
            from .services.whatsapp_journey import connect, connection_state
            role = getattr(args, "role", "owner_read")
            r = connect(role)
            if r.get("ok"):
                st = connection_state(role)
                print(f"Session {role} starting. State: {st.get('state')}")
                print("Open the cockpit WhatsApp tab (or run `mongoose whatsapp status`) to scan the QR for this account.")
            else:
                print("Couldn't connect:", r.get("error"))
            return 0 if r.get("ok") else 1
        elif cmd == "test":
            from .services.whatsapp_journey import send_test
            r = send_test(getattr(args, "to", ""))
            print("Test sent - check WhatsApp on your phone." if r.get("ok") else f"Test failed: {r.get('error')}")
            return 0 if r.get("ok") else 1
        elif cmd == "link":
            url = os.getenv("WAHA_URL", "http://127.0.0.1:3000")
            from . import profile as _profile
            owner_s = os.getenv("WHATSAPP_OWNER_SESSION", _profile.get("owner.whatsapp_session"))
            mong_s = os.getenv("WHATSAPP_MONGOOSE_SESSION", "mongoose")
            print("To link WhatsApp (dual-account mode):")
            print("  1. Start the sidecar: docker compose -f docker-compose.whatsapp.yml up -d")
            print(f"  2. Link {_profile.owner_name()}'s inbox ({owner_s}):    mongoose whatsapp connect --role owner_read")
            print(f"  3. Link Mongoose sender ({mong_s}): mongoose whatsapp connect --role mongoose_send")
            print("  4. Check both: mongoose whatsapp status  (want both WORKING)")
            print(f"  WAHA base URL: {url}")
        elif cmd == "from":
            for r in wa.recent_from(args.contact):
                print(f"[{r['direction']}] {r['body']}")
        elif cmd == "waiting":
            w = wa.unanswered_inbound()
            print("Waiting on a reply: " + (", ".join(x["contact"] for x in w if x["contact"]) or "(no one)"))
        elif cmd == "draft":
            d = wa.draft_message(args.contact, args.body)
            print(f"Draft #{d['id']} to {d['contact']} (pending, NOT sent):")
            print(f"  \"{d['body']}\"")
            print(f"Approve and send with: mongoose whatsapp approve {d['id']}")
        elif cmd == "drafts":
            ds = wa.list_drafts()
            if not ds:
                print("No pending drafts.")
            for d in ds:
                print(f"#{d['id']} to {d['contact']}: \"{d['body']}\"")
        elif cmd == "approve":
            # No real transport wired here; approval marks it ready. A configured
            # bridge would pass send_fn. This keeps the safety boundary explicit.
            res = wa.approve_and_send(args.draft_id)
            print(json.dumps(res, indent=2))
        elif cmd == "cancel":
            print(json.dumps(wa.cancel_draft(args.draft_id), indent=2))
        else:
            print("Usage: mongoose whatsapp [search|from|waiting|draft|drafts|approve|cancel]")
    elif args.command == "verify-data":
        from .services.data_verify import verify_data
        report = verify_data()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Data verification: {'OK' if report['ok'] else 'PROBLEMS'} "
                  f"({report['errors']} error(s), {report['warnings']} warning(s))")
            for f in report["findings"]:
                tag = {"ok": "OK  ", "warn": "WARN", "error": "ERR "}.get(f["level"], "?")
                if f["level"] != "ok":
                    fix = f"  -> repair-data {f['fixable']}" if f.get("fixable") else ""
                    print(f"  [{tag}] {f['check']}: {f['detail']}{fix}")
            if report["ok"] and report["warnings"] == 0:
                print("  All checks passed.")
        return 0 if report["ok"] else 1
    elif args.command == "repair-data":
        from .services.data_verify import repair_data
        result = repair_data(args.action, dry_run=not args.apply)
        print(json.dumps(result, indent=2))
        if not args.apply and result.get("ok") is not False:
            print("\n(dry-run; re-run with --apply to make changes)")
    elif args.command == "setup-embeddings":
        from .services.embeddings import ensure_model, available
        res = ensure_model(quiet=False)
        if not res["ok"]:
            print(f"embeddings setup failed: {res['error']}")
            return 1
        st = available()
        print("embeddings ready" if st.get("ok")
              else f"files present but libraries missing: {st.get('hint', '')}")
        return 0 if st.get("ok") else 1
    elif args.command == "install-missing":
        from .services.capabilities import capabilities_report, install_all_missing
        rep = capabilities_report()
        if not rep["missing"]:
            print("All capabilities already have their dependencies. Nothing to install.")
            return 0
        print("Missing optional libraries:")
        for m in rep["missing"]:
            print(f"  {m['capability']}: {m['missing']}")
        if args.dry_run:
            results = install_all_missing(dry_run=True)
            for r in results:
                if r.get("would_install"):
                    print(f"would install for {r['capability']}: {', '.join(r['would_install'])}")
            return 0
        if not args.yes:
            resp = input("\nInstall these into the active environment now? [Y/n] ").strip().lower()
            if resp not in ("", "y", "yes"):
                print("Skipped. Run again with --yes to install.")
                return 0
        for r in install_all_missing():
            if r.get("ok") and r.get("installed"):
                print(f"installed for {r['capability']}: {', '.join(r['installed'])}")
            elif r.get("skipped_commands"):
                print(f"{r['capability']}: needs system command(s) {', '.join(r['skipped_commands'])} (install via your OS package manager)")
            elif not r.get("ok"):
                print(f"FAILED for {r['capability']}: {r.get('error', 'unknown')}")
    elif args.command == "setup":
        from .setup_wizard import run_wizard, integration_status
        if args.capability or args.all:
            from .services.capabilities import install_capability, install_all_missing, detect_all
            if args.all:
                if not args.yes:
                    miss = [d["capability"] for d in detect_all() if d["missing_pip"]]
                    if not miss:
                        print("All capabilities already have their dependencies.")
                        return 0
                    resp = input(f"Install dependencies for: {', '.join(miss)}? [Y/n] ").strip().lower()
                    if resp not in ("", "y", "yes"):
                        return 0
                for r in install_all_missing():
                    print(json.dumps(r))
            else:
                if args.capability == "whatsapp":
                    _setup_whatsapp_capability()
                else:
                    r = install_capability(args.capability)
                    print(json.dumps(r, indent=2))
        elif args.status:
            print(json.dumps(integration_status(), indent=2))
        else:
            run_wizard()
    elif args.command == "update":
        from .updater import (run_update, run_update_flag, run_update_guided,
                              stage_update_candidate, list_update_stages, check_update_stage,
                              approve_update_stage, apply_update_stage)
        # Staged-updater path (observe -> stage -> check -> approve -> apply). Nothing is applied
        # without an explicit approved+checked stage and --yes.
        if args.stages:
            rows = list_update_stages()
            if not rows:
                print("No staged update candidates.")
            for st in rows:
                checks = st.get("checks", [])
                checks_ok = bool(checks) and all(c.get("ok") for c in checks)
                print(f"  {st['stage_id']}  {st.get('current_version','?')} -> {st.get('target_version','?')}  "
                      f"[{'approved' if st.get('approved') else 'unapproved'}; "
                      f"checks {'passed' if checks_ok else 'pending/failed'}]")
            return 0
        if args.check_stage:
            r = check_update_stage(args.check_stage, runtime_smoke=bool(getattr(args, "runtime_smoke", False)))
            print(json.dumps(r, indent=2))
            return 0 if r.get("ok") else 1
        if getattr(args, "rollback_drill", None):
            from .updater import rollback_drill_stage
            r = rollback_drill_stage(args.rollback_drill)
            print(json.dumps(r, indent=2))
            return 0 if r.get("ok") else 1
        if args.approve_stage:
            r = approve_update_stage(args.approve_stage)
            ok = bool(r) and r.get("status") == "approved"
            print("Approved." if ok else "Could not approve (unknown stage?).")
            return 0 if ok else 1
        if args.apply_stage:
            return apply_update_stage(args.apply_stage, yes=args.yes)
        if getattr(args, "fetch", False):
            from .updater import fetch_update_candidate
            r = fetch_update_candidate(args.source if args.source else None)
            if not r.get("ok"):
                print(f"Fetch failed: {r.get('error')}")
                return 1
            print(f"Fetched + staged {r['stage_id']} ({r.get('current_version','?')} -> {r.get('target_version','?')}).")
            if r.get("fetched_from"):
                print(f"  from: {r['fetched_from']}")
            if not r.get("is_newer"):
                print("  note: target version is not newer than current.")
            print(f"  check:   mongoose update --check-stage {r['stage_id']}")
            print(f"  approve: mongoose update --approve-stage {r['stage_id']}")
            print(f"  apply:   mongoose update --apply-stage {r['stage_id']} --yes")
            return 0
        if getattr(args, "stage_asset", None):
            from .services import assets as _assets
            rec = _assets.get_asset(args.stage_asset)
            if not rec:
                print(f"No asset with id {args.stage_asset}.")
                return 1
            if not rec.get("available_for_self_update"):
                print(f"Asset {args.stage_asset} ({rec.get('kind')}) is not a valid Mongoose build "
                      f"(needs pyproject.toml + src/mongoose), so it can't be staged for self-update.")
                return 1
            from pathlib import Path as _P
            src = _P(rec["stored_path"])
            if not src.exists():
                print(f"Asset file is missing on disk: {src}")
                return 1
            r = stage_update_candidate(src)
            if not r or not r.get("stage_id"):
                print("Staging failed.")
                return 1
            print(f"Staged build asset {args.stage_asset} as {r['stage_id']} "
                  f"({r.get('current_version','?')} -> {r.get('target_version','?')}).")
            print(f"  check:   mongoose update --check-stage {r['stage_id']}")
            print(f"  approve: mongoose update --approve-stage {r['stage_id']}")
            print(f"  apply:   mongoose update --apply-stage {r['stage_id']} --yes")
            return 0
        if args.stage:
            if not args.source:
                print("Provide a source path/zip to stage.")
                return 1
            r = stage_update_candidate(args.source)
            if not r or not r.get("stage_id"):
                print("Staging failed.")
                return 1
            print(f"Staged candidate {r['stage_id']} ({r.get('current_version','?')} -> {r.get('target_version','?')}).")
            print(f"  check:   mongoose update --check-stage {r['stage_id']}")
            print(f"  approve: mongoose update --approve-stage {r['stage_id']}")
            print(f"  apply:   mongoose update --apply-stage {r['stage_id']} --yes")
            return 0
        if args.rollback:
            return run_update_flag("--rollback")
        if args.list_backups:
            return run_update_flag("--list-backups")
        if not args.source:
            print("Provide a source path/zip, or use --rollback / --list-backups / --stage.")
            return 1
        if args.raw:
            return run_update(args.source)
        install = "auto" if args.install_missing else ("never" if args.no_install else "ask")
        return run_update_guided(args.source, install_missing=install)
    elif args.command == "simulate-surpriser":
        from .services.surpriser_sim import simulate, format_simulation
        res = asyncio.run(simulate(days=args.days, seed=not args.no_seed,
                                   source_db=(args.source_db or None),
                                   hour=args.hour,
                                   scheduler_path=getattr(args, "scheduler_path", False)))
        print(format_simulation(res))
        return 0
    elif args.command == "liveness":
        from .liveness import liveness, format_liveness
        print(format_liveness(liveness()))
        return 0
    elif args.command == "_install-step":
        from .install_progress import set_step, begin_install
        if args.begin:
            begin_install()
        set_step(args.key, args.status, args.detail)
        return 0
    elif args.command == "_install-phase":
        from .install_progress import install_phase
        ph = install_phase()
        field = getattr(args, "field", "") or ""
        if field:
            val = ph.get(field, "")
            # booleans printed as the shell-friendly lowercase strings true/false
            if isinstance(val, bool):
                print("true" if val else "false")
            else:
                print(val)
        else:
            print(json.dumps(ph, indent=2))
        return 0
    elif args.command == "validate-home":
        from .services.home_router import HomeCommand, route_home_command
        from .tools import _infer_domain_service
        eid = args.entity_id
        if "." not in eid:
            print(f"entity_id must look like 'light.spare' (got {eid!r})")
            return 1
        cmd = _infer_domain_service(HomeCommand(
            room="validate", device_id="validate", action=args.action,
            entity_id=eid, privacy="low"))
        print(f"Routing {cmd.domain}.{cmd.service} -> {cmd.entity_id} (HA-first, Alexa fallback if allowed)...")
        out = asyncio.run(route_home_command(cmd))
        print(f"  ok={out['ok']} route={out.get('route')}")
        for a in out.get("attempts", []):
            extra = ""
            if a.get("response") and isinstance(a["response"], dict) and a["response"].get("outcome"):
                extra = f" [{a['response']['outcome']}]"
            print(f"    {a['route']}: {'OK' if a['success'] else 'failed'}"
                  f"{(', ' + a['reason']) if a.get('reason') else ''}{extra}")
        print(f"  {out.get('say','')}")
        return 0 if out["ok"] else 1
    elif args.command == "readiness":
        if getattr(args, "refresh", False):
            from .services.readiness import refresh_readiness
            refresh_readiness()
        from .services.readiness_presenter import readiness_for_voice, needs_me_for_voice
        if args.needs_me:
            print(needs_me_for_voice()["speech"])
            return 0
        if args.voice:
            print(readiness_for_voice()["speech"])
            return 0
        from .services.readiness import readiness_report
        rep = readiness_report()
        head = {"ready": "READY", "degraded": "USABLE (with gaps)", "not_ready": "NOT READY"}.get(rep["overall"], rep["overall"])
        print(f"Mongoose: {head}")
        if rep["attention_count"]:
            print(f"  {rep['attention_count']} item(s) need attention.\n")
        # group by what needs you vs what's fine
        attention = [it for it in rep["items"] if it.get("severity") in ("critical", "warning")]
        fine = [it for it in rep["items"] if it.get("severity") not in ("critical", "warning")]
        if attention:
            print("Needs you / not fully working:")
            for it in attention:
                na = it.get("next_action") or {}
                fix = f"  -> {na.get('label')}" if na.get("label") else ""
                print(f"  [{it['status']:18}] {it['label']}: {it.get('why','')}{fix}")
            print()
        print("Set up / working:")
        for it in fine:
            print(f"  [{it['status']:18}] {it['label']}")
        return 0
    elif args.command in ("alarm", "timer", "reminder"):
        from .services import time_alerts as TA
        kind = args.command
        subcmd = getattr(args, f"{kind}_cmd", None)
        if subcmd == "set":
            phrase = " ".join(args.when)
            r = TA.set_from_text(phrase, kind=kind, targets=getattr(args, "targets", None))
            print(r["say"])
            if r.get("alert"):
                print(f"  (id: {r['alert']['id']})")
            return 0 if r.get("ok") else 1
        elif subcmd == "list":
            alerts = TA.list_alerts(kind=kind)
            if not alerts:
                print(f"No upcoming {kind}s.")
                return 0
            for a in alerts:
                _print_alert(a)
            return 0
        elif subcmd == "cancel":
            if args.id == "all":
                actives = TA.list_alerts(kind=kind)
                if not actives:
                    print(f"No {kind}s to cancel.")
                    return 0
                if not args.yes:
                    print(f"This cancels {len(actives)} {kind}(s). Re-run with --yes to confirm.")
                    return 0
                for a in actives:
                    TA.cancel_alert(a["id"])
                print(f"Cancelled {len(actives)} {kind}(s).")
                return 0
            res = TA.cancel_alert(args.id)
            print("Cancelled." if res else "No alert with that id (or already fired/cancelled).")
            return 0
        else:
            print(f"Use: mongoose {kind} set|list|cancel")
            return 0
    elif args.command == "body":
        from .services import body_state as BS, body_readiness as BR
        sub_cmd = getattr(args, "body_cmd", None)
        if sub_cmd == "log":
            from .services.body_log_parse import log_from_text
            print(log_from_text(" ".join(args.text))["say"])
            return 0
        elif sub_cmd == "readiness":
            est = BR.load_estimate()
            print(est["speech"])
            print(f"\n({est['disclaimer']})")
            return 0
        elif sub_cmd == "checkin":
            felt = max(0, min(5, args.load))
            BR.record_checkin(felt_load=felt)
            print(f"Logged: you feel {felt}/5 loaded. That helps me learn your patterns.")
            return 0
        elif sub_cmd == "today":
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).date()
            any_logged = False
            for kind in BS.KINDS:
                for e in BS.list_entries(kind, limit=20):
                    try:
                        d = datetime.fromisoformat(e["at"].replace("Z", "+00:00")).date()
                    except Exception:
                        continue
                    if d == today:
                        any_logged = True
                        note = e.get("notes") or e.get("focus") or kind
                        print(f"  {kind}: {note}")
            if not any_logged:
                print("Nothing logged today yet.")
            return 0
        elif sub_cmd == "history":
            recent = BS.recent_all(days=args.days)
            for kind, items in recent.items():
                if items:
                    print(f"{kind} ({len(items)}):")
                    for e in items[:5]:
                        print(f"  {e['at'][:10]}  {e.get('notes') or e.get('focus') or e.get('mode') or ''}")
            return 0
        elif sub_cmd == "import":
            from .services import alpha_import as AI

            def _read(p):
                return open(p, encoding="utf-8").read() if p else None
            res = AI.import_all(sessions=_read(args.sessions), sets=_read(args.sets),
                                body_comp=_read(args.body_comp))
            for k, v in res.get("summary", {}).items():
                print(f"  {k}: imported {v['imported']}")
            if not res.get("summary"):
                print("Nothing imported, pass --sessions, --sets and/or --body-comp paths.")
            return 0
        elif sub_cmd in ("checkin-flow", "reset", "journal"):
            from .services import body_literacy as BL
            cid = "cli"
            # CLI flows are one-shot starts; the interactive back-and-forth happens by voice/chat.
            # Here we just kick off and show the first prompt (or run a reset's instructions).
            if sub_cmd == "checkin-flow":
                print(BL.start_checkin(cid)["speech"])
                print("\n(Continue this by voice or chat, the flow remembers where you are.)")
            elif sub_cmd == "reset":
                print(BL.start_reset(cid, kind=getattr(args, "kind", ""))["speech"])
            else:
                print(BL.start_journal(cid, kind=getattr(args, "kind", ""))["speech"])
                print("\n(Continue this by voice or chat.)")
            return 0
        else:
            print("Use: mongoose body log|readiness|checkin|today|history")
            return 0
    elif args.command == "rtm":
        from .services import rtm_intake as R
        rc = getattr(args, "rtm_cmd", None)
        if rc == "status":
            s = R.status()
            print(_profile_text("{{rtm.name}} RTM") + f" ({s['address']})")
            print(f"  open cases: {s['open_cases']} | pending drafts: {s['pending_drafts']}")
            print(f"  live mailbox: {'ON' if s['live_mailbox'] else 'off'} | "
                  f"live send: {'ON' if s['live_send'] else 'off'}")
            print(f"  draft tier: {s.get('draft_tier', 'local template only')}")
            if not s['live_mailbox'] and not s['live_send']:
                print("  (safe mode: not monitoring or sending for real)")
            return 0
        elif rc == "cases":
            for c in R.list_cases():
                print(f"  [{c['urgency']}/{c['responsibility']}] {c['subject']}, {c['sender_email']} ({c['id']})")
            return 0
        elif rc == "obligations":
            from .services.rtm_obligations import upcoming_obligations, s20_timeline
            if getattr(args, "s20_noi", None):
                t = s20_timeline(args.s20_noi)
                if not t.get("ok"):
                    print("Error:", t.get("error"))
                    return 1
                print(f"s.20 consultation timeline (Notice of Intention {t['notice_of_intention']}):")
                print(f"  Stage 1 observation ends: {t['stage1_observation_ends']}")
                print(f"  Stage 2 observation ends: {t['stage2_observation_ends']}")
                print(f"  Earliest works commitment: {t['earliest_works_commitment']} ({t['days_until_can_commit']:+d}d)")
                print(f"  Basis: {t['basis']}")
                return 0
            obs = upcoming_obligations()
            if not obs:
                print("No statutory obligations within the horizon.")
                return 0
            print("RTM statutory obligations (soonest first):")
            for o in obs:
                flag = "!" if o["band"] in ("overdue", "due_soon") else " "
                print(f"  {flag} [{o['band']:8}] {o['due_date']} ({o['days_remaining']:+d}d)  {o['title']}")
                print(f"        basis: {o['basis']}")
            return 0
        elif rc == "drafts":
            for d in R.list_drafts("pending"):
                print(f"  {d['id']} [{d['kind']}] to {d['to_email']}: {d['subject']}")
            return 0
        elif rc == "import-voicemail":
            res = R.import_voicemail_dir(args.root)
            if not res.get("ok"):
                print(f"Could not import: {res.get('reason')} ({res.get('root')})")
            else:
                print(f"Scanned {res['scanned']}, imported {res['imported']}, skipped {res['skipped']}.")
            return 0
        elif rc == "search":
            hits = R.search_intake(args.query, args.limit)
            if not hits:
                print("No matches.")
            for h in hits:
                u = h.get("classification", {}).get("urgency", "?")
                print(f"  [{u}] {h['kind']}: {h['subject']} ({h['intake_id']})")
            return 0
        elif rc == "list":
            for it in R.list_intake(args.limit):
                u = it.get("classification", {}).get("urgency", "?")
                print(f"  [{u}] {it['kind']}: {it['subject']}, {it['sender']} ({it['id']})")
            return 0
        elif rc == "intake":
            res = asyncio.run(R.handle_incoming(sender=args.sender, subject=args.subject,
                                                body=args.body, auto_push=False))
            cls = res["classification"]
            print(f"Case {res['case_id']}, urgency={cls['urgency']} responsibility={cls['responsibility']} "
                  f"category={cls['category']} routine={cls['routine']}")
            if res["draft"].get("ok"):
                print(f"\nDraft ({res['draft']['kind']}) to {res['draft']['to']}:")
                print(f"Subject: {res['draft']['subject']}")
                print(res['draft']['body'])
            return 0
        elif rc == "poll":
            r = R.poll_once()
            print(r.get("note") or f"Polled: {r}")
            return 0
        elif rc == "approve":
            r = R.approve_draft(args.draft_id)
            print(r.get("note") or f"{r}")
            return 0
        elif rc == "cancel":
            R.cancel_draft(args.draft_id)
            print(f"Cancelled {args.draft_id}.")
            return 0
        elif rc == "secretary":
            from .services import rtm_secretary as S
            sc = getattr(args, "secretary_cmd", None)
            if sc in (None, "status"):
                print(S.format_status(S.status_report()))
                return 0
            if sc == "discover":
                print(S.format_status(S.compute_compliance()))
                return 0
            if sc == "configure":
                S.configure_profile(company_name=args.company_name, company_number=args.company_number,
                                    incorporation_date=args.incorporation_date,
                                    accounting_reference_date=args.accounting_reference_date,
                                    confirmation_statement_date=args.confirmation_statement_date,
                                    corporation_tax_period_end=args.corporation_tax_period_end,
                                    dormant_for_corporation_tax=args.dormant_for_corporation_tax,
                                    vat_registered=args.vat_registered, payroll_registered=args.payroll_registered,
                                    notes=args.notes)
                print(S.format_status(S.compute_compliance()))
                return 0
            if sc == "done":
                print(S.mark_done(args.item_id, args.note))
                return 0
            if sc == "guidance":
                g = S.guidance(args.item_id)
                if not g.get("ok"):
                    print(g.get("error", "unknown item"))
                else:
                    it = g["item"]
                    print(f"{it['title']} ({it['id']})")
                    for i, step in enumerate(g["steps"], 1):
                        print(f"  {i}. {step}")
                return 0
            if sc == "calendar":
                print(S.export_calendar_csv(args.path))
                return 0
            if sc == "artefacts":
                print(S.artifact_pack(args.dir))
                return 0
            print("Use: mongoose rtm secretary status|discover|configure|done|guidance|calendar|artefacts")
            return 0
        elif rc == "books":
            from .services import rtm_bookkeeper as B
            bc = getattr(args, "books_cmd", None)
            if bc in (None, "summary"):
                print(B.format_summary(B.summary()))
                return 0
            if bc == "weekly":
                wp = B.weekly_prompt()
                print(wp["title"])
                for q in wp["questions"]:
                    print(f"  - {q}")
                return 0
            if bc == "opening":
                print(B.record_opening_balance(args.fund, args.amount, args.date, args.notes))
                return 0
            if bc == "balance":
                print(B.record_bank_balance(fund=args.fund, balance=args.amount, as_at=args.date,
                                            bank_account_name=args.account_name, notes=args.notes))
                return 0
            if bc == "tx":
                print(B.record_transaction(fund=args.fund, amount=args.amount, direction=args.direction,
                                           occurred_on=args.date, description=" ".join(args.description),
                                           category=args.category, counterparty=args.counterparty,
                                           reference=args.reference, notes=args.notes))
                return 0
            if bc == "transfer":
                print(B.record_transfer(from_fund=args.from_fund, to_fund=args.to_fund, amount=args.amount,
                                        occurred_on=args.date, description=args.description,
                                        reference=args.reference, notes=args.notes))
                return 0
            if bc == "reconcile":
                print(json.dumps(B.reconcile(), indent=2))
                return 0
            if bc == "chart":
                print(B.chart())
                return 0
            if bc == "export":
                print(B.export_csv(args.path, fund=args.fund or None))
                return 0
            print("Use: mongoose rtm books summary|weekly|opening|balance|tx|transfer|reconcile|chart|export")
            return 0
        elif rc == "trust":
            from .services import rtm_trust as TR
            tc = getattr(args, "trust_cmd", None)
            if tc == "receipt-build":
                amount_pence = TR._money_to_pence(args.amount)
                res = TR.build_receipt(args.flat, args.document, args.payment_ref, amount_pence,
                                       args.received, method=args.method)
                print(json.dumps(res, indent=2))
                return 0 if res.get("ok") else 1
            if tc == "receipt-render":
                # Resolve the branded receipt pack (receipt then demand) via the template engine,
                # building it if not stored yet. The pack always includes the demand, so attach_demand
                # is moot now.
                from .services.rtm_portal import resolve_receipt_pdf
                path = resolve_receipt_pdf(args.flat, args.document)
                if not path:
                    print(json.dumps({"ok": False, "reason": "receipt_not_found; build it first"}, indent=2))
                    return 1
                print(json.dumps({"ok": True, "path": path}, indent=2))
                return 0
            if tc == "reserve-position":
                print(json.dumps(TR.reserve_position(), indent=2))
                return 0
            if tc == "reconcile-weekly":
                print(json.dumps(TR.reconcile_weekly(as_of=args.as_of or None), indent=2))
                return 0
            if tc == "transfers-status":
                print(json.dumps(TR.transfer_status(as_of=args.as_of or None), indent=2))
                return 0
            print("Use: mongoose rtm trust receipt-build|receipt-render|reserve-position|reconcile-weekly|transfers-status")
            print("(records and checks only; never moves money)")
            return 0
        elif rc == "manage":
            from .services import rtm_management as M
            mc = getattr(args, "manage_cmd", None)
            if mc == "dashboard":
                print(json.dumps(M.management_dashboard(), indent=2))
                return 0
            if mc == "import-flats":
                print(json.dumps(M.import_flats_csv(args.path), indent=2))
                return 0
            if mc == "import-demands":
                print(json.dumps(M.import_demands_csv(args.path), indent=2))
                return 0
            if mc == "export-pack":
                print(json.dumps(M.export_management_pack(args.dir, year=args.year or None), indent=2))
                return 0
            if mc in (None, "status"):
                print(M.format_status())
                return 0
            if mc == "seed-flats":
                print(M.seed_flats())
                return 0
            if mc == "flats":
                for f in M.list_flats():
                    ref = f.get("payment_reference") or "(no ref)"
                    name = f.get("leaseholder_name") or "(no name)"
                    print(f"  {f['flat_id']:6} {name} · ref {ref} · {f.get('apportionment_band') or 'unknown'}")
                return 0
            if mc == "flat":
                print(M.upsert_flat(block=args.block, flat_number=args.number, leaseholder_name=args.name,
                                    correspondence_address=args.address, apportionment_band=args.apportionment,
                                    payment_reference=args.payment_reference, notes=args.notes))
                return 0
            if mc == "demand":
                print(M.create_demand(document_number=args.document_number, flat=args.flat, period=args.period,
                                      amount=args.amount, issue_date=args.issue_date, due_date=args.due_date,
                                      delivery_method=args.delivery_method, revision=args.revision,
                                      supersedes=args.supersedes, schedule=args.schedule, notes=args.notes))
                return 0
            if mc == "demands":
                for d in M.list_demands(status=args.status or None, flat=args.flat or None, limit=args.limit):
                    print(f"  {d['document_number']} {d['flat_id']} {d['period']} {d['amount']} paid {d['allocated']} outstanding {d['outstanding']} [{d['status']}]")
                return 0
            if mc == "statement":
                st = M.flat_statement(args.flat)
                print(f"{st['flat_id']} balance {st['balance']} ({st['position']})")
                for e in st["entries"]:
                    print(f"  {e['entry_date']} {e['kind']:16} {e['signed']:>12} -> {e['running']:>12} {e['description']}")
                return 0
            if mc == "metro-import":
                print(json.dumps(M.import_metro_csv(args.path), indent=2))
                return 0
            if mc == "review-payments":
                q = M.allocation_review_queue()
                print(f"{q['count']} transaction(s) need review")
                for t in q["transactions"]:
                    print(f"  {t['id']} {t['bank_date']} {t['amount']} [{t['match_status']}] {t['reference'] or t['description']}")
                return 0
            if mc == "allocate":
                allocs = []
                for item in args.allocations:
                    if ":" not in item:
                        raise SystemExit("allocations must look like DOC:AMOUNT")
                    doc, amount = item.rsplit(":", 1)
                    allocs.append({"document_number": doc, "amount": amount})
                print(M.allocate_payment(args.bank_transaction_id, allocs))
                return 0
            if mc == "arrears":
                a = M.arrears()
                print(f"Arrears as at {a['as_at']}: {a['count']} item(s)")
                for it in a["items"]:
                    print(f"  {it['flat_id']} {it['document_number']} {it['amount_outstanding']} · {it['days_overdue']}d · {it['next_action']['label']}")
                return 0
            if mc == "reminder":
                print(json.dumps(M.generate_reminder(args.document_number, action_type=args.type or None), indent=2))
                return 0
            if mc == "release-reminder":
                print(M.release_recovery_action(args.action_id, args.method))
                return 0
            if mc == "referral-pack":
                print(json.dumps(M.referral_pack(flat=args.flat or None, document_number=args.document_number or None), indent=2))
                return 0
            if mc == "payable":
                print(M.add_payable(supplier=args.supplier, description=args.description, schedule=args.schedule,
                                    amount=args.amount, invoice_date=args.invoice_date, due_date=args.due_date,
                                    budget_line=args.budget_line, vat_rate=args.vat_rate,
                                    vat_treatment=args.vat_treatment, cadence=args.cadence, notes=args.notes))
                return 0
            if mc == "payables":
                for p in M.list_payables(status=args.status or None):
                    print(f"  {p['id']} {p['due_date']} {p['supplier']}: {p['amount']} [{p['status']}] {p['budget_line']}")
                return 0
            if mc == "approve-payable":
                print(M.approve_payable(args.payable_id))
                return 0
            if mc == "pay-payable":
                print(M.mark_payable_paid(args.payable_id, amount=args.amount, paid_date=args.date, bank_reference=args.bank_reference))
                return 0
            if mc == "cashflow":
                print(json.dumps(M.cashflow(), indent=2))
                return 0
            if mc == "budget":
                print(M.set_budget_line(year=args.year, schedule=args.schedule, budget_line=args.line, amount=args.amount, notes=args.notes))
                return 0
            if mc == "budget-actuals":
                print(json.dumps(M.budget_actuals(year=args.year or None), indent=2))
                return 0
            if mc == "evidence":
                print(M.add_evidence(target_type=args.target_type, target_id=args.target_id,
                                     document_kind=args.document_kind, title=args.title,
                                     path=args.path, notes=args.notes))
                return 0
            if mc == "evidence-list":
                for e in M.list_evidence(target_type=args.target_type or None, target_id=args.target_id or None):
                    print(f"  {e['id']} {e['target_type']}:{e['target_id']} {e['document_kind']} · {e['title']} {e.get('path') or ''}")
                return 0
            print("Use: mongoose rtm manage status|dashboard|seed-flats|flats|import-flats|flat|demand|import-demands|demands|statement|metro-import|review-payments|allocate|arrears|reminder|release-reminder|referral-pack|payable|payables|approve-payable|pay-payable|cashflow|budget|budget-actuals|evidence|evidence-list|export-pack")
            return 0
        elif rc == "capability":
            from .services import rtm_capability as C
            st = C.rtm_state()
            cap = st["capability"]
            print(f"RTM capability: {cap.get('summary', '?')}")
            for r in cap.get("requirements", []):
                print(f"  {r['requirement']} {r['label']}: {r['state']} ({r['why']})")
            b = st["build"]
            print(f"Build candidates: {b['by_status'] or 'none'} | lock: "
                  f"{'held by ' + b['lock']['holder'] if b['lock'].get('held') else 'free'}")
            return 0
        elif rc == "candidates":
            from .services import rtm_capability as C
            act = getattr(args, "action", "list")
            if act == "new":
                r = C.create_candidate(args.goal, approach=args.approach, origin="adhoc")
                print(f"Created candidate {r['id']} for goal '{r['goal']}'.")
            elif act == "promote":
                r = C.promote_candidate(args.id)
                print(r.get("reason") and f"Could not promote: {r['reason']}" or
                      f"Promoted {r['chosen']}; superseded {r['superseded']}.")
            elif act == "prune":
                r = C.prune_candidates(goal=args.goal or None)
                print(f"Pruned {len(r['pruned'])} candidate(s); {r['remaining']} remain.")
            else:
                for c in C.list_candidates(args.goal or None):
                    print(f"  {c['id']} [{c['status']}] {c['goal']} ({c.get('approach') or 'no approach'}) origin={c['origin']}")
            return 0
        elif rc == "build":
            from .services import rtm_capability as C
            from .services import builder as _b
            from .services.build_plans import build_example_court_management_plan
            lock = C.acquire_build_lock("adhoc:cli")
            if not lock["ok"]:
                print(f"Build lock held by {lock.get('held_by')} (a scheduled job is building). Try again shortly.")
                return 0
            try:
                plans = _b.list_plans()
                from .services.build_plans import plan_name
                mgmt = next((p for p in plans if p.get("name") == plan_name("management")), None)
                if not mgmt:
                    created = build_example_court_management_plan()
                    print(f"Created management build plan {created['plan_id']} (unapproved). Approve it, then build.")
                elif not mgmt.get("approved"):
                    print(f"Management build plan {mgmt['plan_id']} awaits approval (mongoose build-approve {mgmt['plan_id']}).")
                else:
                    summary = _b.run_plan(mgmt["plan_id"], max_steps=args.steps)
                    print(f"Advanced build by up to {args.steps} step(s): {summary}")
            finally:
                C.release_build_lock(lock["token"])
            return 0
        elif rc == "sync":
            from .services import sync_checkpoint as SC
            act = getattr(args, "action", "now")
            if act == "channels":
                for ch, s in SC.channel_status().items():
                    print(f"  {ch}: enabled={s['enabled']} deliverable={s['deliverable']} ({s['reason']})")
            elif act == "history":
                for c in SC.checkpoint_history():
                    print(f"  {c['at']} {c['summary']}")
            else:
                r = asyncio.run(SC.run_checkpoint())
                cp = r["checkpoint"]
                print(f"Checkpoint {cp['id']}: {cp['summary']}")
                for q in cp["asking"]:
                    print(f"  ? {q}")
            return 0
        elif rc == "load-real-data":
            from .services import rtm_example_court as PC
            r = PC.apply_real_data(with_demands=False)
            print(r.get("reason") and f"Could not load: {r['reason']}" or
                  f"Loaded {r['flats_loaded']} flats + {r['budget_lines_loaded']} budget lines.")
            return 0
        elif rc == "load-real-demands":
            from .services import rtm_example_court as PC
            if not args.confirm:
                print("This creates the 18 real half-year demands (not marked served). Re-run with --confirm.")
                return 0
            r = PC.apply_real_data(with_demands=True)
            print(f"Loaded {r['flats_loaded']} flats, {r['budget_lines_loaded']} budget lines, "
                  f"{r['demands_created']} demands. Caveats: {len(r['caveats'])} (see rtm real-data-knowledge).")
            return 0
        elif rc == "real-data-knowledge":
            from .services import rtm_example_court as PC
            k = PC.knowledge_summary()
            print(_profile_text("{{rtm.name}} operating facts:"))
            for kk, vv in k["facts"].items():
                print(f"  {kk}: {vv}")
            print("Caveats:")
            for c in k["caveats"]:
                print(f"  [{c['severity']}] {c['code']}: {c['detail'][:90]}")
            return 0
        elif rc == "portal":
            import json as _pjj
            from .services.rtm_portal import (
                portal_readiness, build_portal_snapshot, publish_portal_snapshot,
                pull_pending_claims, process_pending_claims, pull_portal_issues,
                generate_claim_keypair, get_claim_public_key, attach_demand_pdf, portal_brain_summary,
                generate_snapshot_signing_key, get_snapshot_signing_public_key, check_snapshot_drift,
            )
            pcmd = getattr(args, "portal_cmd", None) or "readiness"
            if pcmd == "readiness":
                result = portal_readiness()
            elif pcmd == "snapshot":
                result = build_portal_snapshot()
            elif pcmd == "publish":
                result = publish_portal_snapshot()
            elif pcmd == "pull-claims":
                result = pull_pending_claims()
            elif pcmd == "process-claims":
                result = process_pending_claims()
            elif pcmd == "pull-issues":
                result = pull_portal_issues()
            elif pcmd == "generate-keypair":
                result = generate_claim_keypair()
            elif pcmd == "public-key":
                result = get_claim_public_key()
            elif pcmd == "generate-signing-key":
                result = generate_snapshot_signing_key()
            elif pcmd == "signing-public-key":
                result = get_snapshot_signing_public_key()
            elif pcmd == "check-drift":
                result = check_snapshot_drift()
            elif pcmd == "seed-flats":
                from .services.rtm_management import seed_flats
                result = seed_flats()
            elif pcmd == "attach-demand-pdf":
                dn = getattr(args, "document_number", "")
                pp = getattr(args, "path", "")
                result = attach_demand_pdf(document_number=dn, path=pp)
            else:
                result = portal_brain_summary()
            print(_pjj.dumps(result, indent=2, default=str))
            return 0
        else:
            print("Use: mongoose rtm status|cases|drafts|intake|poll|approve|cancel|secretary|books|manage|"
                  "capability|candidates|build|sync|load-real-data|load-real-demands|real-data-knowledge")
            return 0
    elif args.command == "unstuck":
        from .services import unstuck as U
        if args.did:
            U.record_did(args.did, helped=True if args.helped else None)
            print(f"Noted you did: {args.did}" + (", and it helped." if args.helped else "."))
        elif args.what_helps:
            print(U.what_helps()["speech"])
        else:
            print(U.unstick(force=True)["speech"])
        return 0
    elif args.command == "delight":
        from .services import delight as D
        d = D.delight(kind=args.kind, interest=(args.interest or None),
                      allow_model=not args.no_model)
        print(d["text"])
        if d.get("kind") == "fact" and d.get("source") == "model":
            print("  (model-generated, worth a sanity check before repeating as fact)")
        return 0
    elif args.command == "diagnostics":
        from .services import diagnostics as D
        dc = getattr(args, "diag_cmd", None)
        if dc == "status":
            st = D.report_status()
            print("Diagnostics report status:")
            print(f"  {'READY' if st['ready'] else 'accumulating'}" +
                  (f" (reason: {st['reason']})" if st['ready'] else ""))
            if st.get("event_count"):
                print(f"  events: {st['event_count']} (fast {st.get('fast_lane',0)}, slow {st.get('slow_lane',0)})")
                print(f"  weight: {st['accumulated_weight']}/{st['weight_threshold']}  |  "
                      f"oldest: {st['oldest_age_hours']}h/{st['max_age_hours']}h")
            else:
                print("  no events yet")
            return 0
        elif dc == "export":
            exp = D.export_report(mark_reported=not args.no_reset)
            print(f"Exported {exp['event_count']} events.")
            print(f"  markdown: {exp['md_path']}")
            print(f"  json:     {exp['json_path']}")
            print("Upload these to Claude. " +
                  ("Accumulator reset; collection continues." if exp["reset"]
                   else "(not reset; events still pending)"))
            return 0
        elif dc == "recent":
            for e in D.recent_events(args.limit, lane=getattr(args, "lane", None)):
                print(f"  [{e['kind']}/{e['scope']}] w{e['weight']} {e['lane']}: {e['summary']}")
            return 0
        else:
            print("Use: mongoose diagnostics status|export|recent")
            return 0
    elif args.command == "rehearse":
        from .dress_rehearsal import dress_rehearsal, format_rehearsal
        report = asyncio.run(dress_rehearsal())
        print(format_rehearsal(report))
        return 1 if report["has_fail"] else 0
    elif args.command == "intro":
        from .services import tutorial as TUT
        if args.cool:
            r = asyncio.run(TUT.do_something_cool())
            if r.get("is_sample"):
                print("(example, not your real data)\n")
            print(r["speech"])
        else:
            print(TUT.intro_speech())
            TUT.mark_intro_seen()
        return 0
    elif args.command == "alerts":
        from .services import time_alerts as TA
        alerts = TA.list_alerts()
        if not alerts:
            print("No upcoming alarms, timers or reminders.")
            return 0
        for a in alerts:
            _print_alert(a)
        return 0
    elif args.command == "system-map":
        from .transparency import system_map, format_system_map
        print(format_system_map(system_map()))
        return 0
    elif args.command == "receipts":
        from .services.receipts import receipts_ledger, format_ledger
        ledger = receipts_ledger(limit=args.limit, source=(args.source or None))
        print(format_ledger(ledger))
        return 0
    elif args.command == "suggest":
        from .services.surpriser import pick_surprise, record_feedback
        if args.feedback:
            if not args.signature:
                print("Provide --signature with --feedback (shown after a suggestion).")
                return 1
            r = record_feedback(args.signature, args.feedback)
            print("Thanks, noted." if r["ok"] else f"Failed: {r.get('error')}")
            return 0 if r["ok"] else 1
        # on-demand: force past quiet-hours/min-gap, but still novel + grounded
        d = asyncio.run(pick_surprise(force=True, source="on_demand"))
        if not d["surfaced"]:
            print(f"Nothing worth surfacing right now ({d.get('reason')}).")
            return 0
        s = d["suggestion"]
        print(s["text"])
        print(f"\n(why: {s['why']})")
        print(f"feedback: mongoose suggest --feedback useful --signature {s['signature']}")
        return 0
    elif args.command == "daily":
        from .services.daily_picture import daily_operating_picture, format_dop
        dop = asyncio.run(daily_operating_picture())
        print(format_dop(dop))
        return 0
    elif args.command == "prepare":
        from .services.prepare import prepare_card, format_prepare
        topic = " ".join(args.topic).strip() or "today"
        card = asyncio.run(prepare_card(topic))
        print(format_prepare(card))
        return 0
    elif args.command == "devices":
        from . import devices as D
        if args.action == "list":
            ds = D.list_devices()
            if not ds:
                print("No devices paired. Pair one with: mongoose devices pair")
                return 0
            for d in ds:
                state = "REVOKED" if d["revoked"] else "active"
                print(f"  {d['name']} [{state}] · {d['device_type']} · {d.get('profile','')}")
                print(f"    id={d['device_id']} last_seen={d.get('last_seen','never')}")
                print(f"    capabilities: {', '.join(d.get('capabilities',[]))}")
            return 0
        elif args.action == "pair":
            c = D.create_pairing_code(args.profile)
            print(f"Pairing code (valid {c['expires_in']//60} min, single use): {c['code']}")
            print(f"Server fingerprint: {c['fingerprint']}")
            print("On the device, open:  http://<this-mac-ip>:<port>/pair?code=" + c["code"])
            print("Or use the cockpit Devices tab to show a scannable QR code.")
            return 0
        elif args.action == "revoke":
            if not args.id:
                print("Provide a device id: mongoose devices revoke --id <id>  (see: mongoose devices list)")
                return 1
            r = D.revoke_device(args.id)
            print(f"Revoked {args.id}." if r["ok"] else f"Failed: {r.get('error')}")
            return 0 if r["ok"] else 1
    elif args.command == "mail":
        from .mail_status import mail_status, sync_now
        if args.action == "status":
            st = mail_status()
            print(f"Mail sync status ({st['message_count']} messages indexed):")
            for p in st["providers"]:
                state = "authorized" if p["authorized"] else ("configured" if p["configured"] else "not set up")
                last = p.get("last_completed") or "never"
                line = f"  {p['provider']:10} {state}"
                if p["authorized"]:
                    line += f"; last sync {last}"
                    if p.get("last_status"):
                        line += f" ({p['last_status']})"
                print(line)
                if p["note"]:
                    print(f"             -> {p['note']}")
            if not st["any_active"]:
                print("\nNo mail provider is authorized yet. Pick one above and follow its hint.")
            return 0
        elif args.action == "sync":
            print("Syncing now (this runs the same cycle the scheduler runs)...")
            res = asyncio.run(sync_now())
            if not res["ok"]:
                print(f"Sync failed: {res.get('error')}")
                return 1
            print(f"Done. {res['new_messages']} new message(s); {res['message_count']} total indexed.")
            for prov, r in (res.get("providers") or {}).items():
                ok = r.get("ok", True) if isinstance(r, dict) else True
                detail = (r.get("error") or r.get("reason") or "ok") if isinstance(r, dict) else "ok"
                print(f"  {prov}: {'ok' if ok else 'failed'} ({detail})")
            return 0
    elif args.command == "protocol-v3":
        from .services import protocol_v3 as _p3
        action = getattr(args, "action", "status")
        if action == "handshake":
            print(json.dumps(_p3.version_handshake_message(), indent=2))
        elif action == "transport":
            print(json.dumps(_p3.transport_status(), indent=2))
        elif action == "domains":
            print(json.dumps(_p3.proposed_domains(), indent=2))
        elif action == "push-file":
            local = getattr(args, "local", None)
            remote = getattr(args, "remote", None)
            if not local or not remote:
                print("push-file needs --local and --remote")
                return 1
            r = _p3.push_file_to_peer(local, remote)
            if r.get("unchanged"):
                print(f"Unchanged: peer already has an identical {remote} (no transfer).")
            elif r.get("ok"):
                print(f"Transferred {local} -> {remote} (sha256 {r.get('sha256','')[:12]}…, verified={r.get('verified')}).")
            else:
                print(f"Failed: {r.get('error')}")
                return 1
            return 0
        else:
            print(json.dumps(_p3.supports_v3_report(), indent=2))
        return 0
    elif args.command == "authorize":
        from .oauth import authorize_google, authorize_microsoft
        if args.provider == "google":
            ok, msg = authorize_google()
        else:
            ok, msg = authorize_microsoft(todo_only=getattr(args, "todo_only", False))
        print(("OK " if ok else "FAILED ") + msg)
        if ok:
            # Self-wiring: bring the unlocked capabilities online now instead of waiting for the
            # next scheduled sync, so the user sees it working immediately.
            try:
                from .services.self_wiring import wire_after_authorize
                w = wire_after_authorize(args.provider)
                if w.get("wired"):
                    print(f"  Wired up: {', '.join(w['wired'])} (first sync run now).")
                for r in w.get("results", []):
                    if not r.get("ok"):
                        print(f"  ({r['capability']}: {r['detail']})")
            except Exception as exc:
                print(f"  (capability wiring deferred to next sync: {exc})")
        return 0 if ok else 1
    elif args.command == "merge-env":
        from .setup_wizard import merge_new_env_keys
        added = merge_new_env_keys()
        print(f"Added {len(added)} new key(s) to .env: {', '.join(added)}" if added else "No new keys to add.")
    elif args.command == "dedupe-env":
        from .setup_wizard import dedupe_env
        removed = dedupe_env()
        if removed:
            print(f"Removed duplicate(s), kept last value for: {', '.join(removed)}")
        else:
            print("No duplicate keys found.")
    elif args.command == "bus-send":
        from .services import bus
        r = bus.send_message(sender=args.sender, to=args.to, subject=args.subject,
                             type=args.type, body=args.body, re_id=args.re_id)
        print(f"Sent {r['id']}" if r.get("ok") else f"Error: {r.get('error')}")
    elif args.command == "bus":
        from .services import bus
        msgs = bus.list_messages(to=args.to, sender=args.sender, type=args.type,
                                 unread_only=not args.all)
        if not msgs:
            print("No messages.")
        for m in msgs:
            print(f"{m['created'][:19]}  [{m['type']:5}] {m['sender']} -> {m['recipient']}: "
                  f"{m['subject']}  ({m['status']})  id={m['id']}")
    elif args.command == "bus-read":
        from .services import bus
        m = bus.read_message(args.id, archive=not args.keep)
        if not m:
            print("No such message.")
        else:
            print(f"From: {m.get('from')}  To: {m.get('to')}  Type: {m.get('type')}")
            print(f"Subject: {m.get('subject')}\n")
            print(m.get("body", ""))
    elif args.command == "bus-search":
        from .services import bus
        for m in bus.search_messages(args.query):
            print(f"[{m['type']}] {m['subject']} (from {m['sender']}, {m['status']}) id={m['id']}")
    elif args.command == "bus-reindex":
        from .services import bus
        print(f"Indexed {bus.reindex()} messages.")
    elif args.command == "benchmark":
        from .benchmark import run_benchmark, DEFAULT_SHORTLIST, CANDIDATE_MODELS
        if getattr(args, "voice", None):
            from .voice_benchmark import run_voice_benchmark, format_voice_result
            r = asyncio.run(run_voice_benchmark(args.voice, audio_model=args.audio_model))
            print(format_voice_result(r))
            return 0
        if getattr(args, "results", False):
            from .benchmark import render_results_file
            render_results_file()
            return 0
        if getattr(args, "recommend", False):
            from .benchmark import load_results_for_recommend
            from .benchmark_recommend import recommend, format_recommendation, task_kind_map
            results, cold = load_results_for_recommend()
            if not results:
                print("No benchmark results yet. Run `mongoose benchmark ...` first, then "
                      "`mongoose benchmark --recommend`.")
                return 0
            rec = recommend(results, task_kind_map(), cold_load_by_model=cold)
            print(format_recommendation(rec))
            return 0
        models = args.models or DEFAULT_SHORTLIST
        if getattr(args, "candidates", False) and not args.models:
            models = list(models) + CANDIDATE_MODELS
        asyncio.run(run_benchmark(models, measure_cold=not args.no_cold,
                                  auto_pull=not args.no_pull, rounds=args.rounds,
                                  repeats=args.repeats, cooldown_s=args.cooldown))
    elif args.command == "tune":
        from . import model_tuning as MT
        if args.revert:
            r = MT.revert()
            print("Reverted to your base models." if r.get("ok") else "Nothing to revert.")
            return 0
        if args.dismiss:
            MT.dismiss()
            print("Dismissed. I won't raise this again until the recommendation changes.")
            return 0
        if args.apply:
            r = MT.apply_recommendation()
            if r.get("ok"):
                print("Applied the recommended models (reversible, `mongoose tune --revert` to undo):")
                for role, m in r["applied"].items():
                    print(f"  {role:9} -> {m}")
            else:
                print("Nothing to apply right now.")
            return 0
        # status / show
        ev = MT.evaluate(allow_auto_apply=False)
        status = MT.vesting_status()
        if not ev.get("vested"):
            stage = status["stage"]
            print(f"Model tuning: {stage.upper()}")
            print(f"  {status['reason']}")
            return 0
        if not ev.get("actionable"):
            print("Model tuning: VESTED")
            print("  You're already running the best setup for how you use Mongoose. Nothing to change.")
            return 0
        from .benchmark_recommend import format_recommendation
        print(format_recommendation(ev["recommendation"]))
        print()
        if ev.get("large_and_safe"):
            print("This is a clear, safe win, run `mongoose tune --apply` to switch (reversible).")
        else:
            print("Run `mongoose tune --apply` to switch (reversible), or `mongoose tune --dismiss`.")
        return 0
    elif args.command == "brief":
        from .services.brief import generate_brief
        out = asyncio.run(generate_brief(post_to_bus=args.post))
        print(out["brief"])
        if out["signals"]:
            print("\n(signals: " + "; ".join(out["signals"]) + ")")
        if args.post:
            print("\nPosted to the bus.")
    elif args.command == _profile_text("build-plan-{{rtm.slug}}"):
        from .services.build_plans import build_example_court_plan
        plan = build_example_court_plan()
        print(f"Created plan {plan['plan_id']} ('{plan['name']}') with {len(plan['steps'])} steps.")
        print("Review with: mongoose build-show " + plan["plan_id"])
        print("Then approve: mongoose build-approve " + plan["plan_id"])
    elif args.command == _profile_text("build-plan-{{rtm.slug}}-intake"):
        from .services.build_plans import build_example_court_intake_plan
        plan = build_example_court_intake_plan()
        print(f"Created plan {plan['plan_id']} ('{plan['name']}') with {len(plan['steps'])} steps.")
        print("Review with: mongoose build-show " + plan["plan_id"])
        print("Then approve: mongoose build-approve " + plan["plan_id"])
    elif args.command == _profile_text("build-plan-{{rtm.slug}}-governance"):
        from .services.build_plans import build_example_court_governance_plan
        plan = build_example_court_governance_plan()
        print(f"Created plan {plan['plan_id']} ('{plan['name']}') with {len(plan['steps'])} steps.")
        print("Review with: mongoose build-show " + plan["plan_id"])
        print("Then approve: mongoose build-approve " + plan["plan_id"])
    elif args.command == _profile_text("build-plan-{{rtm.slug}}-management"):
        from .services.build_plans import build_example_court_management_plan
        plan = build_example_court_management_plan()
        print(f"Created plan {plan['plan_id']} ('{plan['name']}') with {len(plan['steps'])} steps.")
        print("Review with: mongoose build-show " + plan["plan_id"])
        print("Then approve: mongoose build-approve " + plan["plan_id"])
    elif args.command == _profile_text("build-plan-{{rtm.slug}}-rtm-trust-reconciliation"):
        from .services.build_plans import build_example_court_rtm_trust_plan
        plan = build_example_court_rtm_trust_plan()
        print(f"Created plan {plan['plan_id']} ('{plan['name']}') with {len(plan['steps'])} steps.")
        print("Records and checks only; never moves money.")
        print("Review with: mongoose build-show " + plan["plan_id"])
        print("Then approve: mongoose build-approve " + plan["plan_id"])
    elif args.command == "build-new":
        from .services.builder import create_and_run
        out = create_and_run(args.description, name=args.name,
                             auto_approve=args.auto_approve,
                             post_to_bus=not args.no_bus,
                             code_call_budget=args.code_budget,
                             allow_shell=args.dangerously_allow_shell_checks,
                             max_seconds=int(args.max_hours * 3600) if args.max_hours else None)
        if not out.get("ok"):
            print("Error:", out.get("error"))
        elif out.get("awaiting_approval"):
            print(f"Planned {out['steps']} steps as {out['plan_id']}.")
            print("Review: mongoose build-show " + out["plan_id"])
            print("Approve+run: mongoose build-approve " + out["plan_id"] + " && mongoose build-run " + out["plan_id"])
        else:
            print(f"Ran {out['ran']} step(s). {out['done']}/{out['total']} done, "
                  f"{out['code_calls']} Code calls.")
            if out["blocked"]:
                print("BLOCKED:", ", ".join(out["blocked"]))
    elif args.command == "builds":
        from .services.builder import list_plans
        plans = list_plans()
        if not plans:
            print("No build plans.")
        for p in plans:
            print(f"{p['plan_id']}  {p['name']:28} {p['progress']:>7}  "
                  f"{'approved' if p['approved'] else 'NOT approved'}")
    elif args.command == "build-show":
        from .services.builder import load_plan
        plan = load_plan(args.plan_id)
        if not plan:
            print("No such plan.")
            return 1
        else:
            print(f"{plan['name']}  ({'approved' if plan['approved'] else 'NOT approved'})\n")
            for s in plan["steps"]:
                dep = f" <- {', '.join(s['depends_on'])}" if s["depends_on"] else ""
                print(f"  [{s['status']:7}] {s['id']:22} {s['title']}{dep}")
    elif args.command == "build-approve":
        from .services.builder import approve_plan
        if approve_plan(args.plan_id):
            print("Approved.")
        else:
            print("No such plan.")
            return 1
    elif args.command == "build-run":
        from .services.builder import run_plan
        max_seconds = int(args.max_hours * 3600) if args.max_hours else None
        out = run_plan(args.plan_id, post_to_bus=not args.no_bus, code_call_budget=args.code_budget,
                       allow_shell=args.dangerously_allow_shell_checks, max_seconds=max_seconds,
                       where=getattr(args, "where", "local"),
                       allow_api_key=getattr(args, "allow_pi_api_key", False))
        if not out.get("ok"):
            print("Error:", out.get("error"))
        else:
            extra = []
            if out.get("recovered"):
                extra.append(f"recovered {out['recovered']} stale")
            if out.get("hit_time_cap"):
                extra.append("hit time cap")
            print(f"Ran {out['ran']} step(s) in {out.get('elapsed_s', 0)}s. "
                  f"{out['done']}/{out['total']} done, {out['code_calls']} Code calls"
                  + (f" ({'; '.join(extra)})" if extra else "") + ".")
            if out["blocked"]:
                print("BLOCKED:", ", ".join(out["blocked"]))
    elif args.command == "build-recover":
        from .services.builder import recover_stale_steps
        n = recover_stale_steps(args.plan_id)
        print(f"Recovered {n} stale 'running' step(s)." if n else "No stale steps found.")
    elif args.command == "pi-build":
        from .services import remote_build as RB
        action = getattr(args, "action", "status")
        if action == "status":
            st = RB.status_report()
            print(json.dumps(st, indent=2))
            return 0 if st.get("ok") else 1
        # action == run
        if not getattr(args, "repo", None) or not getattr(args, "task", None):
            print("pi-build run needs --repo <local path> and --task <build instruction>")
            return 1
        out = RB.coordinated_remote_build(args.task, args.repo,
                                  allow_api_key=getattr(args, "allow_api_key", False))
        if not out.get("ok"):
            print(f"Pi build did not succeed (stage={out.get('stage')}): {out.get('detail') or ''}")
            if out.get("output_tail"):
                print(out["output_tail"][-1000:])
            return 1
        print(f"Pi build ran in {out['remote_repo']} ({out['files_synced']} files synced"
              + (", rate-limited" if out.get("rate_limited") else "") + ").")
        print(out.get("review_hint", ""))
        return 0
    elif args.command == "version":
        from . import __version__
        print(__version__)
    elif args.command == "serve":
        import uvicorn
        import os as _os
        # Foreign-brain guard (register item R2, born of two real incidents: a mis-paired
        # week on a stray Downloads brain, and the release gate hijacking the LIVE server
        # onto a pytest temp DB). The household brain lives in exactly one place:
        # ~/.mongoose/data/mongoose.sqlite3. If this serve would boot on ANY other brain
        # path, refuse loudly and name both paths, unless MONGOOSE_ALLOW_FOREIGN_BRAIN=1
        # is set (tests, CI, sandboxes, and deliberate spare-machine runs set it; nothing
        # on the real Mac should).
        try:
            from pathlib import Path as _P
            _canon = str(_P(_os.path.expanduser("~")) / ".mongoose" / "data" / "mongoose.sqlite3")
            _resolved = _os.environ.get("MONGOOSE_DB_PATH", "").strip()
            if not _resolved:
                from .settings import _default_db_path as _ddp
                _resolved = _ddp()
            if (str(_P(_resolved).expanduser()) != _canon
                    and _os.environ.get("MONGOOSE_ALLOW_FOREIGN_BRAIN", "") != "1"):
                print("REFUSING TO SERVE: this would boot on a FOREIGN brain.")
                print(f"  resolved brain: {_resolved}")
                print(f"  canonical:      {_canon}")
                print("The household brain lives only at the canonical path. If this is a test,")
                print("CI, or a deliberate spare-machine run, set MONGOOSE_ALLOW_FOREIGN_BRAIN=1.")
                return 2
        except SystemExit:
            raise
        except Exception:
            pass  # the guard must never be the thing that breaks a legitimate boot
        # Owner requirements that are safe to prepare automatically -- an auth token, a
        # private ntfy topic, and LAN reachability (MONGOOSE_HOST=0.0.0.0,
        # MONGOOSE_ALLOW_REMOTE=true) -- go here, before get_settings() so the first read
        # already reflects them. See owner_defaults.py's own docstring for the full
        # reasoning; it never touches public/WAN ingress (DDNS, port-forwarding, UPnP), only
        # LAN reachability, which is bounded to the owner's own trusted home network.
        try:
            from .owner_defaults import ensure_owner_baseline
            ensure_owner_baseline(source="serve")
        except Exception as _baseline_exc:
            print(f"Owner baseline preparation could not complete ({_baseline_exc}); continuing with saved settings.")
        # A configured Cloudflare tunnel is supervised by Mongoose itself (no system
        # service, no sudo), so it must come back whenever the server does.
        try:
            from .cloudflare_tunnel import tunnel_state, ensure_tunnel_running
            if tunnel_state().get("configured"):
                _t = ensure_tunnel_running()
                if not _t.get("ok"):
                    print(f"Cloudflare tunnel not started ({_t.get('error')}); guests won't reach the public address until it runs.")
        except Exception:
            pass
        from .settings import get_settings
        s = get_settings()
        host = args.host or s.host
        port = args.port or s.port
        cert = getattr(s, "tls_certfile", "")
        key = getattr(s, "tls_keyfile", "")
        # HTTPS IS ON BY DEFAULT. A secure context is required for phone mic/voice/push on the LAN
        # (loopback 127.0.0.1 is a secure context regardless), so a fresh install should serve https
        # without manual cert steps. Precedence: explicit --no-tls (or MONGOOSE_TLS=off) serves plain
        # http; otherwise TLS is on. A configured MONGOOSE_TLS_CERTFILE/KEYFILE (e.g. mkcert) is used
        # as-is; if none is set we auto-provision a self-signed cert.
        _tls_env = (_os.getenv("MONGOOSE_TLS", "") or "").strip().lower()
        tls_off = args.no_tls or _tls_env in ("off", "0", "false", "no", "none")
        want_tls = not tls_off
        tls_kwargs = {}
        self_signed = False
        if want_tls:
            have_configured = bool(cert and key and _os.path.exists(cert) and _os.path.exists(key))
            if not have_configured:
                # No usable cert configured: use the self-signed one, generating ONLY when the files
                # are genuinely absent or the cert is provably expired. A status-check wobble must
                # never rotate a cert that exists and works: every rotation invalidates the trust the
                # user's browser has stored, and that is exactly the failure that dressed itself up
                # as a stuck offline page.
                try:
                    from .self_signed_tls import generate_self_signed, self_signed_status, cert_paths
                    cp, kp = cert_paths()
                    files_exist = _os.path.exists(cp) and _os.path.exists(kp)
                    st = {}
                    try:
                        st = self_signed_status() or {}
                    except Exception:
                        st = {}
                    expired = bool(st.get("expired"))
                    if files_exist and not expired:
                        pass  # keep the existing certificate; stability beats freshness here
                    else:
                        if files_exist and expired:
                            print("TLS: the self-signed certificate has expired; generating a fresh one. "
                                  "Browsers will ask you to trust it once.")
                        gen = generate_self_signed()
                        if isinstance(gen, dict) and gen.get("ok") is False and gen.get("error"):
                            raise RuntimeError(gen["error"])
                    cert, key = str(cp), str(kp)
                    have_configured = _os.path.exists(cert) and _os.path.exists(key)
                    self_signed = have_configured
                except Exception as exc:
                    print(f"Could not set up HTTPS automatically ({exc}); starting on http. "
                          "Phone voice/push need https, run `mongoose serve --tls` once a cert exists, "
                          "or set MONGOOSE_TLS=off to silence this.")
                    want_tls = False
            if want_tls and have_configured:
                tls_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
            elif want_tls:
                want_tls = False
        scheme = "https" if want_tls else "http"
        if want_tls:
            # Make certificate identity VISIBLE on every start. A silently regenerated cert
            # invalidates the browser's stored trust exception, and with a stale service
            # worker in play that once became an unescapable false "offline" page. If the
            # fingerprint printed here changes between starts, that is the moment browsers
            # will re-prompt to trust the certificate.
            try:
                import hashlib as _hl
                with open(cert, "rb") as _f:
                    _fp = _hl.sha256(_f.read()).hexdigest()[:16]
                _kind = "self-signed" if self_signed else "configured"
                print(f"TLS: {_kind} certificate {cert} (sha256 {_fp}). If your browser shows a "
                      f"certificate warning after an update, the cert changed: accept it once and "
                      f"everything heals.")
            except Exception:
                pass
        try:
            from .access_urls import local_browser_base, lan_browser_base, user_facing_base
            cockpit_base = user_facing_base(port, scheme=scheme, host=host)
            mac_base = local_browser_base(port, scheme=scheme)
            lan_base = lan_browser_base(port, scheme=scheme)
        except Exception:
            cockpit_base = f"{scheme}://127.0.0.1:{port}"
            mac_base = cockpit_base
            lan_base = cockpit_base
        if host in ("0.0.0.0", "::"):
            print(f"Starting Mongoose API on {scheme}, listening on all interfaces, port {port}.")
        else:
            shown_host = "127.0.0.1" if host in ("127.0.0.1", "::1") else host
            print(f"Starting Mongoose API on {scheme}://{shown_host}:{port}")
        print(f"Cockpit (status, setup, activity, approvals, voice): {cockpit_base}/ui")
        if self_signed:
            print("HTTPS is on with a self-signed certificate (the default).")
            print(f"  • On this Mac, open the cockpit at {mac_base}/ui. "
                  "The installer/status checks accept the local self-signed cert automatically.")
            if host in ("0.0.0.0", "::"):
                print(f"  • On a phone/tablet on the same Wi‑Fi, use {lan_base}/login. "
                      "The first visit will show a one-time certificate warning unless you install a trusted cert.")
            print("  • Manage or regenerate the cert in the cockpit: Setup → Voice on phones (HTTPS). "
                  "To run plain http instead, start with --no-tls.")
        s_token = getattr(s, "api_token", "")
        s_remote = getattr(s, "allow_remote", False)
        if host in ("0.0.0.0", "::") or (s_remote and s_token):
            print(f"LAN access: phone/tablet browser URL is {lan_base}/login. Enter the access token once.")
            if want_tls:
                print("Voice works on LAN devices over https (mic needs a secure context). "
                      "For warning-free access, install a trusted local cert/root CA; see docs/HTTPS.md.")
            else:
                print("Note: voice/mic on LAN devices needs https. Enable it with --tls "
                      "(see docs/HTTPS.md), or use voice on the Mac itself.")
        # Advertise on the LAN for phone/tablet discovery (soft dep; non-fatal). Skip
        # under --reload, which forks worker processes and would double-register.
        _adv = None
        if not args.reload:
            try:
                from .discovery import advertise
                _adv = advertise(port, scheme)
            except Exception:
                _adv = None
        # Write a pidfile so `mongoose stop` (and the installer) can reliably stop THIS server,
        # rather than guessing with process-name matching. Best-effort; cleaned up on exit.
        _pidfile = None
        if not args.reload:
            try:
                from pathlib import Path as _P
                _pidfile = _P(s.data_dir) / "mongoose-server.pid" if hasattr(s, "data_dir") else _P("data/mongoose-server.pid")
                _pidfile.parent.mkdir(parents=True, exist_ok=True)
                _pidfile.write_text(f"{_os.getpid()}\n{port}\n")
            except Exception:
                _pidfile = None
        # A fresh serve means the server is intentionally up , lift any watchdog pause left by a prior
        # `mongoose stop` so the watchdog supervises this new server.
        try:
            from .services.server_watchdog import resume as _wd_resume
            _wd_resume()
        except Exception:
            pass
        # Mutual supervision: make sure a watchdog is actually running so a crash, or a failed
        # restart, is recovered automatically. The watchdog keeps the server alive (probes +
        # relaunch); this keeps the watchdog alive (spawns it if absent). No-op if one already is.
        # Skipped under --reload (which forks workers) and when explicitly opted out.
        if not args.reload and _os.getenv("MONGOOSE_NO_WATCHDOG", "").strip().lower() not in ("1", "true", "yes"):
            try:
                from .services.server_watchdog import spawn_detached as _wd_spawn
                _wd_spawn(reason="serve-start")
            except Exception:
                pass
        try:
            # Timestamped logging: by default uvicorn emits bare "INFO: ... GET /x" lines with no time,
            # which makes the log nearly useless for diagnosis ("the log stopped 8 minutes ago" can't even
            # be seen without timestamps). Give every line an ISO-ish timestamp.
            import copy as _copy
            from uvicorn.config import LOGGING_CONFIG as _ULC
            log_config = _copy.deepcopy(_ULC)
            _ts = "%(asctime)s"
            log_config["formatters"]["default"]["fmt"] = f"{_ts} %(levelprefix)s %(message)s"
            log_config["formatters"]["access"]["fmt"] = (
                f'{_ts} %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
            )
            for _f in ("default", "access"):
                log_config["formatters"][_f]["datefmt"] = "%Y-%m-%d %H:%M:%S"
            # Route Mongoose's OWN loggers (mongoose.request, mongoose.lan, mongoose.watchdog, ...)
            # through the same timestamped handler. Without this, uvicorn configures only its own
            # loggers and Mongoose's logging.getLogger("mongoose.*") output goes nowhere , so the new
            # request.start/done and exception traces would be invisible, exactly the gap that made the
            # 8-minute freeze undiagnosable.
            log_config.setdefault("loggers", {})["mongoose"] = {
                "handlers": ["default"], "level": "INFO", "propagate": False,
            }
            uvicorn.run("mongoose.app:app", host=host, port=port, reload=args.reload,
                        # Cap the shutdown drain: uvicorn's default waits FOREVER for
                        # in-flight connections, and a browser tab holding one (the
                        # /install page polling, a hung cockpit request) turns every
                        # update handover into a three-way deadlock: terminal waits on the
                        # server, the server waits on the browser, the browser waits on
                        # the server. Five seconds is drain enough for real requests.
                        timeout_graceful_shutdown=5,
                        log_config=log_config, **tls_kwargs)
        finally:
            if _pidfile is not None:
                try:
                    _pidfile.unlink()
                except Exception:
                    pass
            if _adv is not None:
                _adv.stop()
    elif args.command == "stop":
        import os as _os
        import signal as _signal
        import time as _time
        from pathlib import Path as _P
        from .settings import get_settings
        s = get_settings()
        # Tell the external watchdog to stand down BEFORE we kill the server, so it doesn't immediately
        # respawn the thing we're deliberately stopping. (resume happens on the next `serve`.)
        try:
            from .services.server_watchdog import pause as _wd_pause, is_paused as _wd_is_paused
            _wd_pause("mongoose stop")
            # pause() is best-effort (swallows write errors). If the marker did NOT take, the watchdog
            # would respawn the server we're about to stop (bounded by its circuit breaker, but
            # confusing). Verify and tell the user the concrete reason instead of leaving them puzzled.
            try:
                if not _wd_is_paused():
                    wdir = _P(getattr(get_settings(), "data_dir", "data")) / "watchdog"
                    print(f"Warning: could not pause the server watchdog (couldn't write {wdir/'paused'}). "
                          f"If the server restarts on its own, check that {wdir} is writable, or stop the "
                          f"watchdog with: mongoose server-watchdog --uninstall")
            except Exception:
                pass
        except Exception:
            pass
        stopped = False
        # 1) Preferred: the pidfile written by `serve`.
        pidfile = _P(getattr(s, "data_dir", "data")) / "mongoose-server.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().splitlines()[0].strip())
                if not _pid_looks_like_mongoose_server(pid):
                    print(f"Pidfile points to pid {pid}, but it does not look like a Mongoose "
                          f"server; refusing to kill it.")
                else:
                    _os.kill(pid, _signal.SIGTERM)
                    for _ in range(20):
                        _time.sleep(0.1)
                        try:
                            _os.kill(pid, 0)  # still alive?
                        except OSError:
                            stopped = True
                            break
                    if not stopped:
                        _os.kill(pid, _signal.SIGKILL)
                        stopped = True
                    print(f"Stopped Mongoose server (pid {pid}).")
            except (ProcessLookupError, ValueError):
                print("Pidfile was stale; no server with that pid.")
            except Exception as exc:
                print(f"Could not stop via pidfile: {exc}")
            try:
                pidfile.unlink()
            except Exception:
                pass
        # 2) Fallback: anything listening on the port (covers a server started without a pidfile).
        if not stopped:
            port = getattr(s, "port", 8765)
            killed_any = False
            refused = []
            try:
                import subprocess as _sp
                # lsof is present on macOS; find PIDs listening on the port
                out = _sp.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                              capture_output=True, text=True)
                pids = [p for p in out.stdout.split() if p.strip().isdigit()]
                refused = []
                for p in pids:
                    try:
                        pid = int(p)
                        if not _pid_looks_like_mongoose_server(pid):
                            refused.append(p)
                            continue
                        _os.kill(pid, _signal.SIGTERM)
                        killed_any = True
                    except Exception:
                        pass
                if killed_any:
                    print(f"Stopped server(s) listening on port {port}.")
                if refused:
                    print(f"Left {len(refused)} non-Mongoose process(es) on port {port} untouched "
                          f"(pids: {', '.join(refused)}).")
            except FileNotFoundError:
                pass  # lsof not available
            if not killed_any and not refused:
                print("No running Mongoose server found.")
        return 0
    elif args.command == "status":
        from pathlib import Path as _P
        from .settings import get_settings
        from .local_probe import health as _probe_health, preferred_local_base as _preferred_local_base, get_bytes as _probe_bytes
        from .access_urls import user_facing_base as _user_facing_base
        s = get_settings()
        port = getattr(s, "port", 8765)
        running = False
        ui_ok = False
        info = {}
        base = _preferred_local_base(port)
        try:
            url, info = _probe_health(port, timeout=4)
            base = url.rsplit("/health", 1)[0]
            running = info.get("name") == "mongoose"
            try:
                _ui_url, body = _probe_bytes("/ui", port=port, timeout=4)
                ui_ok = b"Mongoose" in body or b"Cockpit" in body
            except Exception:
                ui_ok = False
        except Exception:
            running = False
        # pidfile, if present
        pid = None
        pidfile = _P(getattr(s, "data_dir", "data")) / "mongoose-server.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().splitlines()[0].strip())
            except Exception:
                pid = None
        if running:
            scheme = "https" if base.startswith("https://") else "http"
            display_base = _user_facing_base(port, scheme=scheme, host=getattr(s, "host", "127.0.0.1"))
            print("Mongoose is RUNNING")
            print(f"  version: {info.get('version', '?')}")
            if pid:
                print(f"  pid:     {pid}")
            print(f"  cockpit: {display_base}/ui")
            if not ui_ok:
                print("  warning: /health is answering, but /ui did not render during the status check.")
            return 0
        else:
            print(f"Mongoose is NOT running (nothing answering on port {port}).")
            print(f"  Start it:  ./'Start Mongoose.command'   (or  mongoose serve --port {port} )")
            return 1
    elif args.command == "backup":
        bc = getattr(args, "backup_cmd", None)
        if bc == "create":
            from . import backup as B
            r = B.create_backup(args.label)
            if r["ok"]:
                print(f"Backup created: {r['archive']}")
                print(f"  {r['bytes']} bytes, contains: {', '.join(r['manifest']['contents'].keys())}")
            else:
                print("Backup failed:", r.get("error"))
            return 0 if r["ok"] else 1
        elif bc == "list":
            from . import backup as B
            rows = B.list_backups()
            if not rows:
                print("No local backups yet. Create one with: mongoose backup create")
            for b in rows:
                print(f"  [{b['tier']}] {b['name']}  ({b['bytes']} bytes)")
            return 0
        elif bc == "restore":
            from . import backup as B
            r = B.restore_backup(args.archive, restore_config=not args.no_config)
            if r["ok"]:
                print("Restored:", ", ".join(r["restored"].keys()))
                print("  " + r.get("note", ""))
            else:
                print("Restore failed:", r.get("error"))
            return 0 if r["ok"] else 1
        elif bc == "connect-drive":
            from . import backup_drive as BD
            r = BD.connect_drive()
            print(r.get("detail") if r.get("ok") else ("Could not connect: " + r.get("error", "")))
            return 0 if r.get("ok") else 1
        elif bc == "to-drive":
            from . import backup_drive as BD
            r = BD.backup_to_drive()
            lb = r.get("local_backup", {})
            print(f"Local backup: {'ok' if lb.get('ok') else 'FAILED'} ({lb.get('bytes','?')} bytes)")
            if r.get("drive_uploaded"):
                print(f"Uploaded to Drive: {r['drive_upload'].get('name')}")
                dr = r.get("drive_rota", {})
                print(f"  Drive rota: kept {dr.get('kept','?')}, trashed {dr.get('trashed','?')}")
            else:
                drv = r.get("drive", {})
                print("Drive: " + (drv.get("skipped") or "not uploaded"))
            return 0
        elif bc == "drive-list":
            from . import backup_drive as BD
            if not BD.is_connected():
                print("Drive not connected. Run: mongoose backup connect-drive")
                return 1
            for f in BD.list_drive_backups():
                print(f"  {f['name']}  ({f['bytes']} bytes)")
            return 0
        elif bc == "status":
            from . import backup_drive as BD
            st = BD.backup_status()
            print("Backup lane:")
            print(f"  last successful backup: {st['last_ok_at'] or 'never'}")
            if st.get("last_error"):
                print(f"  last error: {st['last_error']}  (at {st.get('last_error_at')})")
            print(f"  local backups: {st['local_count']}")
            print(f"  Drive connected: {'yes' if st['drive_connected'] else 'no'}")
            return 0
        elif bc == "drive-health":
            from . import backup_drive as BD
            h = BD.connection_health()
            if h["ok"]:
                print("Drive is connected and working.")
            else:
                print("Drive not usable: " + h.get("reason", "unknown"))
            return 0 if h["ok"] else 1
        elif bc == "prune":
            from . import backup as B
            r = B.apply_rota()
            print(f"Local rota applied: kept {r['kept']}, deleted {r['deleted']}.")
            return 0
        else:
            print("Use: mongoose backup create|list|restore|connect-drive|to-drive|drive-list|status|drive-health|prune")
            return 0
    elif args.command == "brain":
        from . import brain_preservation as BP
        bc = getattr(args, "brain_cmd", None)
        if bc == "rollover":
            from .services.memory import rollover_main_conversation
            r = rollover_main_conversation(getattr(args, "reason", "") or "")
            if r.get("archive"):
                print(f"Archived {r['moved']} messages to conversation '{r['archive']}'.")
                print("The main conversation starts fresh with the next message. Nothing was deleted.")
            else:
                print(r.get("detail") or "nothing to do")
            return 0
        if bc == "verify":
            v = BP.verify_brain()
            print("Brain integrity:", "OK" if v["ok"] else "PROBLEMS")
            print(f"  database: {v['db']}")
            if v.get("integrity"):
                print(f"  integrity_check: {v['integrity']}")
            for t, n in v.get("tables", {}).items():
                print(f"  {t}: {n}")
            for p in v.get("problems", []):
                print(f"  ! {p}")
            return 0 if v["ok"] else 1
        elif bc == "wakeup":
            from .brain_wakeup import wake_up
            rep = wake_up(repair=not getattr(args, "check_only", False))
            print("Brain wake-up:", "OK" if rep["ok"] else "PROBLEMS")
            if rep.get("integrity"):
                print(f"  integrity_check: {rep['integrity']}")
            if rep.get("missing_tables"):
                print(f"  missing tables: {rep['missing_tables']}")
            if rep.get("fts", {}).get("desynced"):
                print(f"  memory-search desync: {rep['fts']['desynced']}")
            for a in rep.get("actions", []):
                print(f"  repaired: {a}")
            for p in rep.get("problems", []):
                print(f"  ! {p}")
            if rep["ok"] and not rep.get("actions"):
                print("  the brain woke up with full working memory; nothing needed repair.")
            return 0 if rep["ok"] else 1
        elif bc == "snapshot":
            r = BP.snapshot_brain(args.label)
            if r["ok"]:
                print(f"Brain snapshotted: {r['snapshot_dir']}")
                print(f"  database: {r.get('db_bytes', 0)} bytes" +
                      (" + session_salt" if r.get("session_salt_snapshot") else ""))
            else:
                print("Snapshot failed:", r.get("error"))
            return 0 if r["ok"] else 1
        elif bc == "list":
            rows = BP.list_snapshots()
            if not rows:
                print("No brain snapshots yet.")
            for s in rows:
                print(f"  {s['name']}  ({s['db_bytes']} bytes" + (", +salt" if s["has_salt"] else "") + ")")
                print(f"     {s['snapshot_dir']}")
            return 0
        elif bc == "restore":
            r = BP.restore_brain(args.snapshot_dir)
            if r["ok"]:
                print(f"Brain restored from {r['from']}. Conversation and memory are back.")
                print("  (Restart Mongoose if it's running, so it reads the restored brain.)")
            else:
                print("Restore failed:", r.get("error"))
            return 0 if r["ok"] else 1
        elif bc == "prune":
            r = BP.prune_snapshots(keep=args.keep)
            print(f"Kept {r['kept']} brain snapshot(s), deleted {r['deleted']}.")
            return 0
        else:
            print("Use: mongoose brain verify|snapshot|list|restore|prune")
            return 0
    elif args.command == "harden":
        from .services import self_harden as H
        hc = getattr(args, "harden_cmd", None)
        if hc == "status":
            st = H.status()
            print("Self-harden status:")
            print(f"  open observations: {st['open']} (P0: {st['open_p0']})")
            if st.get("last_run"):
                print(f"  last run: {st['last_run'].get('kind')} {st['last_run'].get('status')} at {st['last_run'].get('created_at')}")
            for o in st.get("top", [])[:10]:
                print(f"  [{o['severity']}] {o.get('claim_key') or 'unmapped'}: {o['summary']} ({o['scope']})")
            return 0
        elif hc == "observe":
            r = H.record_observation(args.source, args.kind, args.scope, args.summary,
                                     severity=args.severity, claim_key=args.claim)
            print(f"Recorded observation {r['id']} [{r['severity']}] {r['summary']}")
            return 0
        elif hc == "scan":
            r = H.scan_runtime()
            print(json.dumps(r, indent=2) if args.json else f"Recorded/refreshed {r['observations']} observation(s).")
            return 0
        elif hc == "consistency":
            from .services import claim_consistency as CC
            r = CC.run_builtin()
            if args.json:
                print(json.dumps(r, indent=2))
                return 0 if r["ok"] else 1
            print(f"Claim-consistency: {r['total']} claim(s) checked, {r['diverging_count']} diverging.")
            for rep in r["reports"]:
                if not rep["comparable"]:
                    print(f"  ~ {rep['key']}: not comparable here ({rep['evaluated']} evaluable probe(s)).")
                elif rep["agree"]:
                    print(f"  OK {rep['key']}: {rep['evaluated']} authorities agree.")
                else:
                    print(f"  !! {rep['key']}: DIVERGENCE, {rep['description']}")
                    for val, auths in rep.get("divergence", {}).items():
                        print(f"       {', '.join(auths)} -> {val}")
            if not r["ok"]:
                print("\nFix: make the divergent caller REUSE the trusted source, do not add a third definition.")
            return 0 if r["ok"] else 1
        elif hc == "recent":
            for o in H.recent(args.limit, status=args.status):
                print(f"  {o['id']} [{o['severity']}/{o['status']}] {o.get('claim_key') or 'unmapped'}: {o['summary']} ({o['scope']})")
            return 0
        elif hc == "claims":
            for c in H.claims():
                print(f"  [{c['severity']}] {c['key']}: {c['promise']}")
                print(f"       invariant: {c['invariant']}")
            return 0
        elif hc == "module-index":
            if args.rebuild:
                r = H.rebuild_module_index(".")
                print(f"Indexed {r['modules']} module(s); saw {r['tests_seen']} test file(s).")
            rows = H.module_index(args.limit, query=args.query)
            for m in rows:
                bits = []
                if m.get("commands"):
                    bits.append("commands=" + ",".join(m["commands"][:4]))
                if m.get("routes"):
                    bits.append("routes=" + ",".join(m["routes"][:4]))
                if m.get("tests"):
                    bits.append("tests=" + ",".join(m["tests"][:4]))
                print(f"  {m['name']}: {'; '.join(bits) if bits else m.get('purpose','')}")
            return 0
        elif hc == "reingest":
            r = H.reingest_codebase(".", out=(args.path or None))
            print(f"Reingested code map ({r['modules']} modules):")
            print(f"  markdown: {r['md_path']}")
            print(f"  json:     {r['json_path']}")
            return 0
        elif hc == "export":
            r = H.export_packet(args.path or None, include_module_index=not args.no_modules)
            print(f"Exported hardening packet ({r['observations']} observations):")
            print(f"  markdown: {r['md_path']}")
            print(f"  json:     {r['json_path']}")
            return 0
        elif hc == "review-packet":
            r = H.create_reviewer_packet(args.path or None, observation_id=(args.observation_id or None),
                                         reviewer=args.reviewer)
            print(f"Created {args.reviewer} review packet {r['id']}:")
            print(f"  markdown: {r['md_path']}")
            print(f"  json:     {r['json_path']}")
            return 0
        elif hc == "plan":
            p = H.create_hardening_plan(args.observation_id or None, title=args.title)
            print(f"Created hardening build plan {p['plan_id']} ({len(p['steps'])} steps).")
            print("Approve it with: mongoose build-approve " + p['plan_id'])
            return 0
        else:
            print("Use: mongoose harden status|observe|scan|recent|claims|module-index|reingest|export|review-packet|plan")
            return 0
    elif args.command == "crawler":
        from .services import crawler as cr
        if args.status or not (args.freshen or args.backfill):
            rows = cr.crawler_status()
            if not rows:
                print("No crawl sources yet. Run a freshen/backfill, or start the scheduler.")
            for s in rows:
                print(s["summary"])
        elif args.freshen:
            if args.freshen.lower() == "imap":
                print(json.dumps(cr.crawl_imap_fresh("INBOX"), indent=2))
            else:
                print(json.dumps(cr.crawl_files(args.freshen, device=args.device, mode="fresh"), indent=2))
        elif args.backfill:
            print(json.dumps(cr.crawl_files(args.backfill, device=args.device, mode="backfill"), indent=2))
    elif args.command == "service":
        from . import service
        if args.action == "generate":
            p = service.write_plist()
            print(f"Wrote launchd plist: {p}")
            print("It runs `mongoose scheduler` continuously (KeepAlive restarts it if it dies).")
            print(f"Logs: {service._log_dir()}/scheduler.out.log and scheduler.err.log")
            print("Activate it with: mongoose service install   (or: launchctl load "
                  f"{p})")
            return 0
        elif args.action == "install":
            # generate + load, but confirm first (the owner's choice: offer, don't impose).
            p = service.plist_path()
            print("About to install the Mongoose scheduler as a launchd agent:")
            print(f"  plist:   {p}")
            print("  runs:    mongoose scheduler  (KeepAlive: restarts if it dies)")
            print(f"  logs:    {service._log_dir()}/scheduler.{{out,err}}.log")
            print("  effect:  the scheduler starts now and on every login, unattended.")
            if not args.yes:
                try:
                    resp = input("Proceed? [y/N] ").strip().lower()
                except EOFError:
                    resp = ""
                if resp not in ("y", "yes"):
                    print("Cancelled. (Nothing loaded. The plist was not written.)")
                    return 1
            res = service.install()
            if res["ok"]:
                print(f"Installed and loaded: {res['plist']}")
                print("Check it with: mongoose service status")
            else:
                print(f"Install reported a problem: {res['detail']}")
                print("On macOS, ensure you're in a logged-in GUI session (launchctl "
                      "load needs your user domain).")
            return 0 if res["ok"] else 1
        elif args.action == "status":
            st = service.status()
            print(f"Service: {st['label']}")
            print(f"  plist installed: {'yes' if st['installed'] else 'no'} ({st['plist']})")
            print(f"  loaded (running): {'yes' if st['loaded'] else 'no'}")
            print(f"  out log: {st['out_log']}")
            print(f"  err log: {st['err_log']}")
            if st["installed"] and not st["loaded"]:
                print("  (installed but not loaded: mongoose service install, or launchctl load the plist)")
            if not st["installed"]:
                print("  (not installed: mongoose service install to run the scheduler unattended)")
            return 0
        elif args.action == "uninstall":
            res = service.uninstall()
            print(f"Unloaded: {'yes' if res['unloaded'] else 'no'}; "
                  f"plist removed: {'yes' if res['removed'] else 'no'}")
            return 0
    elif args.command == "scheduler":
        from .services.scheduler import run_scheduler, run_due_jobs, job_status, scheduler_control
        if getattr(args, "free_memory", False):
            print(json.dumps(scheduler_control("free_memory", reason=args.reason or "CLI memory relief"), indent=2))
        elif getattr(args, "disable", None) is not None:
            print(json.dumps(scheduler_control("disable", jobs=args.disable, reason=args.reason or "CLI pause"), indent=2))
        elif getattr(args, "enable", None) is not None:
            print(json.dumps(scheduler_control("enable", jobs=args.enable, reason=args.reason or "CLI resume"), indent=2))
        elif args.status:
            for j in job_status():
                last = j.get("last_run") or "never"
                flag = "enabled" if j.get("enabled", True) else f"DISABLED: {j.get('disabled_reason') or ''}"
                print(f"{j['job']:22} {j['schedule']:20} {flag:34} last: {last} ({j.get('last_status') or '-'})")
        elif args.once:
            ran = asyncio.run(run_due_jobs())
            print(json.dumps(ran, indent=2) if ran else "No jobs were due.")
        else:
            print("Mongoose scheduler running (Ctrl-C to stop). Jobs: daily brief, email sync, file reindex.")
            try:
                asyncio.run(run_scheduler(tick_seconds=args.tick))
            except KeyboardInterrupt:
                print("\nScheduler stopped.")
    elif args.command == "server-watchdog":
        from .services import server_watchdog as _wd
        if getattr(args, "install", False):
            try:
                path = _wd_install_agent()
                print(f"Installed the server watchdog launch agent: {path}")
                print("Load it with:")
                uid = os.getuid()
                print(f"  launchctl bootstrap gui/{uid} {path}")
                print("It will probe /health/live and restart the server if the event loop wedges.")
            except Exception as exc:
                print(f"Couldn't install the watchdog agent: {exc}")
                return 1
        elif getattr(args, "uninstall", False):
            # Stop and remove the watchdog so it no longer respawns the server. Boot it out of launchd
            # first (best-effort, it may not be loaded), then remove the plist. Also lift any pause
            # marker so a future install starts clean.
            from pathlib import Path as _P
            import subprocess as _sp
            label = "com.mongoose.server-watchdog"
            plist = _P.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            uid = os.getuid()
            try:
                _sp.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                        capture_output=True, text=True, timeout=10)
            except Exception:
                pass
            removed = False
            try:
                if plist.exists():
                    plist.unlink(); removed = True
            except Exception as exc:
                print(f"Could not remove the watchdog plist {plist}: {exc}")
                return 1
            try:
                _wd.resume()   # clear any leftover pause marker
            except Exception:
                pass
            if removed:
                print(f"Removed the server watchdog agent ({plist}). It will no longer respawn the server.")
            else:
                print("The server watchdog agent was not installed (nothing to remove). "
                      "If a watchdog process is still running, it was started manually; stop that process.")
        elif args.once:
            state = {"consecutive_failures": 0, "last_restart": 0.0}
            out = _wd.watch_once(state, fail_threshold=args.fail_threshold,
                                 cooldown_seconds=args.cooldown)
            print(json.dumps(out, indent=2, default=str))
        else:
            print("Mongoose server watchdog running (Ctrl-C to stop). Probing /health/live; will "
                  "restart the server if its event loop wedges.")
            try:
                _wd.run_forever(interval_seconds=args.interval, fail_threshold=args.fail_threshold,
                                cooldown_seconds=args.cooldown)
            except KeyboardInterrupt:
                print("\nWatchdog stopped.")
    elif args.command == "chat":
        from .role_router import classify_role
        from .persona import persona_card
        from .tools import run_tool_turn
        from .llm import LocalModelError, chat_with_memory, ask_ollama
        from .services.memory import DEFAULT_CLI_CONVERSATION
        role = args.role if args.role in {"chat", "tool", "code"} else classify_role(args.prompt)
        try:
            if role == "tool":
                out = asyncio.run(run_tool_turn(args.prompt))
                print(out.get("reply", ""))
                if out.get("tool"):
                    print(f"\n[tool: {out['tool']}  executed: {out.get('executed')}]")
            elif args.no_memory:
                system = persona_card(args.mode) if role == "chat" else None
                print(asyncio.run(ask_ollama(args.prompt, system=system, role=role)))
            else:
                conv = args.conversation or DEFAULT_CLI_CONVERSATION
                out = asyncio.run(chat_with_memory(args.prompt, conversation_id=conv, role=role, mode=args.mode))
                print(out["reply"])
                for ev in out.get("memory_events", []):
                    if ev.get("type") == "remembered" and ev.get("fact"):
                        print(f"[remembered: {ev['fact']}]")
                    elif ev.get("type") == "forgotten" and ev.get("fact"):
                        print(f"[forgotten: {ev['fact']}]")
                # back-compat: if no structured events but legacy remembered list present
                if not out.get("memory_events") and out.get("remembered"):
                    print(f"\n[remembered: {'; '.join(out['remembered'])}]")
        except LocalModelError as exc:
            print(exc.user_message)
            return 1
    elif args.command == "voice":
        if args.session:
            from .voice import run_session
            return run_session(mode=args.mode or "default", conversation_id=args.conversation)
        from .voice import run_voice
        return run_voice(mic=args.mic, mode=args.mode or "default",
                         conversation_id=args.conversation, once=args.once)
    elif args.command == "setup-voice":
        from .voice import setup_voice
        return setup_voice(yes=args.yes)
    elif args.command == "setup-ntfy":
        from .setup_ntfy import setup_ntfy
        return setup_ntfy(yes=args.yes)
    elif args.command == "start-docker":
        return _cmd_start_docker()
    elif args.command == "enable-phone":
        from .enable_phone import enable_phone
        return enable_phone(yes=args.yes)
    elif args.command == "tls":
        from . import acme_tls as _tls
        tc = getattr(args, "tls_cmd", None)
        if tc == "preflight":
            pf = _tls.preflight()
            print(json.dumps(pf, indent=2))
            return 0 if pf["ok"] else 1
        elif tc == "obtain":
            print("Obtaining a Let's Encrypt certificate (DNS-01 via DuckDNS). This can take a minute…")
            r = _tls.obtain_certificate(staging=getattr(args, "staging", False))
            if r.get("ok"):
                print(f"✓ {r['detail']}")
                print(f"  cert: {r['certfile']}")
                return 0
            print(f"✗ {r.get('error')}")
            return 1
        elif tc == "install-certbot":
            import subprocess as _sp
            import sys as _sys
            print("Installing certbot via pip…")
            rc = _sp.run([_sys.executable, "-m", "pip", "install", "--quiet", "certbot"]).returncode
            print("✓ certbot installed." if rc == 0 else "✗ certbot install failed.")
            return rc
        else:  # status
            st = _tls.certificate_status()
            pf = _tls.preflight()
            if st.get("present"):
                print(f"Certificate present for {st.get('host')}")
                if st.get("expires"):
                    print(f"  expires: {st['expires']}")
                print(f"  cert: {st['certfile']}")
            else:
                print(f"No certificate yet for {st.get('host') or '(DuckDNS host not configured)'}.")
            if not pf["ok"]:
                print("Prerequisites still needed:")
                for p in pf["problems"]:
                    print(f"  - {p}")
            else:
                print("Ready to obtain: run `mongoose tls obtain`.")
            return 0
    elif args.command == "tunnel":
        from .cloudflare_tunnel import tunnel_state, tunnel_end_to_end_proof, setup_plan, set_tunnel_hostname
        tcmd = getattr(args, "tunnel_command", None)
        if tcmd == "set-hostname":
            r = set_tunnel_hostname(args.hostname)
            if not r.get("ok"):
                print(r.get("error", "could not set hostname"))
                return 1
            print(f"Tunnel hostname saved: {r['hostname']}")
            print(f"Public URL is now: {r['public_url']}")
            print("Pairing, guest invites, and the phone app will all use this address.")
            print("Restart Mongoose so the running server picks it up, then run: mongoose tunnel diagnose")
            return 0
        if tcmd == "diagnose":
            st = tunnel_state()
            if not st["configured"]:
                print("No tunnel hostname configured yet. Run: mongoose tunnel setup")
                return 1
            print(f"Proving https://{st['hostname']} end-to-end (out to Cloudflare's edge and back)...")
            proof = tunnel_end_to_end_proof(st["hostname"])
            if proof.get("ok"):
                print(f"PROVEN: {proof['hostname']} answers with this Mongoose (version {proof.get('version')}).")
                print("This is the exact path a guest's phone uses, so guests will reach it too.")
                return 0
            print("NOT REACHABLE end-to-end yet.")
            if not st["installed"]:
                print("  cloudflared is not installed. Run: brew install cloudflared")
            elif not st["process_running"]:
                print("  cloudflared is not running. Run: sudo cloudflared service install && sudo cloudflared service start")
            elif proof.get("status") == 200:
                print(f"  {proof.get('error')}")
            else:
                print(f"  {proof.get('error', 'no answer')} -- check ~/.cloudflared/config.yml points at this Mongoose")
            return 1
        # status and setup share the same live step view; setup adds the exact next command.
        plan = setup_plan()
        st = tunnel_state()
        print("Cloudflare Tunnel setup:")
        next_step = None
        for p in plan:
            mark = "done" if p["done"] else "    "
            print(f"  [{mark}] {p['title']}")
            if next_step is None and not p["done"]:
                next_step = p
        if next_step is None:
            print("\nAll steps done. Prove it: mongoose tunnel diagnose")
            return 0
        if tcmd == "setup":
            print(f"\nNext step: {next_step['title']}")
            print(f"\n{next_step['command']}\n")
            print("Re-run `mongoose tunnel setup` after each step; it always shows the next one.")
        else:
            print(f"\nNext: {next_step['title']} (run `mongoose tunnel setup` for the exact command)")
        return 0
    elif args.command == "ddns":
        sub_cmd = getattr(args, "ddns_command", None)
        if sub_cmd == "setup":
            from .setup_ddns import setup_ddns
            return setup_ddns(provider=getattr(args, "provider", None), yes=getattr(args, "yes", False))
        elif sub_cmd == "update":
            from .ddns import update_now
            res = update_now(force=getattr(args, "force", False))
            print(res.get("detail", "done"))
            return 0 if res.get("ok") else 1
        elif sub_cmd == "status":
            from .ddns import status as ddns_status
            st = ddns_status()
            if not st["provider"]:
                print("No DDNS provider configured. Run: mongoose ddns setup")
                return 1
            print(f"Provider: {st['provider_name']} ({st['kind']})")
            print(f"Configured: {st['configured']}  Enabled: {st['enabled']}")
            if st["last_ip"]:
                print(f"Last updated IP: {st['last_ip']}")
            if st["needs_portforward"]:
                print("Reminder: this needs a router port-forward + enable-phone to actually reach Mongoose.")
            return 0
        elif sub_cmd == "diagnose":
            from .ddns import public_listener_acceptance, reachability_diagnosis, public_url
            from .setup_wizard import effective_env
            env = effective_env()
            url = public_url(env)
            if not url:
                print("No DDNS provider configured. Run: mongoose ddns setup")
                return 1
            print(f"Public address: {url}\n")
            listener = public_listener_acceptance()
            if listener.get("loopback_accepting") and not listener.get("lan_accepting"):
                print("LAN listener: FAILING")
                print("  Mongoose answers on 127.0.0.1 (the Mac itself) but refuses on its own")
                print("  LAN-facing address. A router's port-forward reaches the LAN address,")
                print("  not loopback, so this alone causes ERR_CONNECTION_REFUSED even with a")
                print("  correctly configured router. This is Mongoose-side, not your router:")
                print("  restart Mongoose (`mongoose restart` or Cockpit -> Repair phone access)")
                print("  and run this again.")
                return 1
            elif not listener.get("listener_accepting"):
                print("LAN listener: not accepting at all right now (is Mongoose actually running?)")
                return 1
            print("LAN listener: OK (accepting on both loopback and the LAN-facing address)\n")
            diag = reachability_diagnosis(env)
            reason = diag.get("reason", "unknown")
            if reason == "reachable":
                print("DDNS route: reachable. This Mac can reach its own public address.")
                print("(This is a self-test; it cannot fully prove a genuinely external client")
                print(" can reach you. If it's still refusing from an actual outside network,")
                print(" the router/ISP forwarding path is the remaining suspect.)")
                return 0
            elif reason == "dns_stale":
                print("DDNS route: STALE")
                print(f"  {diag.get('hostname')} resolves to {diag.get('dns_ip')}, but this")
                print(f"  network's actual current public IP is {diag.get('actual_public_ip')}.")
                print("  The DDNS updater has fallen behind. Fix: mongoose ddns update --force")
                return 1
            elif reason == "hairpin_or_no_portforward":
                print("DDNS route: NOT REACHABLE (router-side)")
                print("  DNS is correct and Mongoose's own listener is fine. The remaining cause")
                print("  is on your router specifically: either it can't loop back to its own")
                print("  public address for a device on the SAME home network (this often works")
                print("  fine from actual cellular data instead), or the port isn't forwarded to")
                print("  this Mac at all. Check your router's port-forward for the port in the")
                print("  address above; if MONGOOSE_PUBLIC_PORT differs from the port Mongoose")
                print("  actually listens on (MONGOOSE_PORT, usually 8765), the forward needs to")
                print("  map that external port to Mongoose's real internal port.")
                return 1
            else:
                print(f"DDNS route: could not confirm ({reason})")
                return 1
        else:
            print("Usage: mongoose ddns {setup|update|status}")
            return 1
    elif args.command == "remember":
        from .services.memory import remember_fact
        r = remember_fact(args.fact, source="explicit")
        print("Remembered." if r.get("stored") else f"Not stored: {r.get('reason')}")
    elif args.command == "remember-from":
        from .services.memory import propose_facts_via_model, remember_fact
        from .llm import LocalModelError
        try:
            proposed = asyncio.run(propose_facts_via_model(args.text))
        except LocalModelError as exc:
            print(exc.user_message)
            return 1
        if not proposed:
            print("No durable facts found.")
        for fact in proposed:
            if args.yes:
                keep = True
            else:
                keep = input(f'Remember: "{fact}"? [y/N] ').strip().lower() in ("y", "yes")
            if keep:
                remember_fact(fact, source="model")
                print(f"  stored: {fact}")
    elif args.command == "memory":
        from .services.memory import all_facts
        facts = all_facts()
        if not facts:
            print("No stored facts yet.")
        for f in facts:
            print(f"- {f['fact']}  ({f['source']})")
    elif args.command == "forget":
        from .services.memory import forget_fact
        r = forget_fact(args.fact)
        print("Forgotten." if r.get("forgotten") else f"Nothing to forget: {r.get('reason')}")
    elif args.command == "new-conversation":
        from .services.memory import clear_conversation, DEFAULT_CLI_CONVERSATION
        r = clear_conversation(DEFAULT_CLI_CONVERSATION)
        print(f"Cleared {r['deleted_messages']} messages from the default conversation.")
    elif args.command == "weather":
        print(json.dumps(asyncio.run(weather_summary(force=args.force)), indent=2))
    elif args.command == "sync-imap":
        print(json.dumps(sync_imap(args.mailbox, args.limit), indent=2))
    elif args.command == "index-files":
        print(json.dumps(index_files(args.root, args.limit, device=args.device), indent=2))
    elif args.command == "search-files":
        from .services.file_index import search_files
        results = search_files(args.query, args.limit)
        for r in results:
            print(f"[{r.get('device', 'local')}] {r.get('title')}  ({r.get('path')})")
        if not results:
            print("(no matching files)")
    elif args.command == "search-attachments":
        from .services.attachments import search_attachments
        results = search_attachments(args.query, args.limit)
        for r in results:
            size = f"{r.get('size_bytes')}b" if r.get("size_bytes") else "?"
            priv = r.get("privacy", "normal")
            label = "[private]" if priv in ("high", "secret") else ""
            print(f"{r.get('filename')} {label} | from {r.get('sender')} | "
                  f"'{r.get('subject')}' | {r.get('email_date')} | {size}")
        if not results:
            print("(no matching attachments)")
    elif args.command == "ha-prompts":
        from .services.ha_prompts import list_prompts
        for p in list_prompts():
            print(f"{p['key']:22} {p['title']:22} {p['description']}")
    elif args.command == "ha-prompt":
        from .services.ha_prompts import run_prompt
        result = asyncio.run(run_prompt(args.key, extra=args.extra))
        if result.get("ok"):
            print(result["reply"])
        else:
            print(json.dumps(result, indent=2))
    elif args.command == "push":
        from .services.notifications import send_to_phone
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] or None
        result = asyncio.run(send_to_phone(
            args.title, args.body, priority=args.priority, tags=tags,
            click=args.click or None))
        print(json.dumps(result, indent=2))
    elif args.command == "find":
        from .services.find import find, find_export
        kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip()) or None
        if getattr(args, "export", None):
            exp = find_export(args.query, limit=args.limit, kinds=kinds, fmt=args.export)
            if getattr(args, "out", None):
                from pathlib import Path as _P
                _P(args.out).write_text(exp["content"])
                print(f"Exported {exp['count']} result(s) to {args.out}")
            else:
                print(exp["content"])
            return 0
        res = find(args.query, limit=args.limit, kinds=kinds)
        if not res["results"]:
            print("(nothing found)")
        for h in res["results"]:
            print(f"[{h['kind']:12}] {h['title']}")
            print(f"               {h['where']}")
            if h.get("snippet"):
                print(f"               {h['snippet'][:120]}")
    elif args.command == "sync-google-calendar":
        from .connectors.google_calendar import sync_google_calendar
        print(json.dumps(sync_google_calendar(args.days_ahead, args.calendar_id), indent=2))
    elif args.command == "sync-gmail":
        from .connectors.gmail import sync_gmail
        print(json.dumps(sync_gmail(args.query, args.limit), indent=2))
    elif args.command == "sync-ms-calendar":
        from .connectors.microsoft_graph import sync_microsoft_calendar
        print(json.dumps(sync_microsoft_calendar(args.days_ahead), indent=2))
    elif args.command == "sync-ms-todo":
        from .connectors.microsoft_graph import sync_microsoft_todo, _todo_summary
        res = sync_microsoft_todo()
        print(_todo_summary(res, first_run=False))
        if res.get("warnings"):
            print(f"  ({len(res['warnings'])} note(s): {res['warnings'][0]})")
        print(json.dumps(res, indent=2))
    elif args.command == "sync-ms-mail":
        from .connectors.microsoft_graph import sync_microsoft_mail
        print(json.dumps(sync_microsoft_mail(args.folder, args.limit), indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
