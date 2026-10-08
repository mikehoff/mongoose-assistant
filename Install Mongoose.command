#!/usr/bin/env bash
# ── Mongoose ──────────────────────────────────────────────────────────────────
# Double-click this file to install and start Mongoose.
#
# It opens a clean progress screen in your browser and, when it's done, shows a QR
# code to pair your phone. You shouldn't need to touch the Terminal, but it stays
# open so you can see what's happening (and answer the one password prompt macOS
# asks for, to give the local model enough memory).
#
# Re-running this is safe: it just starts Mongoose again if it's already set up.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"

# ── Default install location: ~/.mongoose/app, NOT wherever the zip was unzipped ────────────────
# Downloads (the usual unzip spot) is TCC-protected on macOS, so a LaunchAgent cannot auto-start the
# server from there at login , reboot persistence is impossible in Downloads. Relocate the code to
# the canonical home once, build/refresh the venv there, and continue the install from that copy.
CANON="$HOME/.mongoose/app"
if [ "$REPO" != "$CANON" ]; then
  echo "Setting Mongoose up in its permanent home: $CANON"
  echo "(out of $REPO , Downloads cannot auto-start at login)"
  mkdir -p "$CANON"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.venv/' --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
      --exclude 'data/' "$REPO"/ "$CANON"/
  else
    ditto "$REPO" "$CANON"; rm -rf "$CANON/.venv" "$CANON/.git"
  fi
  chmod +x "$CANON"/*.command 2>/dev/null || true
  # Continue the install from the canonical copy (this same script, which will pass the check above).
  exec "$CANON/Install Mongoose.command"
fi

# macOS quarantines files that came in a downloaded zip (the com.apple.quarantine xattr), so each
# .command shows the "unidentified developer / can't be opened" Gatekeeper prompt , you'd otherwise
# clear every one by hand in Privacy & Security. We can't fully fix this without an Apple Developer ID
# to codesign + notarize (that's a paid cert and can't be done in the build), but we CAN remove the
# quarantine flag from the sibling launchers + the app here, so after this first install the
# Start/Stop/Status/RTM buttons just work. (This very file still gets ONE prompt the first time, since
# it has to run before it can de-quarantine anything , unavoidable for an unsigned downloaded file.)
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine . >/dev/null 2>&1 || true
  for f in *.command Mongoose.app; do
    [ -e "$f" ] && xattr -dr com.apple.quarantine "$f" >/dev/null 2>&1 || true
  done
fi
# Zipping can drop the executable bit, which on its own makes a .command refuse to open. Re-assert it
# on every launcher so Start/Stop/Status/RTM/Trust all run.
for f in *.command; do [ -e "$f" ] && chmod +x "$f" 2>/dev/null || true; done

PORT="${MONGOOSE_PORT:-8765}"
# Mongoose serves HTTPS by default; use scheme-aware local probes for status/open checks.
# shellcheck source=scripts/mongoose_local_probe.sh
. "$REPO/scripts/mongoose_local_probe.sh"
# Read the version from release.json (the single source of truth) so launcher copy can never drift from
# the actual build. Falls back to a literal only if release.json is missing/unreadable.
_read_version() {
  if [ -f release.json ]; then
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' release.json | head -1
  fi
}
WRAPPER_VERSION="$(_read_version)"; [ -n "$WRAPPER_VERSION" ] || WRAPPER_VERSION="1.1"
mkdir -p data/logs
LOG="data/logs/install-command-wrapper.log"
touch "$LOG" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1

expected_version_from_archive() {
  if [[ -f release.json ]]; then
    /usr/bin/python3 -c 'import json; print(json.load(open("release.json")).get("version", ""))' 2>/dev/null
    return 0
  fi
  if [[ -f pyproject.toml ]]; then
    /usr/bin/python3 -c 'import re; t=open("pyproject.toml").read(); m=re.search(r"^version\s*=\s*\"([^\"]+)\"", t, re.M); print(m.group(1) if m else "")' 2>/dev/null
    return 0
  fi
  return 0
}

# Expose `mongoose` as a real command after install, so the user can run `mongoose status` (etc.) from
# any terminal , NOT just inside this script's activated venv. The console-script lives at
# .venv/bin/mongoose (a pip entry point); without this it is invisible to an interactive shell, which
# is the "command not found" gap. Strategy, best-effort and idempotent:
#   1) symlink .venv/bin/mongoose into a standard PATH dir if one is writable (/usr/local/bin, or the
#      Homebrew bin on Apple Silicon). No sudo: only dirs we can already write are used.
#   2) fallback: append a single PATH line to the user's shell rc (~/.zshrc, since macOS defaults to
#      zsh) pointing at this install's .venv/bin , guarded so re-running never duplicates it.
# Always prints exactly what it did and how to make it live, so nothing is hidden.
link_mongoose_onto_path() {
  local bin="$REPO/.venv/bin/mongoose"
  if [[ ! -x "$bin" ]]; then
    echo "  Note: mongoose CLI not found at .venv/bin/mongoose; skipping PATH setup."
    return 0
  fi

  # (1) Try a standard PATH directory we can write to without sudo.
  local target_dir=""
  local d
  for d in /usr/local/bin /opt/homebrew/bin "$HOME/.local/bin"; do
    if [[ -d "$d" && -w "$d" ]]; then target_dir="$d"; break; fi
    # /usr/local/bin may not exist yet but be creatable on Intel Macs
    if [[ "$d" == "/usr/local/bin" && ! -e "$d" ]] && mkdir -p "$d" 2>/dev/null; then target_dir="$d"; break; fi
  done
  if [[ -n "$target_dir" ]]; then
    if ln -sf "$bin" "$target_dir/mongoose" 2>/dev/null; then
      echo "  Linked the 'mongoose' command into $target_dir."
      if command -v mongoose >/dev/null 2>&1; then
        echo "  You can now run 'mongoose status' from any terminal."
      else
        echo "  ($target_dir is not on your PATH yet; open a new terminal, or see the line below.)"
      fi
      return 0
    fi
  fi

  # (2) Fallback: add this install's venv bin to ~/.zshrc (guarded, no duplicates).
  local rc="$HOME/.zshrc"
  local marker="# added by Mongoose installer"
  local line="export PATH=\"$REPO/.venv/bin:\$PATH\"  $marker"
  if [[ -f "$rc" ]] && grep -qF "$REPO/.venv/bin" "$rc" 2>/dev/null; then
    echo "  'mongoose' PATH entry already present in ~/.zshrc."
  else
    printf '\n%s\n' "$line" >> "$rc" 2>/dev/null \
      && echo "  Added 'mongoose' to your PATH in ~/.zshrc." \
      || echo "  Could not modify ~/.zshrc automatically."
  fi
  echo "  To use it in THIS terminal now, run:  export PATH=\"$REPO/.venv/bin:\$PATH\""
  echo "  New terminals will have 'mongoose' automatically."
}


printf '\n  Mongoose v%s installer\n  Setting up your local assistant. This window will tell you when it is ready.\n  Log: data/logs/install-command-wrapper.log\n\n' "$WRAPPER_VERSION"
printf '  Wrapper loaded. Checking current install state on port %s...\n' "$PORT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "  This installer is for macOS. On other systems, see README.md (Quickstart)."
  echo "  Press any key to close." ; read -n 1 -s || true ; exit 1
fi

# Force-free the port when a graceful `mongoose stop` didn't (stale/old pidfile, or an old install in
# a different dir). ONLY kills a process whose command actually looks like a Mongoose server , never
# an unrelated process that happens to use the port. Returns 0 if the port is free afterwards.
force_stop_mongoose_on_port() {
  mongoose_curl "/health" >/dev/null || { _kill_owned_port_holders; return $?; }   # not answering: try identity-based stop
  mongoose_curl "/health" | grep -q '"name":"mongoose"' || { _kill_owned_port_holders; return $?; }  # answering but not ours-by-health: still try identity
  # If a Mongoose launch agent is loaded, bootout/unload it first , otherwise KeepAlive would respawn
  # the server the instant we kill the PID, and the new install could never bind the port. Best-effort.
  local _uid; _uid="$(id -u 2>/dev/null || echo 0)"
  for label in com.mongoose.scheduler com.mongoose.server com.mongoose.api; do
    if launchctl list 2>/dev/null | grep -q "$label"; then
      launchctl bootout "gui/$_uid/$label" 2>/dev/null \
        || launchctl bootout "gui/$_uid" "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null \
        || launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
    fi
  done
  # The two curl guards at the top already PROVED a mongoose server is what's answering on this
  # port. So the PID(s) listening on it are that mongoose server , kill them. We re-check /health
  # before each kill (defensive: never kill a non-mongoose process that grabbed the port in between),
  # but we do NOT additionally require ps's command line to match a narrow pattern. That pattern gate
  # was the bug: a launch-agent / different-path old server didn't match it, so the kill was skipped
  # and the new install could never bind. Loop a few times in case KeepAlive briefly respawns it.
  if command -v lsof >/dev/null 2>&1; then
    local _try
    for _try in 1 2 3; do
      mongoose_curl "/health" | grep -q '"name":"mongoose"' || break
      for pid in $(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null); do
        mongoose_curl "/health" | grep -q '"name":"mongoose"' || break
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        kill -9 "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      done
      for _ in $(seq 1 12); do mongoose_curl "/health" >/dev/null || break; sleep 0.25; done
    done
  fi
  for _ in $(seq 1 20); do mongoose_curl "/health" >/dev/null || break; sleep 0.25; done
  ! mongoose_curl "/health" >/dev/null
}

# Prove a PID is OUR Mongoose without relying on /health (which a wedged server may not answer):
#   (a) it matches data/mongoose-server.pid we wrote, OR
#   (b) its command line is a python running THIS folder's `mongoose serve` (argv/cwd contains $REPO).
# This is a STRONGER ownership proof than /health for the stuck case, and still safe: a random user
# process on the port matches neither and is left alone.
_cmd_is_mongoose_server() {
  local low; low="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$low" in *mongoose.app:app*) case "$low" in *uvicorn*) return 0;; esac ;; esac
  case "$low" in *mongoose*" serve"*) return 0 ;; esac
  return 1
}
_pid_is_our_mongoose() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  # (a) exact pidfile match
  if [[ -f data/mongoose-server.pid ]]; then
    local fpid; fpid="$(head -n1 data/mongoose-server.pid 2>/dev/null | tr -dc '0-9')"
    [[ -n "$fpid" && "$fpid" == "$pid" ]] && return 0
  fi
  # (b) the process's current working directory IS this install , exact
  if command -v lsof >/dev/null 2>&1; then
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep -qx "n$REPO" && return 0
  fi
  # (c) command line looks like a mongoose server by the proven pattern (full ps command, not lsof-trunc):
  # "mongoose" + " serve", or uvicorn + "mongoose.app:app". Won't match a path like .../mongoose-ntfy/...
  _cmd_is_mongoose_server "$(ps -o command= -p "$pid" 2>/dev/null)" && return 0
  return 1
}

# Kill any listener on $PORT that we can PROVE is our Mongoose by identity (pidfile/cmdline/cwd), even
# if /health is dead. Tears down launch agents first so KeepAlive can't respawn. Returns 0 if the port
# is free afterwards. Leaves unprovable (foreign) processes untouched.
_kill_owned_port_holders() {
  command -v lsof >/dev/null 2>&1 || return 1
  local pid killed=0
  # bootout any mongoose launch agent first (else it respawns the server we kill)
  local _uid; _uid="$(id -u 2>/dev/null || echo 0)"
  for label in com.mongoose.scheduler com.mongoose.server com.mongoose.api; do
    launchctl list 2>/dev/null | grep -q "$label" && launchctl bootout "gui/$_uid/$label" 2>/dev/null || true
  done
  for pid in $(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null); do
    if _pid_is_our_mongoose "$pid"; then
      echo "  Stopping a stuck Mongoose process (PID $pid) from a previous run that was holding port $PORT..."
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      kill -9 "$pid" 2>/dev/null || true
      killed=1
    fi
  done
  # report whether the port is now free
  ! ( command -v lsof >/dev/null 2>&1 && lsof -n -P -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 )
}

# Find PIDs listening on $PORT WITHOUT requiring lsof. Prefers lsof; falls back to parsing the macOS
# `netstat -anv` table for the port and resolving owners. Prints one PID per line (may be empty).
_pids_on_port() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null
    return
  fi
  # lsof unavailable: derive PIDs from netstat. macOS `netstat -anv -p tcp` includes the owning pid in
  # the last columns for LISTEN sockets on *.PORT / 127.0.0.1.PORT.
  netstat -anv -p tcp 2>/dev/null \
    | awk -v p=".$PORT" '$0 ~ /LISTEN/ && ($4 ~ p"$") {print $9}' \
    | grep -E '^[0-9]+$' | sort -u
}

# Is the process with this PID a Mongoose server? (full ps command; identity-only, no /health.)
_pid_is_mongoose_serve() {
  _cmd_is_mongoose_server "$(ps -o command= -p "$1" 2>/dev/null)"
}

# THE single, transparent port-clearing routine used by BOTH the first-run and already-installed paths.
# Goal: leave $PORT free for THIS install to bind, while NEVER killing an unrelated process.
# Order of attempts, each logged:
#   1) graceful `mongoose stop` (validates pids itself),
#   2) for each PID on the port: if its command line is a `mongoose serve` (an orphaned/wedged Mongoose
#      from ANY folder, incl. one moved to Trash), tear down launch agents and kill it,
#   3) re-check; if still held by something that is NOT a mongoose serve and does NOT answer /health as
#      mongoose, STOP with an actionable message naming the exact holder. Returns 0 if the port is free.
ensure_port_free_for_mongoose() {
  # (1) graceful
  mongoose stop >/dev/null 2>&1 || true
  for _ in $(seq 1 8); do _pids_on_port | grep -q . || { echo "  Port $PORT is free."; return 0; }; sleep 0.25; done

  # tear down any respawning launch agent before killing, so it can't come back
  local _uid; _uid="$(id -u 2>/dev/null || echo 0)"
  for label in com.mongoose.scheduler com.mongoose.server com.mongoose.api; do
    launchctl list 2>/dev/null | grep -q "$label" && {
      echo "  Removing Mongoose launch agent $label (so it cannot respawn the old server)..."
      launchctl bootout "gui/$_uid/$label" 2>/dev/null \
        || launchctl bootout "gui/$_uid" "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null \
        || launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
    }
  done

  # (2) identity-based kill: any `mongoose serve` on the port is ours to stop, whatever folder it ran
  # from. We log the command line we matched so the decision is auditable.
  local pid cmd acted=0
  for pid in $(_pids_on_port); do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null)"
    if _pid_is_mongoose_serve "$pid"; then
      echo "  Found a Mongoose server holding port $PORT (PID $pid); stopping it:"
      echo "    $cmd"
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      kill -9 "$pid" 2>/dev/null || true
      acted=1
    fi
  done
  [ "$acted" = "1" ] && for _ in $(seq 1 12); do _pids_on_port | grep -q . || break; sleep 0.25; done

  # (3) verdict
  if ! _pids_on_port | grep -q .; then echo "  Port $PORT is free."; return 0; fi
  # still held , is it actually a live mongoose answering /health? then a stop race; one more try.
  if mongoose_curl "/health" | grep -q '"name":"mongoose"'; then
    mongoose stop >/dev/null 2>&1 || true
    for _ in $(seq 1 12); do _pids_on_port | grep -q . || { echo "  Port $PORT is free."; return 0; }; sleep 0.25; done
  fi
  return 1
}

# Print an actionable failure naming the exact holder of $PORT and how to free it.
_report_port_blocked() {
  local pid cmd
  pid="$(_pids_on_port | head -n1)"
  cmd="$(ps -o command= -p "$pid" 2>/dev/null | cut -c1-100)"
  echo "  ERROR: port $PORT is in use by a process that is NOT a Mongoose server, so the installer will"
  echo "  not kill it (it could be something of yours)."
  echo "    holder: PID ${pid:-?}  ${cmd:-(unknown command)}"
  echo "  Free the port and re-run:   kill ${pid:-<pid>}    (or:  lsof -ti tcp:$PORT -sTCP:LISTEN | xargs kill )"
  echo "  (Mongoose's own optional containers use 8090/8888, not $PORT.)"
}


_choose_install_preloader_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()' 2>/dev/null \
    || /usr/bin/python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()' 2>/dev/null
}


launch_install_preloader() {
  # Human-facing bootstrap page. It runs on a temporary loopback-only port so the browser has
  # something useful to show BEFORE the real FastAPI /install route exists. It then hands off to the
  # real /install only after that route is proven renderable. This removes the blank
  # https://127.0.0.1:$PORT/install spinner during fresh install/configure work.
  local title="${1:-Mongoose is getting ready}"
  local log_path="${2:-$REPO/data/logs/install-command-wrapper.log}"
  local py=""
  if command -v python3 >/dev/null 2>&1; then py="$(command -v python3)"; elif [[ -x /usr/bin/python3 ]]; then py="/usr/bin/python3"; fi
  [[ -n "$py" && -f scripts/install_preloader.py ]] || return 1
  INSTALL_PRELOADER_PORT="${MONGOOSE_INSTALL_PRELOADER_PORT:-$(_choose_install_preloader_port)}"
  [[ -n "$INSTALL_PRELOADER_PORT" ]] || return 1
  mkdir -p data/logs
  # nohup deliberately lets the page survive this shell briefly; the preloader self-exits after the
  # real page has been ready for a few minutes, or after a hard max lifetime.
  nohup "$py" scripts/install_preloader.py \
    --port "$INSTALL_PRELOADER_PORT" \
    --main-port "$PORT" \
    --log "$log_path" \
    --title "$title" \
    --exit-after-ready 180 \
    --max-seconds 1800 \
    > data/logs/install-preloader.log 2>&1 &
  export MONGOOSE_INSTALL_PRELOADER=1
  export MONGOOSE_INSTALL_PRELOADER_PORT
  # Give the tiny server a moment to bind, then open its immediate human page. If it fails to bind,
  # return non-zero so the legacy /install opener remains as fallback.
  for _ in $(seq 1 20); do
    if curl --connect-timeout 1 --max-time 1 -fsS "http://127.0.0.1:${INSTALL_PRELOADER_PORT}/status" >/dev/null 2>&1; then
      open "http://127.0.0.1:${INSTALL_PRELOADER_PORT}/installing" >/dev/null 2>&1 || true
      printf '  Opened temporary install progress page: http://127.0.0.1:%s/installing\n' "$INSTALL_PRELOADER_PORT"
      return 0
    fi
    sleep 0.1
  done
  return 1
}

open_install_screen() {
  # Open the browser as soon as the server answers /health.
  # Runs in the background so the bootstrap or restart can keep working.
  # On an update (server starts in ~2s) the browser opens almost immediately after double-click.
  # On a fresh install it opens as soon as bootstrap finishes starting the server.
  # We no longer gate on version match: that check caused the "browser opens late" problem by
  # waiting for the old server to die AND the new one to start AND its version to match — which
  # could take 20-30s. But /health alone is not enough: open only once the actual /install page
  # renders, so a live API with a broken web UI does not look successful.
  for _ in $(seq 1 180); do
    if mongoose_curl "/health" | grep -q '"name":"mongoose"' \
       && mongoose_page_ready "/install" "Installing Mongoose|Mongoose"; then
      # Open on LOCALHOST: /install is the owner setup page and the owner-pairing secret is
      # loopback-only. The phone QR inside the page still targets the LAN/DDNS address.
      mongoose_open_local "/install" >/dev/null
      return 0
    fi
    sleep 0.5
  done
  # Do NOT open on timeout. bootstrap_macos.sh will open the
  # install page itself once the new server is genuinely up.
  return 1
}

printf '  Checking for any previous Mongoose server on port %s...\n' "$PORT"

# ── Existing-install detection ──────────────────────────────────────────────────
# The brain (conversation, memory, identity) lives at ~/.mongoose/data/, OUTSIDE this folder, so it
# survives swapping the code. Config (.env) and integration tokens (secrets/) live in THIS folder. If
# we detect a prior Mongoose, offer a choice rather than silently doing one or the other:
#   - Update (default): keep your brain, config, and connected integrations; just refresh the code.
#   - Clean: start fresh (brain, config and tokens are archived first, never deleted outright).
BRAIN_DB="$HOME/.mongoose/data/mongoose.sqlite3"
HAVE_BRAIN=0; [[ -f "$BRAIN_DB" ]] && HAVE_BRAIN=1
HAVE_CONFIG=0; [[ -f .env ]] && HAVE_CONFIG=1
HAVE_SECRETS=0; [[ -d secrets && -n "$(ls -A secrets 2>/dev/null || true)" ]] && HAVE_SECRETS=1
MONGOOSE_UPDATE_MODE="fresh"   # fresh | update | clean
if [[ "$HAVE_BRAIN" == "1" || "$HAVE_CONFIG" == "1" || "$HAVE_SECRETS" == "1" ]]; then
  printf '\n  Mongoose detected on this Mac.\n'
  [[ "$HAVE_BRAIN" == "1" ]]   && printf '    • Existing brain (your conversation + memory) at ~/.mongoose/data\n'
  [[ "$HAVE_CONFIG" == "1" ]]  && printf '    • Existing config (.env)\n'
  [[ "$HAVE_SECRETS" == "1" ]] && printf '    • Connected integrations (secrets/)\n'
  printf '\n  How would you like to proceed?\n'
  printf '    [U] Update  (default) — keeps everything: your conversation, settings, and integrations\n'
  printf '    [C] Fresh start — your data is archived first (never deleted), then a clean install runs\n'
  # Non-interactive runs (CI, double-click with no TTY) default to the safe UPDATE path.
  ans="U"
  if [[ -t 0 ]]; then
    printf '  Your data is kept by default. Press Enter to update, or type C for a fresh start: '
    read -r reply || true
    [[ -n "${reply:-}" ]] && ans="$reply"
  else
    printf '  (no terminal input available; defaulting to Update , your data is preserved)\n'
  fi
  case "$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]' | cut -c1)" in
    c)
      MONGOOSE_UPDATE_MODE="clean"
      STAMP="$(date +%Y%m%d-%H%M%S)"
      ARCHIVE="$HOME/.mongoose/archive-$STAMP"
      printf '  Clean update chosen. Archiving your current brain/config/tokens to %s ...\n' "$ARCHIVE"
      mkdir -p "$ARCHIVE" 2>/dev/null || true
      [[ "$HAVE_BRAIN" == "1" ]]   && cp -p "$HOME/.mongoose/data/"mongoose.sqlite3* "$ARCHIVE/" 2>/dev/null || true
      [[ "$HAVE_BRAIN" == "1" ]]   && cp -p "$HOME/.mongoose/data/session_salt" "$ARCHIVE/" 2>/dev/null || true
      [[ "$HAVE_CONFIG" == "1" ]]  && cp -p .env "$ARCHIVE/" 2>/dev/null || true
      [[ "$HAVE_SECRETS" == "1" ]] && cp -Rp secrets "$ARCHIVE/secrets" 2>/dev/null || true
      # Move the live brain aside so a fresh one is created; archived copy is the restore point.
      [[ "$HAVE_BRAIN" == "1" ]]   && rm -f "$HOME/.mongoose/data/"mongoose.sqlite3* "$HOME/.mongoose/data/session_salt" 2>/dev/null || true
      [[ "$HAVE_CONFIG" == "1" ]]  && rm -f .env 2>/dev/null || true
      printf '  Archived. Starting fresh. (To restore later, copy files back from %s.)\n' "$ARCHIVE"
      ;;
    *)
      MONGOOSE_UPDATE_MODE="update"
      printf '  Update chosen. Your brain, config and connected integrations are preserved.\n'
      # Take a verified brain snapshot before any code swap, so the update is always roll-back-able.
      printf '  Taking a pre-update brain snapshot...\n'
      if [[ -f .venv/bin/mongoose ]]; then
        .venv/bin/mongoose brain snapshot 2>/dev/null           && printf '  ✓ Brain snapshot taken.\n'           || printf '  (Brain snapshot unavailable; full-tree backup still covers you.)\n'
      fi
      ;;
  esac
  export MONGOOSE_UPDATE_MODE
fi
# ── Old-install recovery hint ─────────────────────────────────────────────────
# If this is a FRESH install (brain/config not found above), check whether there's an older Mongoose
# installation nearby whose .env contains HA_URL, IMAP settings, and other config. The user cannot
# know to do this on their own. Print an actionable hint rather than letting them re-configure from scratch.
if [[ "$MONGOOSE_UPDATE_MODE" == "fresh" ]]; then
  # Look for a sibling Mongoose folder (common pattern: unzipped into the same place each time)
  OLD_ENV=""
  for candidate in ../mongoose-alpha*/.env ../Mongoose*/.env ~/Desktop/mongoose-alpha*/.env ~/Downloads/mongoose-alpha*/.env; do
    if [[ -f "$candidate" && "$candidate" != "./.env" ]]; then
      OLD_ENV="$candidate"
      break
    fi
  done
  if [[ -n "$OLD_ENV" ]]; then
    printf '\n  ✓ Found your previous Mongoose config. Restoring it automatically...\n'
    # Auto-copy into this install's folder. cp -n = no-clobber (safe to run twice).
    # We resolve the absolute path before copying so this works regardless of what
    # directory the user is in when they double-click the .command file.
    _abs_old="$(cd "$(dirname "$OLD_ENV")" && pwd)/$(basename "$OLD_ENV")" 2>/dev/null || _abs_old="$OLD_ENV"
    if cp -n "$_abs_old" "$REPO/.env" 2>/dev/null; then
      printf '  ✓ Your Home Assistant, mail, and integration settings have been restored.\n\n'
    elif [[ -f "$REPO/.env" ]]; then
      # .env already exists — do a safe merge: old values fill in any gaps, existing values win
      python3 -c "
