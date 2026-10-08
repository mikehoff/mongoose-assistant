#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This bootstrap script is for macOS. Use the manual steps in docs/IMPLEMENTATION_STEPS.md on other systems."
  exit 1
fi

mkdir -p data/logs secrets

PORT="${MONGOOSE_PORT:-}"
if [[ -z "$PORT" && -f .env ]]; then
  PORT="$(grep -E '^MONGOOSE_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
fi
PORT="${PORT:-8765}"
# Mongoose serves HTTPS by default; health/open helpers try HTTPS (self-signed OK) and HTTP fallback.
# shellcheck source=scripts/mongoose_local_probe.sh
. "$(dirname "$0")/mongoose_local_probe.sh"

# Reset install progress at the VERY START, before the server can come up and serve a stale page.
# A previous install leaves data/install_progress.json showing "ready/finished"; if the browser
# opens before the CLI exists to reset it (the CLI reset happens later, once the venv is built), the
# user sees the previous install already complete. Removing the file here closes that race, the
# /install page then shows a clean pending state until this run records real progress.
rm -f data/install_progress.json 2>/dev/null || true
_BOOTSTRAP_VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' release.json 2>/dev/null | head -1)"; [ -n "$_BOOTSTRAP_VERSION" ] || _BOOTSTRAP_VERSION="1.1"
echo "Bootstrap started for Mongoose v$_BOOTSTRAP_VERSION on port $PORT."
echo "If this appears to stop, keep this window open and inspect data/logs/install-command.log."

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

echo "Checking Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Installing Homebrew."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
fi

echo "Checking Python 3.12..."
if ! command -v python3.12 >/dev/null 2>&1; then
  echo "Installing Python 3.12 (system python3 may be too old)..."
  brew install python@3.12
fi
PY312="$(command -v python3.12 || true)"
if [[ -z "$PY312" ]]; then
  echo "ERROR: python3.12 not found after install. Install it and re-run."
  exit 1
fi

echo "Checking Claude Code CLI (optional: needed for the three-agent HA coordination)..."
if ! command -v claude >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    NPM_PREFIX="$(npm config get prefix 2>/dev/null || true)"
    # The classic macOS Homebrew-Node failure: the default global npm directory is not owned by
    # this user, so `npm install -g` fails with EACCES, silently, unless output is captured,
    # which this script never did before. Switch to a user-owned prefix (npm's own documented
    # fix) instead of ever suggesting or running sudo.
    if [[ -n "$NPM_PREFIX" ]] && ! [[ -w "$NPM_PREFIX" ]]; then
      echo "  npm's global install directory ($NPM_PREFIX) isn't writable by this user;"
      echo "  switching to a user-owned prefix so no sudo is needed..."
      mkdir -p "$HOME/.npm-global"
      npm config set prefix "$HOME/.npm-global" >/dev/null 2>&1 || true
      export PATH="$HOME/.npm-global/bin:$PATH"
      for rc in "$HOME/.zshrc" "$HOME/.bash_profile"; do
        [[ -f "$rc" ]] || continue
        grep -qs 'npm-global/bin' "$rc" 2>/dev/null || {
          echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> "$rc"
          echo "  Added $HOME/.npm-global/bin to PATH in $rc (new terminal windows pick this up)."
        }
      done
    fi
    echo "  Installing Claude Code via npm..."
    NPM_OUT="$(npm install -g @anthropic-ai/claude-code 2>&1)"
    NPM_RC=$?
    if [[ "$NPM_RC" -eq 0 ]] && command -v claude >/dev/null 2>&1; then
      echo "  Claude Code installed ($(claude --version 2>/dev/null || echo 'version unknown'))."
    else
      echo "  Claude Code install failed. The actual npm error (not hidden this time):"
      printf '%s\n' "$NPM_OUT" | tail -n 10 | sed 's/^/    /'
      echo "  Install manually: npm install -g @anthropic-ai/claude-code"
    fi
  else
    echo "  npm not found; skipping Claude Code install. Install manually: npm install -g @anthropic-ai/claude-code"
  fi
else
  echo "  Claude Code already installed ($(claude --version 2>/dev/null || echo 'version unknown'))."
fi

echo "Checking Ollama..."
if ! command -v ollama >/dev/null 2>&1; then
  echo "Installing Ollama..."
  brew install --cask ollama
fi

echo "Checking Docker Desktop (optional local push/search containers)..."
if ! command -v docker >/dev/null 2>&1; then
  echo ""
  echo "[Optional component] Installing Docker Desktop via Homebrew now."
  echo "  Mongoose uses Docker only as the bundled LOCAL home for two optional services:"
  echo "    - ntfy    : phone push notifications (kept on your machine)"
  echo "    - SearXNG : private web search (kept on your machine)"
  echo "  Neither is required for core Mongoose. If you skip Docker, phone push still works via"
  echo "  'mongoose setup-ntfy' (native, no Docker). Downloading Docker can take a few minutes..."
  brew install --cask docker
  echo "  Docker installed. It will be started and the two containers launched further down,"
  echo "  but only once Docker Desktop itself is running (it may need you to open it once)."
