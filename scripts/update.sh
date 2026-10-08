#!/usr/bin/env bash
# Mongoose updater. Updates an existing install to a new version while
# preserving local state (.env, data/, secrets/), auto-merging any NEW config
# keys the new version introduces, and supporting rollback.
#
# Usage:
#   ./scripts/update.sh <new-version-folder-or-zip>   # update
#   ./scripts/update.sh --rollback                    # restore newest backup
#   ./scripts/update.sh --list-backups                # show backups
# Run from your current mongoose install directory (legacy mongoose-alpha installs also work).

set -euo pipefail

safe_unzip() {
  python3 - "$1" "$2" <<'EOFPY'
import stat, sys, zipfile
from pathlib import PurePosixPath
zip_path, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as z:
    for info in z.infolist():
        name = info.filename
        p = PurePosixPath(name)
        if not name or name.startswith('/') or name.startswith('\\') or p.is_absolute() or '..' in p.parts:
            raise SystemExit(f"unsafe zip member path: {name!r}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise SystemExit(f"unsafe zip member symlink: {name!r}")
    z.extractall(dest)
EOFPY
}


backups_glob() { ls -dt ../mongoose-backup-* 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# LIVE-SERVER HANDOVER. An update that swaps files while the old process keeps
# serving old code from memory has not updated anything the user can see. Field
# post-mortem: updates were "applied" repeatedly while /health kept reporting the
# old version, because this script never stopped or started a server and the
# ps-string identity check could not even recognise the running one (Homebrew
# framework Python reports a bare interpreter path). Contract now: every update
# ENDS with the live port serving the NEW version, verified, or a loud failure.
# ---------------------------------------------------------------------------
_update_health() { # PORT -> body on stdout (https then http), rc 0 if any answered
  local port="$1" body
  for scheme in https http; do
    body="$(curl -fsSk --max-time 3 "$scheme://127.0.0.1:$port/health" 2>/dev/null || true)"
    if [ -n "$body" ]; then printf '%s' "$body"; return 0; fi
  done
  return 1
}
_update_port_is_mongoose() { _update_health "$1" | grep -q '"name":"mongoose"'; }
_update_live_version() { _update_health "$1" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p'; return 0; }
_update_pids_on_port() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"$1" -sTCP:LISTEN 2>/dev/null; return
  fi
  netstat -anv -p tcp 2>/dev/null | awk -v p=".$1" '$0 ~ /LISTEN/ && ($4 ~ p"$") {print $9}' \
    | grep -E '^[0-9]+$' | sort -u
}
_update_cmd_is_mongoose() {
  local low; low="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$low" in *mongoose.app:app*) case "$low" in *uvicorn*) return 0;; esac ;; esac
  case "$low" in *mongoose*" serve"*) return 0 ;; esac
  return 1
}
mongoose_teardown_port() { # PORT PYBIN -> rc 0 iff port free afterwards
  local port="$1" pybin="$2" pid cmd port_is_ours=0
  _update_pids_on_port "$port" | grep -q . || return 0
  # Pause the external watchdog so it doesn't respawn the OLD server mid-handover; the new
  # serve lifts the pause when it starts (and the watchdog self-resumes on a timer as backstop).
  "$pybin" -c "import sys; sys.path.insert(0,'src')
from mongoose.services.server_watchdog import pause
pause('update.sh handover')" >/dev/null 2>&1 || true
  # The pause above writes into THIS install's data dir, but a live incident proved the
  # machine can carry stray watchdogs spawned from OTHER install folders (Downloads/mongoose 10
  # vs 12), each reading its own relative data/ and therefore never seeing the pause. Those
  # zombies resurrect the OLD server the moment teardown frees the port, and the new server
  # then fights a respawn loop it cannot win while the installer sits at "configuring" and
  # /install spins forever. Kill every server-watchdog PROCESS on the machine, whatever
  # folder it came from; this is safe because the new serve spawns its own fresh watchdog.
  pkill -f "mongoose.cli server-watchdog" 2>/dev/null || true
  pkill -f "mongoose server-watchdog" 2>/dev/null || true
  sleep 0.3
  # Boot out any mongoose launch agents so nothing respawns what we stop.
  if command -v launchctl >/dev/null 2>&1; then
    local uid; uid="$(id -u)"
    launchctl list 2>/dev/null | awk '/[mM]ongoose/{print $3}' | while read -r lbl; do
      [ -n "$lbl" ] && launchctl bootout "gui/$uid/$lbl" 2>/dev/null || true
    done
  fi
  _update_port_is_mongoose "$port" && port_is_ours=1
  "$pybin" -m mongoose.cli stop >/dev/null 2>&1 || true
  for _ in $(seq 1 12); do _update_pids_on_port "$port" | grep -q . || return 0; sleep 0.25; done
  # Identity kill: the port answering /health as mongoose IS the ownership proof for every pid
  # bound to it (a foreign process never answers that); ps command-line match kept as a second
  # route for a wedged server that no longer answers.
  for pid in $(_update_pids_on_port "$port"); do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    if [ "$port_is_ours" = "1" ] || _update_cmd_is_mongoose "$cmd"; then
      echo "  Stopping the running Mongoose server (PID $pid) so the new version can take the port."
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  for _ in $(seq 1 12); do _update_pids_on_port "$port" | grep -q . || return 0; sleep 0.25; done
  _update_pids_on_port "$port" | grep -q . && return 1 || return 0
}
mongoose_start_and_verify() { # PORT EXPECTED_VERSION PYBIN -> rc 0 iff live /health reports EXPECTED
  local port="$1" expected="$2" pybin="$3" live retried=0 stale_seen=0
  mkdir -p data/logs
  echo "  Starting Mongoose ${expected}..."
  nohup "$pybin" -m mongoose.cli serve --port "$port" \
    >> data/logs/mongoose.out.log 2>> data/logs/mongoose.err.log &
  for i in $(seq 1 90); do
    live="$(_update_live_version "$port" || true)"
    if [ "$live" = "$expected" ]; then
      echo "  Live server verified: version $live answering on port $port."
      return 0
    fi
    if [ -n "$live" ] && [ "$retried" = "0" ]; then
      # Something answered with the WRONG version: an old server won the port race.
      echo "  Port $port answered as version $live (expected $expected); clearing it and retrying once."
      retried=1
      mongoose_teardown_port "$port" "$pybin" || true
      nohup "$pybin" -m mongoose.cli serve --port "$port" \
        >> data/logs/mongoose.out.log 2>> data/logs/mongoose.err.log &
    fi
    sleep 1
  done
  echo "UPDATE FAILED VERIFICATION: nothing on port $port is serving version $expected."
  echo "---- what is on the port ----"
  if command -v lsof >/dev/null 2>&1; then lsof -n -P -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || echo "  (nothing)"; fi
  echo "---- live /health says ----"
  _update_health "$port" || echo "  (no answer)"
  echo "---- watchdog processes (a stray one from ANOTHER install folder resurrects the old server) ----"
  pgrep -fl "server-watchdog" 2>/dev/null || echo "  (none)"
  echo "---- last server errors ----"
  tail -n 25 data/logs/mongoose.err.log 2>/dev/null || echo "  (no err log)"
  return 1
}
# Allow tests to source just the functions above.
if [[ "${UPDATE_SH_LIB_ONLY:-0}" == "1" ]]; then return 0 2>/dev/null || exit 0; fi


