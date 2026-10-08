#!/usr/bin/env bash
# ── Mongoose , Start ────────────────────────────────────────────────────────────
# Double-click to start Mongoose if it is already set up. (For first-time setup,
# use 'Install Mongoose.command' instead , that builds the environment.)
# Opens the cockpit in your browser once the server is answering.
# ────────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")"
PORT="${MONGOOSE_PORT:-8765}"
# shellcheck source=scripts/mongoose_local_probe.sh
. "$(dirname "$0")/scripts/mongoose_local_probe.sh"

clear
printf '\n  Mongoose , starting...\n\n'

if [[ ! -x .venv/bin/mongoose ]]; then
  printf "  Mongoose is not set up yet (no .venv). Double-click 'Install Mongoose.command' first.\n"
  printf '\n  Press any key to close.\n'; read -n 1 -s; exit 1
fi

# Already running? Just open it, but only if the actual cockpit page renders.
if mongoose_curl "/health" | grep -q '"name":"mongoose"'; then
  if mongoose_page_ready "/ui" "Mongoose|Cockpit"; then
    printf '  Mongoose is already running. Opening the cockpit...\n'
    mongoose_open_local "/ui" >/dev/null
    printf '\n  Press any key to close.\n'; read -n 1 -s; exit 0
  else
    printf '  Mongoose answers /health, but the cockpit page did not load. Restarting it...\n'
  fi
fi

# Start Ollama if present (the local brain needs it).
if ! pgrep -x "Ollama" >/dev/null 2>&1 && ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  open -a Ollama >/dev/null 2>&1 || true
fi

mkdir -p data/logs
mkdir -p data/logs
# If the user has ALREADY enabled phone access, make sure this start binds to the LAN too , a restart
# must not silently drop a working phone setup back to loopback (the "QR is right but phone refused"
# trap). We do NOT enable LAN for users who never opted in; this only re-asserts an existing choice.
.venv/bin/python - <<'PY2' >/dev/null 2>&1 || true
import os
from mongoose.setup_wizard import effective_env
env = effective_env()
allow = str(env.get("MONGOOSE_ALLOW_REMOTE") or "").strip().lower() in ("1","true","yes","on")
host = (env.get("MONGOOSE_HOST") or "").strip()
if allow or host in ("0.0.0.0","::"):
    from mongoose.enable_phone import ensure_phone_access_defaults
    ensure_phone_access_defaults()
PY2
nohup .venv/bin/mongoose serve --port "$PORT" > data/logs/mongoose.out.log 2> data/logs/mongoose.err.log &

# Wait for it to answer.
started=0
for _ in $(seq 1 60); do
  if mongoose_curl "/health" >/dev/null; then started=1; break; fi
  sleep 0.5
done

if [[ "$started" -eq 1 ]]; then
  if mongoose_page_ready "/ui" "Mongoose|Cockpit"; then
    BASE="$(mongoose_local_base)"
    LAN_BASE="$(mongoose_human_base)"
    printf '  Mongoose is running. Owner URL: %s/ui\n' "$BASE"
    if [[ "$LAN_BASE" != "$BASE" ]]; then printf '  Phone/LAN URL after pairing: %s/device/launchpad\n' "$LAN_BASE"; fi
    mongoose_open_local "/ui" >/dev/null
  else
    printf '  Mongoose answers /health, but the cockpit page did not load. Last error log lines:\n\n'
    tail -n 20 data/logs/mongoose.err.log 2>/dev/null || printf '  (no error log)\n'
  fi
else
  printf '  Mongoose did not start. Last lines of the error log:\n\n'
  tail -n 20 data/logs/mongoose.err.log 2>/dev/null || printf '  (no error log)\n'
fi

printf '\n  Press any key to close.\n'
read -n 1 -s