fi

echo "Starting Ollama if needed (bounded check, will not hang forever)..."
if ! pgrep -x "Ollama" >/dev/null 2>&1 && ! curl --connect-timeout 1 --max-time 2 -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  open -a Ollama || true
  sleep 5
fi

# Refuse to reuse a .venv built with an older interpreter (the earlier 3.9 trap).
if [[ -d .venv ]]; then
  VENV_PY="$(.venv/bin/python -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")"
  if [[ "$VENV_PY" != "3.12" && "$VENV_PY" != "3.13" ]]; then
    echo "Existing .venv uses Python $VENV_PY (need 3.12+); recreating it."
    rm -rf .venv
  fi
fi
echo "Creating Python virtual environment with $PY312..."
"$PY312" -m venv .venv
source .venv/bin/activate
echo "Installing Python packages into .venv..."
python -m pip install --upgrade pip
pip install -r requirements.txt
# Install with the 'devices' extra so phone/tablet pairing works out of the box: it carries the
# 'qrcode' library used to render the pairing QR on the welcome screen, and 'zeroconf' for local
# discovery. Without this, /welcome shows a pairing code but no QR to scan (a common first-run snag).
echo "Installing Mongoose itself (with phone-pairing support)..."
pip install -e '.[devices]'

# From here the mongoose CLI exists, so we can record install progress for the /install
# screen. Everything before this (Homebrew/Python/Ollama/venv) was the "prerequisites" step.
mcli() { python -m mongoose.cli "$@"; }

# Config preservation: if the user already had a .env (an update, not a first install), merge any NEW
# config keys this version introduces into it WITHOUT touching their existing values. .env was created
# from .env.example only when it didn't exist (above), so here it always exists; merge-env is a no-op on
# a brand-new .env and additive on an existing one. This is what keeps a user's config + connected
# integrations intact across an update while still picking up new settings.
echo "Merging any new config keys into .env (your existing values are preserved)..."
mcli merge-env >/dev/null 2>&1 || echo "  (env merge step skipped; your .env is unchanged)"
# The version THIS install is putting in place. After we start the server we verify /health reports
# exactly this, so a stale old server still holding the port (which would answer /health with its OWN
# old version) is detected instead of being mistaken for a successful install.
EXPECTED_VERSION="$(python -c 'from mongoose import __version__; print(__version__)' 2>/dev/null || echo '')"
mcli _install-step prerequisites done --begin >/dev/null 2>&1 || true
mcli _install-step environment done >/dev/null 2>&1 || true

mcli _install-step database running >/dev/null 2>&1 || true
echo "Initialising Mongoose database..."
python -m mongoose.cli init-db
mcli _install-step database done >/dev/null 2>&1 || true

# Raise the Metal GPU wired-memory limit so the ~18GB chat model fits with context on a 24GB base M4.
# This needs SUDO, so it is done HERE, up front, while you are still looking at the terminal, BEFORE
# the browser opens. Once the browser is open you tab away, and a sudo prompt you can't see would hang
# the whole install. So all interactive prompts must finish before the server comes up and the page opens.
TOTAL_MB="$(( $(sysctl -n hw.memsize) / 1024 / 1024 ))"
if [[ "$TOTAL_MB" -le 25000 ]]; then
  WIRED_MB=21504   # ~21GB for a 24GB machine, leaving headroom for macOS
  CUR_MB="$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)"
  # Only spend a sudo password if the limit actually needs raising. If a previous run, or the
  # persistent launchd plist, already set it to at least the target, skip the prompt entirely ,
  # re-running bootstrap shouldn't ask for your password when nothing has to change.
  if [[ "$CUR_MB" =~ ^[0-9]+$ ]] && [[ "$CUR_MB" -ge "$WIRED_MB" ]]; then
    echo "Metal GPU wired limit already ${CUR_MB}MB (>= ${WIRED_MB}MB target); skipping, no sudo needed."
  else
    echo "Raising Metal GPU wired limit to ${WIRED_MB}MB (needs sudo, non-persistent)"
    sudo sysctl -w iogpu.wired_limit_mb=$WIRED_MB || echo "Could not set wired limit; large models may fall back to slow paths."
  fi
fi

# ---- HTTPS by default + display font: provision both up front (best-effort, non-fatal) ----
# HTTPS is ON BY DEFAULT so phone voice/notifications work on the LAN (loopback stays trusted).
# Generate the self-signed certificate now so the server serves https from its very first start.
# A self-signed cert makes the browser warn "not secure" once per device; tap through / trust it
# (see the cockpit Setup tab, Voice on phones). This step runs BEFORE the server starts.
python - <<'PYEOF' || echo "  (cert step skipped; the server will generate one when it starts)"
try:
    from mongoose.self_signed_tls import generate_self_signed, self_signed_status
    st = self_signed_status()
    if not (st.get("present") and not st.get("expired")):
        generate_self_signed()
        print("  HTTPS: self-signed certificate generated (https is on by default).")
    else:
        print("  HTTPS: self-signed certificate already present.")