if [[ "${1:-}" == "--list-backups" ]]; then
  echo "Available backups (newest first):"; backups_glob; exit 0
fi

if [[ "${1:-}" == "--rollback" ]]; then
  NEWEST="$(backups_glob | head -1)"
  if [[ -z "$NEWEST" ]]; then echo "No backups found to roll back to."; exit 1; fi
  echo "Rolling back to: $NEWEST"
  read -r -p "This replaces the current code with the backup. Your current .env/data are also in that backup. Continue? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
  PRE="../mongoose-prerollback-$(date +%Y%m%d-%H%M%S)"
  cp -R . "$PRE"
  echo "Saved current state to $PRE first."
  # Restore code + state from backup, in place.
  rsync -a --delete --exclude='.git' "$NEWEST"/ ./ 2>/dev/null || cp -Rf "$NEWEST"/. ./
  if command -v xattr >/dev/null 2>&1; then
    xattr -dr com.apple.quarantine . >/dev/null 2>&1 || true
  fi
  echo "Rolled back. Current state was saved at $PRE if you need it."
  RB_VER="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2 || echo unknown)"
  RB_PORT="${MONGOOSE_PORT:-$(sed -n 's/^MONGOOSE_PORT=\(.*\)$/\1/p' .env 2>/dev/null | head -1)}"
  RB_PORT="${RB_PORT:-8765}"
  if [[ -x ".venv/bin/python" ]]; then RB_PY="./.venv/bin/python"; else RB_PY="python3"; export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"; fi
  echo "Handing port $RB_PORT to the rolled-back version..."
  mongoose_teardown_port "$RB_PORT" "$RB_PY" || true
  mongoose_start_and_verify "$RB_PORT" "$RB_VER" "$RB_PY" || exit 1
  exit 0
fi

SRC="${1:-}"
if [[ -z "$SRC" ]]; then
  echo "Usage: ./scripts/update.sh <new-version-folder-or-zip> | --rollback | --list-backups"
  exit 1
fi

if [[ ! -f "pyproject.toml" ]] || [[ ! -d "src/mongoose" ]]; then
  echo "This doesn't look like a Mongoose install. cd into your install first."
  exit 1
fi

CUR_VER="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2 || echo unknown)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="../mongoose-backup-${CUR_VER}-${STAMP}"

echo "Current version: ${CUR_VER}"
echo "Backing up entire install to: ${BACKUP}"
cp -R . "$BACKUP"

WORK="$(mktemp -d)"
if [[ "$SRC" == *.zip ]]; then
  safe_unzip "$SRC" "$WORK"
  NEW="$(find "$WORK" -maxdepth 2 -name pyproject.toml -exec dirname {} \; | head -1)"