import sys
old=open(sys.argv[1]).read(); new_f=sys.argv[2]
try: cur=open(new_f).read()
except: cur=''
old_kv={l.split('=',1)[0]:l for l in old.splitlines() if '=' in l and not l.startswith('#')}
cur_kv={l.split('=',1)[0]:l for l in cur.splitlines() if '=' in l and not l.startswith('#')}
merged={**old_kv, **cur_kv}  # current wins
with open(new_f,'w') as f: f.write('
'.join(merged.values())+'
')
" "$_abs_old" "$REPO/.env" 2>/dev/null         && printf '  ✓ Previous settings merged into your existing config.\n\n'         || printf '  Your previous config is at: %s (could not merge automatically).\n\n' "$_abs_old"
    else
      printf '  Could not copy automatically. Your previous config is at:\n  %s\n\n' "$_abs_old"
    fi
  fi
fi
# ────────────────────────────────────────────────────────────────────────────────

# First run (no environment yet): before doing the full bootstrap, make sure an OLD server from a
# previous install isn't squatting the port (otherwise the browser would open the previous install).
if [[ ! -d .venv ]]; then
  # Clear any previous install's progress up front, so the live page can't show a prior install as
  # already finished while THIS one is just starting.
  rm -f data/install_progress.json 2>/dev/null || true
  # Free the port for THIS install before bootstrapping. The robust routine handles a live server, a
  # wedged/orphaned `mongoose serve` (incl. one from a folder you moved to Trash , the exact case that
  # blocked installs), and returns fast if nothing is there. It will not kill a non-Mongoose process.
  if _pids_on_port | grep -q .; then
    echo "  Something is on port $PORT. Making sure it is clear before installing..."
    if ! ensure_port_free_for_mongoose; then
      _report_port_blocked
      echo "  Press any key to close." ; read -n 1 -s || true ; exit 1
    fi
  fi
  printf '  Starting the first-run bootstrap. This can take several minutes on first install.\n'
  printf '  Progress is also written to data/logs/install-command.log.\n\n'
  mkdir -p data/logs
  printf '  Starting bootstrap script now...\n'
  if ! launch_install_preloader "Mongoose is installing" "$REPO/data/logs/install-command.log"; then
    ( open_install_screen ) &
  fi
  if bash scripts/bootstrap_macos.sh 2>&1 | tee -a data/logs/install-command.log; then
    bootstrap_rc=0
  else
    bootstrap_rc=${PIPESTATUS[0]}
  fi
  # Decide the OUTCOME CLASS from the install-phase state machine, not from the raw exit code. The bug
  # this fixes: a non-zero exit from a best-effort post-readiness step (e.g. the auto-restart watchdog)
  # was reported as "Install failed before Mongoose became ready" even though the smoke test had already
  # proven Mongoose was up and serving. Readiness is proven at the smoke step; once that passes, the
  # only honest messages are "fully installed" or "installed and running, but the watchdog didn't load".
  #
  # CRITICAL: query the phase via the venv, NOT a bare `mongoose`. At this point in the wrapper the
  # console script is not on PATH (it lives in .venv/bin, and the PATH/rc setup only affects FUTURE
  # shells). A bare `mongoose` here silently fails, `|| echo false` makes READY_PROVEN=false, and a
  # fully-successful install gets reported as "failed before ready". Use the absolute venv entrypoint.
  if [[ -x .venv/bin/mongoose ]]; then
    MONGOOSE_CLI=(.venv/bin/mongoose)
  elif [[ -x .venv/bin/python ]]; then
    MONGOOSE_CLI=(.venv/bin/python -m mongoose.cli)
  else
    MONGOOSE_CLI=(python3 -m mongoose.cli)
  fi
  READY_PROVEN="$( "${MONGOOSE_CLI[@]}" _install-phase --field ready_proven 2>/dev/null || echo false )"
  WD_OK="$( "${MONGOOSE_CLI[@]}" _install-phase --field watchdog_ok 2>/dev/null || echo false )"
  WD_DETAIL="$( "${MONGOOSE_CLI[@]}" _install-phase --field watchdog_detail 2>/dev/null || true )"
  # Belt and braces: if for any reason the phase query couldn't run but the bootstrap itself exited 0,
  # treat that as ready. The bootstrap only reaches exit 0 after it marks the install ready, so a clean
  # bootstrap exit is itself proof of readiness. This guarantees we never misreport a successful install.
  if [[ "$READY_PROVEN" != "true" && "${bootstrap_rc:-1}" -eq 0 ]]; then
    READY_PROVEN="true"
  fi
  if [[ "$READY_PROVEN" == "true" ]]; then
    if [[ "$WD_OK" == "true" ]]; then
      printf '\n  Mongoose is installed, running, and protected by the auto-restart watchdog.\n'
      printf '  Open it: %s\n' "$(mongoose_local_url /install)"
    else
      # Mongoose became ready, but a final post-install step failed. This is the phase-true message: it
      # must never say "failed before ready" here, because the smoke test already proved readiness.
      printf '\n  Mongoose became ready, but a final post-install step failed.\n'
      printf '  Mongoose is installed and running. You can use it now.\n'
      printf '  The auto-restart watchdog could not be installed, so Mongoose may not restart itself after a crash.\n'
      [[ -n "${WD_DETAIL:-}" ]] && printf '  Reason: %s\n' "$WD_DETAIL"
      printf '  You can add it later with: mongoose server-watchdog --install\n'
      printf '  Open it: %s\n' "$(mongoose_local_url /install)"
    fi
    # Either way this is a SUCCESS: Mongoose is usable. Exit 0 and do not block with a failure prompt.
  else
    # Genuine failure: a pre-readiness step (prerequisites, environment, database, server, smoke) failed.
    printf '\n  Install failed before Mongoose became ready. See data/logs/install-command.log.\n'
    printf '  Press any key to close.\n'
    read -n 1 -s || true
    exit "${bootstrap_rc:-1}"
  fi
else
  # Already set up. CRITICAL: always restart into THIS install's version. A server from an
  # older install may already be running on the port; if we just checked /health and saw "ok"
  # we'd leave the OLD version running and you'd never see the new one. So: stop whatever is
  # running, then start fresh from here.
  source .venv/bin/activate 2>/dev/null || true

  printf '  Existing .venv found. Checking Ollama and restarting Mongoose...\n'
  if command -v ollama >/dev/null 2>&1 && ! curl --connect-timeout 1 --max-time 2 -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    ( ollama serve >/dev/null 2>&1 & ) || true
  fi

  # Stop any running Mongoose and free the port for THIS install. One transparent routine handles
  # graceful stop, identity-based kill of an orphaned/wedged `mongoose serve` (from any folder, even one
  # trashed), and a clear verdict. It does NOT depend on lsof being present.
  EXPECTED_VERSION="$(mongoose version 2>/dev/null | tr -d '[:space:]' || true)"
  RUNNING_VERSION="$(mongoose_curl "/health" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' || true)"
  if [[ -n "$RUNNING_VERSION" ]]; then
    echo "  A Mongoose server is already running (version $RUNNING_VERSION). Restarting into this install..."
  fi
  if ! ensure_port_free_for_mongoose; then
    _report_port_blocked
    exit 1
  fi

  mkdir -p data/logs
  # Do NOT hardcode --host here: that would override MONGOOSE_HOST from .env, so enabling phone/LAN
  # access (host=0.0.0.0) could never take effect. `mongoose serve` reads the setting (default
  # 127.0.0.1, loopback-only) so the default stays private until the user opts in.
  ( mongoose serve --port "$PORT" >> data/logs/app.log 2>&1 & ) || true
  # Open a temporary human page immediately, then let it hand off once the real /install route is
  # renderable. Falling back to the old gated opener is still better than a broken install if the
  # tiny preloader cannot start for some reason.
  if ! launch_install_preloader "Mongoose is restarting"; then
    ( open_install_screen ) &
  fi
  started=0
  for _ in $(seq 1 60); do
    if mongoose_curl "/health" >/dev/null; then started=1; break; fi
    sleep 0.5
  done
  if [[ "$started" -ne 1 ]]; then
    echo "  ERROR: Mongoose did not answer readiness probes on port $PORT. See data/logs/mongoose.err.log"
    exit 1
  fi
  if ! mongoose_page_ready "/install" "Installing Mongoose|Mongoose"; then
    echo "  ERROR: Mongoose answered /health, but the install web UI did not load."
    echo "  Last lines of the error log:"
    tail -n 40 data/logs/mongoose.err.log 2>/dev/null || true
    exit 1
  fi

  # Confirm we're now actually running this install's version (so a stale server can't masquerade).
  NOW_VERSION="$(mongoose_curl "/health" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' || true)"
  if [[ -n "$EXPECTED_VERSION" && -n "$NOW_VERSION" && "$NOW_VERSION" != "$EXPECTED_VERSION" ]]; then
    echo "  WARNING: the running server reports version $NOW_VERSION but this install is $EXPECTED_VERSION."
    echo "  Another Mongoose may be running from a different folder. Stop it, then re-run this installer."
  elif [[ -n "$NOW_VERSION" ]]; then
    echo "  Running Mongoose version $NOW_VERSION. Tip: hard-refresh the browser (Cmd+Shift+R) to clear any cached page."
  fi

  # Browser was already opened in the background via open_install_screen above.
  # No extra open needed here.
fi

# Make `mongoose` a real command for the user's terminals (the console-script otherwise only resolves
# inside this script's activated venv). Best-effort; prints exactly what it did.
link_mongoose_onto_path

# Phone/LAN setup is handled inside the browser (Settings , Phone pairing card); no need to print
# terminal instructions that nobody reads once the browser is open.

printf '\n  Mongoose is running. You can close this window.\n\n'