except Exception as e:
    print(f"  HTTPS cert step skipped ({e}); the server will generate one on start.")
PYEOF

# Download + cache the Sillygoose display font (Rajdhani) now, so guests see the real face
# immediately instead of a fallback. Best-effort: if offline, it retries automatically at runtime.
echo "Provisioning the Sillygoose display font (Rajdhani)..."
python - <<'PYEOF' || echo "  (font will be fetched automatically at runtime if this did not complete)"
try:
    from mongoose.font_assets import provision_rajdhani
    r = provision_rajdhani(timeout=15.0)
    ok = bool(r.get("ok"))
    if ok:
        print("  Font: Rajdhani provisioned and cached (source: " + str(r.get("source_css", "")) + ").")
    else:
        tried = r.get("sources_tried", 1)
        print(f"  Font: Rajdhani not fetched now ({tried} source(s) tried, "
              f"{r.get('error') or 'unreachable'}); will retry automatically at runtime. "
              "Not a problem: guest invites already show a clean fallback face until then.")
except Exception as e:
    print(f"  Font step skipped ({type(e).__name__}); will retry at runtime.")
PYEOF

# ---- Server up + browser open NOW (all interactive sudo is done above) ----
mcli _install-step server running >/dev/null 2>&1 || true
# Stop any server already holding the port (e.g. a leftover from a previous install) so we always
# start THIS install's version, never silently leave an old one running. Order matters:
#  1) tear down any launch agent that would RESPAWN the old server (else killing the PID is futile);
#  2) graceful `mongoose stop`;
#  3) if it's still answering, KILL the PID(s) listening on the port, gated ONLY on /health
#     confirming a mongoose server. We do NOT additionally require ps's command line to match a
#     narrow pattern: that gate previously let a launch-agent / path-variant old server survive while
#     the installer gave up. /health saying "mongoose" IS the identity proof; a non-mongoose
#     process never answers it, so it is never killed.
# A helper so the same robust teardown is used here and (below) if the version check still trips.
mongoose_is_on_port () { mongoose_curl "/health" | grep -q '"name":"mongoose"'; }

# Prove a PID is OUR Mongoose WITHOUT relying on /health (a wedged server may not answer). Accept any of:
#   (a) exact match against data/mongoose-server.pid we wrote;
#   (b) the process CWD is this repo (exact, via lsof -d cwd);
#   (c) the command line looks like a Mongoose server by the SAME proven test `mongoose stop` uses:
#       "mongoose" followed by " serve", OR uvicorn running "mongoose.app:app". This is the project's
#       established ownership proof. It does NOT false-match a path like .../mongoose-ntfy/serve.yml
#       (that is "mongoose-ntfy" then "/serve.yml", no "mongoose" directly followed by " serve").
# We use the FULL command from `ps -o command=` (not the lsof-truncated "Python"). A foreign process
# matches none of these and is left alone.
_cmd_is_mongoose_server () {
  local low; low="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$low" in *mongoose.app:app*) case "$low" in *uvicorn*) return 0;; esac ;; esac
  case "$low" in *mongoose*" serve"*) return 0 ;; esac
  return 1
}
bootout_mongoose_agents () {
  # Remove any LaunchAgent whose label/target mentions mongoose so the old server can't respawn.
  command -v launchctl >/dev/null 2>&1 || return 0
  local uid; uid="$(id -u)"
  # Known label + anything matching mongoose in this user's domain.
  launchctl bootout "gui/$uid/com.mongoose.server" 2>/dev/null || true
  launchctl bootout "gui/$uid/com.mongoose.ntfy" 2>/dev/null || true
  launchctl list 2>/dev/null | awk '/[mM]ongoose/{print $3}' | while read -r lbl; do
    [ -n "$lbl" ] && launchctl bootout "gui/$uid/$lbl" 2>/dev/null || true
  done
  # Also unload any mongoose-server plist files (legacy load/unload world).
  for plist in "$HOME/Library/LaunchAgents/"*mongoose*server*.plist; do
    [ -e "$plist" ] && launchctl unload "$plist" 2>/dev/null || true
  done
}
something_on_port () { _pids_on_port | grep -q .; }
force_free_port_of_mongoose () {
  # (retained as a thin shim, superseded by ensure_port_free_for_mongoose, kept so any external caller
  # still resolves; it simply delegates to the unified routine.)
  ensure_port_free_for_mongoose
}
# Find PIDs listening on $PORT WITHOUT requiring lsof (falls back to netstat on macOS).
_pids_on_port () {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null; return
  fi
  netstat -anv -p tcp 2>/dev/null \
    | awk -v p=".$PORT" '$0 ~ /LISTEN/ && ($4 ~ p"$") {print $9}' \
    | grep -E '^[0-9]+$' | sort -u
}
_pid_is_mongoose_serve () { _cmd_is_mongoose_server "$(ps -o command= -p "$1" 2>/dev/null)"; }