else
  NEW="$SRC"
fi
if [[ -z "${NEW:-}" ]] || [[ ! -f "$NEW/pyproject.toml" ]]; then
  echo "Could not find the new version's pyproject.toml. Aborting; install untouched."
  rm -rf "$WORK"; exit 1
fi
NEW_VER="$(grep -m1 '^version' "$NEW/pyproject.toml" | cut -d'"' -f2 || echo unknown)"
echo "New version: ${NEW_VER}"

echo "Updating code (src, scripts, docs, configs templates, pyproject, requirements, .env.example)..."
for item in src scripts docs configs pyproject.toml requirements.txt README.md Makefile Dockerfile docker-compose.yml .env.example; do
  if [[ -e "$NEW/$item" ]]; then
    rm -rf "./$item"; cp -R "$NEW/$item" "./$item"
  fi
done
echo "Preserved: .env, data/, secrets/ (your config, database, and memory)."

# De-quarantine the freshly-copied files. If the update zip was downloaded by Safari/Mail/AirDrop
# it carries com.apple.quarantine, and cp preserves that flag, which makes macOS force a Gatekeeper
# re-approval of the .command wrappers in System Settings after the update. The installer already
# does this; doing it here too means an update always hands back a trusted tree, so "it just worked"
# is the consistent outcome rather than a coin-flip on how the zip happened to arrive.
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine . >/dev/null 2>&1 || true
fi

if [[ -d ".venv" ]]; then
  echo "Updating dependencies in .venv..."
  ./.venv/bin/pip install -q -r requirements.txt || echo "pip install had warnings."
  ./.venv/bin/pip install -q -e . || echo "editable install had warnings."
  # Fuzzy-matching model: part of the normal update, never fatal, skipped when present
  # (or when MONGOOSE_AUTO_EMBEDDINGS=0). Startup also self-heals this in the background.
  ./.venv/bin/python -c "from mongoose.services.embeddings import ensure_model; ensure_model()" || echo "embeddings model fetch skipped (will retry at startup)."
  RUN=("./.venv/bin/python" "-m" "mongoose.cli")
else
  export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
  RUN=("python3" "-m" "mongoose.cli")
fi

# Auto-merge any NEW config keys the new version added to .env.example into the
# user's live .env, without overwriting existing values.
echo "Merging any new config keys into .env..."
"${RUN[@]}" merge-env || echo "env merge step reported an issue; check .env manually."

echo "Running database migration (idempotent)..."
"${RUN[@]}" init-db || echo "init-db reported an issue; check manually."

echo "Verifying..."
if "${RUN[@]}" doctor >/dev/null 2>&1; then echo "doctor ran OK."; else echo "doctor reported issues; run 'mongoose doctor'."; fi

# LIVE HANDOVER: files on disk are the new version; now make the RUNNING SERVER the new version,
# and prove it. Without this, the old process keeps serving old code from memory and the update
# is a lie the moment anyone opens the browser.
UPD_PORT="${MONGOOSE_PORT:-$(sed -n 's/^MONGOOSE_PORT=\(.*\)$/\1/p' .env 2>/dev/null | head -1)}"
UPD_PORT="${UPD_PORT:-8765}"
UPD_PY="${RUN[0]}"
echo "Handing port $UPD_PORT over to version ${NEW_VER}..."
if ! mongoose_teardown_port "$UPD_PORT" "$UPD_PY"; then
  echo "Could not free port $UPD_PORT (a non-Mongoose process may hold it):"
  if command -v lsof >/dev/null 2>&1; then lsof -n -P -iTCP:"$UPD_PORT" -sTCP:LISTEN 2>/dev/null; fi
  exit 1
fi
mongoose_start_and_verify "$UPD_PORT" "$NEW_VER" "$UPD_PY" || exit 1

# Voice toolchain hint: don't auto-install brew packages on an update, but tell
# the user exactly how if the mic deps are missing.
if ! { command -v rec >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1; }; then
  echo "Note: voice mic input needs sox + ffmpeg + whisper. Run 'mongoose setup-voice' to install them."
fi

# Config-vs-reality reconciliation: an update must never quietly leave a role pointing at a model that
# isn't installed, or a feature enabled without its dependency. Surface high-severity drift right here.
echo "Checking config against the machine..."
PYTHONPATH=src "$UPD_PY" -c "
from mongoose.services.config_reconcile import reconcile
r = reconcile()
highs = [i for i in r['issues'] if i['severity'] == 'high']
if highs:
    for i in highs:
        print('  ! ' + i['detail'] + '  -> fix: ' + i['fix'])
    print('  ^ config drift detected (this is what used to survive updates silently). See /cockpit/config-health.')
else:
    print('  OK: configured models/features match the machine.')
" 2>/dev/null || echo "  (config check skipped)"

rm -rf "$WORK"
echo ""
echo "Updated ${CUR_VER} -> ${NEW_VER}. The live server on port ${UPD_PORT} has been verified as ${NEW_VER}."
echo "Backup: ${BACKUP}"
echo "Roll back any time with:  ./scripts/update.sh --rollback"