# Print exactly what holds the port (process + any docker container), for the log.
diagnose_port_holder() {
  local pid cmd
  for pid in $(_pids_on_port); do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null)"
    echo "  Port $PORT is held by PID $pid: ${cmd:-(unknown)}"
  done
  if command -v docker >/dev/null 2>&1; then
    local cons; cons="$(docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null | grep -E ":$PORT->" || true)"
    [ -n "$cons" ] && { echo "  A Docker container is publishing port $PORT:"; echo "$cons" | sed 's/^/    /'; }
  fi
}

# Stop any Docker container publishing host port $PORT (containers only; never a host process).
free_port_of_docker_container() {
  command -v docker >/dev/null 2>&1 || return 1
  local id stopped=0
  for id in $(docker ps -q 2>/dev/null); do
    if docker inspect --format '{{range $p,$conf := .NetworkSettings.Ports}}{{range $conf}}{{.HostPort}} {{end}}{{end}}' "$id" 2>/dev/null | grep -qw "$PORT"; then
      echo "  Stopping Docker container $(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##') on port $PORT..."
      docker stop "$id" >/dev/null 2>&1 && stopped=1
    fi
  done
  for _ in $(seq 1 12); do something_on_port || break; sleep 0.25; done
  [ "$stopped" = "1" ] && ! something_on_port
}

# THE single port-clearing routine. Frees $PORT for the native server while never killing a foreign
# process. Logs every decision. Handles a live server, a wedged/orphaned `mongoose serve` from ANY
# folder (incl. one moved to Trash, the case that blocked the user), and a leftover container.
ensure_port_free_for_mongoose() {
  _pids_on_port | grep -q . || return 0
  # graceful first
  bootout_mongoose_agents
  mcli stop >/dev/null 2>&1 || .venv/bin/mongoose stop >/dev/null 2>&1 || true
  for _ in $(seq 1 8); do _pids_on_port | grep -q . || return 0; sleep 0.25; done
  # identity kill: any `mongoose serve` on the port is ours to stop, whatever folder it ran from
  local pid cmd acted=0
  # If the port itself answers /health as mongoose, EVERY pid bound to this port is fair game ,
  # this is the identity proof the comment above promises, actually wired in (previously it was
  # documented but never called, so a server whose `ps` command line didn't literally contain
  # "mongoose"/" serve" (seen in the field on a Homebrew framework Python build) survived every
  # teardown forever, regardless of how many updates were applied on top of it).
  local port_says_mongoose=0
  mongoose_is_on_port && port_says_mongoose=1
  for pid in $(_pids_on_port); do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null)"
    if _pid_is_mongoose_serve "$pid" || [ "$port_says_mongoose" = "1" ]; then
      echo "  Found a Mongoose server holding port $PORT (PID $pid); stopping it:"
      echo "    $cmd"
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      kill -9 "$pid" 2>/dev/null || true
      acted=1
    fi
  done
  [ "$acted" = "1" ] && for _ in $(seq 1 12); do _pids_on_port | grep -q . || break; sleep 0.25; done
  _pids_on_port | grep -q . || return 0
  # still held: try a leftover container on the port
  free_port_of_docker_container || true
  _pids_on_port | grep -q . && return 1 || return 0
}

# Clear the port (or fail with an actionable, exact message naming the holder).
if ! ensure_port_free_for_mongoose; then
  echo "Port $PORT is still in use by a process that is NOT a Mongoose server, so the installer will not"
  echo "kill it (it could be something of yours)."
  diagnose_port_holder
  _hp="$(_pids_on_port | head -n1)"
  echo "  Free it and re-run:   kill ${_hp:-<pid>}    (or:  lsof -ti tcp:$PORT -sTCP:LISTEN | xargs kill )"
  echo "  (Mongoose's own optional containers use 8090/8888, not $PORT.)"
  mcli _install-step server failed --detail "Port $PORT held by a non-Mongoose process (PID ${_hp:-?}); user must free it" >/dev/null 2>&1 || true
  exit 1
fi
echo "Starting Mongoose API..."
# Enable phone/LAN access BEFORE serving, so the pairing QR this install is about to show actually
# works when scanned. The install flow exists to pair your phone; binding loopback-only guarantees the
# QR gives "connection refused". This writes MONGOOSE_HOST=0.0.0.0 + a token (idempotent) so the server
# binds to the LAN from the first start. (It's still your network only, local LAN, token-gated, and
# you can turn it off later in Setup.)
.venv/bin/python - <<'PY2' >/dev/null 2>&1 || true
from mongoose.enable_phone import ensure_phone_access_defaults
ensure_phone_access_defaults()
PY2
# Start via `mongoose serve` (not bare uvicorn) so the server writes a pidfile and is identifiable
# as a Mongoose process (makes a later `mongoose stop` safe and precise). Do NOT pass --host: serve
# reads MONGOOSE_HOST from .env (now 0.0.0.0 from the line above), so phone access works out of the box.
nohup .venv/bin/mongoose serve --port $PORT > data/logs/mongoose.out.log 2> data/logs/mongoose.err.log &
sleep 2
# Wait until the server actually answers before declaring the server step done.
started=0
for _ in $(seq 1 60); do
  if mongoose_curl "/health" >/dev/null; then
    started=1
    break
  fi
  sleep 0.5
done
if [[ "$started" -ne 1 ]]; then
  echo "Mongoose did not answer its HTTPS/HTTP readiness probe on port $PORT."
  echo "---- last lines of data/logs/mongoose.err.log ----"
  tail -n 20 data/logs/mongoose.err.log 2>/dev/null || echo "  (no err log)"
  echo "---- what is listening on port $PORT ----"
  if command -v lsof >/dev/null 2>&1; then lsof -n -P -iTCP:$PORT -sTCP:LISTEN 2>/dev/null || echo "  (nothing, per lsof)"; fi
  echo "--------------------------------------------------"
  mcli _install-step server failed --detail "Server did not answer readiness probes on port $PORT; check data/logs/mongoose.err.log" >/dev/null 2>&1 || true
  exit 1
fi
# /health alone is not enough: the web UI can still be broken while the API process is alive. Prove
# the actual first-run page loads before we open the browser or mark the server step done.
install_page_ready=0
for _ in $(seq 1 30); do
  if mongoose_page_ready "/install" "Installing Mongoose|Mongoose"; then
    install_page_ready=1
    break
  fi
  sleep 0.5
done
if [[ "$install_page_ready" -ne 1 ]]; then
  echo "Mongoose is answering /health, but the web UI route /install did not load."
  echo "This usually means the server is alive but the cockpit/install HTML crashed during rendering."
  echo "---- last lines of data/logs/mongoose.err.log ----"
  tail -n 40 data/logs/mongoose.err.log 2>/dev/null || echo "  (no err log)"
  echo "---- probe details ----"
  echo "  internal probe base: $(mongoose_probe_base 2>/dev/null || echo unknown)"
  echo "  browser URL would be: $(mongoose_local_url /install)"
  echo "--------------------------------------------------"
  mcli _install-step server failed --detail "API health passed but /install web UI did not render" >/dev/null 2>&1 || true
  exit 1
fi
# Verify the server now answering is THIS install, not a leftover old one still holding the port. If
# the versions differ, the new server didn't take over (it likely failed to bind), fail clearly
# instead of opening the browser onto stale code.
if [[ -n "$EXPECTED_VERSION" ]]; then
  RUNNING_VERSION="$(mongoose_curl "/health" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
  if [[ -n "$RUNNING_VERSION" && "$RUNNING_VERSION" != "$EXPECTED_VERSION" ]]; then
    echo "The server on port $PORT is running version $RUNNING_VERSION, but this installer set up $EXPECTED_VERSION."
    echo "An old Mongoose is still holding the port. Removing it and restarting this install's server..."
    # Don't just tell the user to kill it, do the robust teardown again, then relaunch ours.
    bootout_mongoose_agents
    force_free_port_of_mongoose || true
    if ! something_on_port; then
      nohup .venv/bin/mongoose serve --port $PORT > data/logs/mongoose.out.log 2> data/logs/mongoose.err.log &
      for _ in $(seq 1 60); do mongoose_curl "/health" >/dev/null && break; sleep 0.5; done
      RUNNING_VERSION="$(mongoose_curl "/health" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
    fi
    if [[ -n "$RUNNING_VERSION" && "$RUNNING_VERSION" != "$EXPECTED_VERSION" ]]; then
      echo "Still serving $RUNNING_VERSION after teardown. A launch agent may be respawning it."
      echo "---- what is listening on port $PORT ----"
      if command -v lsof >/dev/null 2>&1; then lsof -n -P -iTCP:$PORT -sTCP:LISTEN 2>/dev/null; fi
      echo "---- launch agents mentioning mongoose ----"
      launchctl list 2>/dev/null | grep -i mongoose || echo "  (none)"
      echo "Manual: launchctl bootout gui/\$(id -u)/<label> ; lsof -ti tcp:$PORT -sTCP:LISTEN | xargs kill -9 ; re-run."
      echo "--------------------------------------------------"
      mcli _install-step server failed --detail "Old version $RUNNING_VERSION still on port $PORT (expected $EXPECTED_VERSION)" >/dev/null 2>&1 || true
      exit 1
    fi
    echo "Now serving $EXPECTED_VERSION."
  fi
fi
mcli _install-step server done >/dev/null 2>&1 || true

# Open the live /install page NOW: the server is up and healthy, but the long model pull and smoke
# test still lie ahead. Opening here (not at the very end) is the whole point: the user watches the
# brain download and the steps tick over in real time, instead of the page appearing only once
# everything is already at 100%. The browser opens HERE, right after the Metal sudo (the last keyboard
# prompt) and the server coming up, BEFORE the slow voice toolchain, Docker, and model pull below. That
# way the live /install page is on screen and the progress bar fills in front of you THROUGH the slow
# work, instead of the browser appearing only once most of the install has already happened invisibly.
if [[ "${MONGOOSE_INSTALL_PRELOADER:-}" == "1" ]]; then
  echo "The temporary browser progress page is open; it will hand off to the live install screen now that Mongoose is ready."
else
  INSTALL_URL="$(mongoose_open_local "/install")"
  echo "Opened the live install screen: $INSTALL_URL (watch the rest install there)."
fi


# Voice mic toolchain (optional but the `voice --session` mic mode needs it):
# sox + ffmpeg via brew, openai-whisper into this venv. Output-only voice (say)
# needs none of this. Failures here are non-fatal.
echo "Installing voice mic toolchain (sox, ffmpeg, openai-whisper)..."
if command -v brew >/dev/null 2>&1; then
  brew install sox ffmpeg || echo "  (sox/ffmpeg install had issues; run 'mongoose setup-voice' later)"
fi
python -m pip install openai-whisper || echo "  (whisper install had issues; run 'mongoose setup-voice' later)"


# NOTE: model pull (the long "downloading the local brain" step) is deliberately deferred until AFTER
# the server is up and the browser has opened, see below, just after "server done". That way the live
# /install page is visible WHILE the brain downloads, instead of opening only once everything is already
# finished (which made every step show a checkmark immediately).

# If Docker is installed but the daemon isn't up yet, TRY to start Docker Desktop and wait, rather
# than just telling the user it's not running while compose files sit right there. This is the
# confusing case: Docker present, containers defined, but the install says "not running".
if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
  echo ""
  echo "[Optional services] Docker is installed but not running yet, starting Docker Desktop and"
  echo "  waiting for it (up to ~60s). The ntfy (phone push) and SearXNG (web search) containers will"
  echo "  launch automatically once the engine is up."
  open -a Docker >/dev/null 2>&1 || open -a "Docker Desktop" >/dev/null 2>&1 || true
  for _ in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 1
  done
  if docker info >/dev/null 2>&1; then
    echo "  Docker is up."
  else
    echo "  Docker didn't finish starting in time. It often needs to be opened once manually the very"
    echo "  first time (to accept its terms). You can re-run this installer after it's running, or use"
    echo "  native push instead:  mongoose setup-ntfy"
  fi
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo ""
  echo "[Optional services] Docker is running, launching the local ntfy (phone push) and SearXNG"
  echo "  (private web search) containers now. First launch downloads their images (a few minutes)."
  mcli _install-step services running >/dev/null 2>&1 || true

  # Stop any EXISTING container (from ANY compose project) that is already publishing a host port we
  # need. This is the common re-run failure: each download folder becomes its own compose project
  # (mongoose, mongoose-2, mongoose-4, ...), so a leftover searxng/ntfy container from
  # a previous attempt still holds 127.0.0.1:8888 or :8090, and `docker compose up`, which only manages
  # ITS OWN project, then fails with "port is already allocated". We clear those holders first.
  # Only touches CONTAINERS, never host processes. Best-effort.
  stop_container_on_host_port() {
    local want="$1" id names
    command -v docker >/dev/null 2>&1 || return 0
    for id in $(docker ps -q 2>/dev/null); do
      if docker inspect --format '{{range $p,$conf := .NetworkSettings.Ports}}{{range $conf}}{{.HostPort}} {{end}}{{end}}' "$id" 2>/dev/null | grep -qw "$want"; then
        names="$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##')"
        echo "  Freeing host port $want held by a leftover container ($names) from a previous attempt..."
        docker stop "$id" >/dev/null 2>&1 || true
      fi
    done
  }
  # ntfy publishes host 8090, searxng publishes host 127.0.0.1:8888. Clear any stale holders of both.
  stop_container_on_host_port 8090
  stop_container_on_host_port 8888

  # Note the CPU architecture. On Apple Silicon (arm64) some images only ship amd64 and Docker runs
  # them under emulation, which can surface as a Rosetta error or a failed/unhealthy container. We
  # don't block on it, but if compose fails we steer to native ntfy (no Docker, no emulation at all).
  ARCH="$(uname -m 2>/dev/null || echo unknown)"
  if [[ "$ARCH" == "arm64" ]]; then
    echo "  (Apple Silicon detected. If a container needs an Intel image it runs under emulation; if"
    echo "   that errors, the native ntfy path below avoids Docker entirely and is the recommended route.)"
  fi
  # Give the local SearXNG instance a real secret on first run (the shipped settings.yml
  # has a placeholder; replace it so the instance isn't using a known key).
  if [[ -f configs/searxng/settings.yml ]] && grep -q "mongoose-local-searxng-change-me" configs/searxng/settings.yml; then
    SECRET="$(LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 48 || echo "$(date +%s)$(whoami)")"
    # portable in-place edit (macOS sed needs the empty backup arg)
    sed -i '' "s/mongoose-local-searxng-change-me/$SECRET/" configs/searxng/settings.yml 2>/dev/null || \
      sed -i "s/mongoose-local-searxng-change-me/$SECRET/" configs/searxng/settings.yml
  fi
  # ntfy (phone push) and searxng (local web search) both ship on by default and local.
  if docker compose up -d ntfy searxng; then
    mcli _install-step services done >/dev/null 2>&1 || true
  elif {
        # A first failure here is usually a port still held by a previous project's orphan. Tear this
        # project down (removing orphans), clear the host ports again, and retry ONCE before giving up.
        echo "  First container start failed (often a leftover container on the port). Cleaning up and retrying once..."
        docker compose down --remove-orphans >/dev/null 2>&1 || true
        stop_container_on_host_port 8090
        stop_container_on_host_port 8888
        docker compose up -d ntfy searxng
      }; then
    echo "  Containers started on the retry."
    mcli _install-step services done >/dev/null 2>&1 || true
  else
    echo "Could not start ntfy/searxng via Docker."
    if [[ "$ARCH" == "arm64" ]]; then
      echo "  On Apple Silicon this is often an image-architecture / Rosetta issue. The reliable fix that"
      echo "  needs NO Docker and NO emulation is native push:   mongoose setup-ntfy"
    else
      echo "  Docker may not be fully ready. You can retry, or use native push:   mongoose setup-ntfy"
    fi
    mcli _install-step services failed --detail "Docker was running but ntfy/SearXNG did not start (arch $ARCH); native 'mongoose setup-ntfy' avoids Docker" >/dev/null 2>&1 || true
  fi
else
  echo "Docker isn't running, so the bundled LOCAL ntfy (phone push) and SearXNG (web search) weren't started."
  echo "  These are optional, and phone push is NOT blocked. The easiest, most reliable way to get push"
  echo "  working without Docker is a native local ntfy, just run:   mongoose setup-ntfy"
  echo "  (It installs ntfy via Homebrew, runs it as a background service, and keeps content on your"
  echo "  network. Alternatively, start Docker and re-run this. A public ntfy server works too, but its"
  echo "  notification content would transit a third party.)"
  mcli _install-step services skipped --detail "Docker not running; run 'mongoose setup-ntfy' for native local push, or start Docker" >/dev/null 2>&1 || true
fi

# Show the owner-pairing secret in the terminal too (it's also on the local /install page). The first
# phone to pair becomes the OWNER; from anywhere other than this Mac it must enter this secret, so a
# guest or random network device can't grab the owner slot.
OWNER_SECRET="$(mongoose_curl "/install/owner-secret" | sed -n 's/.*"secret" *: *"\([^"]*\)".*/\1/p')"
if [ -n "$OWNER_SECRET" ]; then
  echo ""
  echo "  ┌─────────────────────────────────────────────┐"
  echo "  │  Owner pairing secret:  $OWNER_SECRET                 │"
  echo "  │  The first phone to pair (yours) uses this.   │"
  echo "  │  Guests are added later from the cockpit.     │"
  echo "  └─────────────────────────────────────────────┘"
  echo ""
fi

# Now that the server is up and the live /install page is open in the browser, pull the role models ,
# the genuinely long step. Doing it HERE (not before the server) means the user actually watches "the
# local brain" download in real time, instead of the page opening with everything already ticked.
if command -v ollama >/dev/null 2>&1; then
  mcli _install-step models running --detail "Downloading the local brain, this can take a while on first run" >/dev/null 2>&1 || true
  echo "Pulling role models via scripts/pull_models.sh"
  if ./scripts/pull_models.sh; then
    mcli _install-step models done >/dev/null 2>&1 || true
  else
    echo "Model pull failed. Retry later with ./scripts/pull_models.sh"
    mcli _install-step models failed --detail "Model download failed; re-run ./scripts/pull_models.sh" >/dev/null 2>&1 || true
  fi
fi

mcli _install-step smoke running >/dev/null 2>&1 || true
if ! ./scripts/smoke_test.sh; then
  echo "Smoke test failed. Leaving the install in a failed state instead of pretending it succeeded."
  mcli _install-step smoke failed --detail "Smoke test failed; check data/logs/mongoose.err.log and scripts/smoke_test.sh output" >/dev/null 2>&1 || true
  mcli _install-step ready failed --detail "Smoke test failed; check data/logs/mongoose.err.log and scripts/smoke_test.sh output" >/dev/null 2>&1 || true
  exit 1
fi
# Readiness is now PROVEN. From here on, nothing may report the install as "failed before ready":
# the watchdog below is a best-effort protective extra, and its failure is non-fatal.
mcli _install-step smoke done >/dev/null 2>&1 || true

# Install the EXTERNAL server watchdog as a launchd agent (KeepAlive) so it's always running. It probes
# /health/live and restarts the server if its event loop ever wedges, the one failure the in-process
# watchdog can't recover. This is now a standard part of install (not opt-in): a household assistant
# should heal itself without the user noticing. Non-fatal if launchctl isn't available.
echo "Installing the server watchdog (auto-restarts Mongoose if it ever becomes unresponsive)..."
# The watchdog step is explicitly NON-FATAL: a household assistant that has already passed its smoke
# test must not be reported as a failed install just because the watchdog agent couldn't be installed.
# We capture the watchdog's own stdout/stderr to files (rather than discarding stderr), so if it fails
# the real reason is printed to the terminal and the log instead of vanishing, that opacity was part of
# the original misdiagnosis. `|| WD_RC=$?` keeps a non-zero exit from tripping `set -e -o pipefail`.
mcli _install-step watchdog running >/dev/null 2>&1 || true
WD_OUT="$(mktemp -t mongoose-wd-out.XXXXXX 2>/dev/null || echo /tmp/mongoose-wd-out.$$)"
WD_ERR="$(mktemp -t mongoose-wd-err.XXXXXX 2>/dev/null || echo /tmp/mongoose-wd-err.$$)"
WD_RC=0
.venv/bin/mongoose server-watchdog --install >"$WD_OUT" 2>"$WD_ERR" || WD_RC=$?
cat "$WD_OUT" 2>/dev/null || true
WD_PLIST="$( sed -n 's/^Installed the server watchdog launch agent: //p' "$WD_OUT" 2>/dev/null | head -1 )"
WD_OK=0
WD_REASON=""
if [[ "$WD_RC" -eq 0 && -n "${WD_PLIST:-}" ]] && command -v launchctl >/dev/null 2>&1; then
  _uid="$(id -u)"
  launchctl bootout "gui/${_uid}/com.mongoose.server-watchdog" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/${_uid}" "$WD_PLIST" >/dev/null 2>&1; then
    echo "  Server watchdog is active (com.mongoose.server-watchdog)."
    WD_OK=1
  else
    echo "  Watchdog agent written to $WD_PLIST; load it with: launchctl bootstrap gui/${_uid} \"$WD_PLIST\""
    WD_REASON="agent written but launchctl bootstrap did not load it"
  fi
else
  WD_REASON="launchctl unavailable or the watchdog agent could not be created"
fi
if [[ "$WD_OK" == "1" ]]; then
  mcli _install-step watchdog done >/dev/null 2>&1 || true
else
  # Non-fatal: surface the real error (the captured stderr tail) so the user and the log see WHY, then
  # record it for the wrapper. We do NOT fail the install: readiness was already proven by the smoke test.
  if [[ "$WD_RC" -ne 0 ]]; then
    echo "  Watchdog install exited with status $WD_RC. Mongoose is installed and running; the auto-restart watchdog could not be installed:"
    sed 's/^/    /' "$WD_ERR" 2>/dev/null | tail -20 || true
  else
    echo "  Mongoose is installed and running, but the auto-restart watchdog could not be installed ($WD_REASON)."
  fi
  mcli _install-step watchdog failed --detail "${WD_REASON:-watchdog install exited $WD_RC}" >/dev/null 2>&1 || true
fi
rm -f "$WD_OUT" "$WD_ERR" 2>/dev/null || true

# Done. Mark ready (INSTALL_COMPLETE). The /install page is already open in the browser (we opened it
# right after the server came up, so the model download was visible live). Readiness was proven at the
# smoke step; the watchdog outcome above does not change that.
mcli _install-step ready done >/dev/null 2>&1 || true
echo ""
echo "Mongoose is ready. The install screen (with your phone pairing code) is open in your browser."
echo "If it didn't open: $(mongoose_local_url /install)"
if [[ "$(mongoose_probe_scheme)" == "https" ]]; then
  echo "Safari may ask you to trust this Mac's local certificate the first time (and again after an"
  echo "update changes it): choose Show Details, then 'visit this website'. That is expected and safe here."
  echo ""
  RED_BOLD="$(printf '\033[1;31m')"; RESET="$(printf '\033[0m')"
  # True terminal blinking (\e[5m) is unreliable and often deliberately disabled in modern
  # terminals, including Terminal.app, for accessibility reasons, so bold red is the honest
  # equivalent here rather than a promise this can't reliably keep. Apple also provides no
  # scriptable/AppleScript way to clear a single site's stored browser data from a script, that
  # gate is deliberate on Apple's part, a script silently wiping browser storage would itself be
  # a privacy problem, so this cannot be automated further than the one-time step below.
  echo "${RED_BOLD}IF Safari ever shows a stuck \"Mongoose is offline\" page that a normal reload never${RESET}"
  echo "${RED_BOLD}clears (Private Browsing or Chrome work fine, only regular Safari does not): a leftover${RESET}"
  echo "${RED_BOLD}service worker from BEFORE THIS VERSION is the cause, not the server. One-time fix,${RESET}"
  echo "${RED_BOLD}per Mac, per Safari profile:${RESET}"
  echo "${RED_BOLD}  Safari > Settings > Privacy > Manage Website Data > search \"localhost\" > Remove.${RESET}"
  echo "${RED_BOLD}This version already sends a header that clears this automatically the moment any${RESET}"
  echo "${RED_BOLD}request reaches the real server; this note is only the manual fallback if a worker is${RESET}"
  echo "${RED_BOLD}intercepting requests before that header can ever arrive.${RESET}"
fi
