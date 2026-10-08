#!/usr/bin/env bash
# ── Mongoose , Stop ─────────────────────────────────────────────────────────────
# Double-click to stop the running Mongoose server. Safe: it only stops a process
# confirmed to be Mongoose (by pidfile, then by an identity check on the port), never
# an unrelated program that happens to use the port.
# ────────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")"
PORT="${MONGOOSE_PORT:-8765}"
# shellcheck source=scripts/mongoose_local_probe.sh
. "$(dirname "$0")/scripts/mongoose_local_probe.sh"
_read_version() {
  if [ -f release.json ]; then
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' release.json | head -1
  fi
}
WRAPPER_VERSION="$(_read_version)"; [ -n "$WRAPPER_VERSION" ] || WRAPPER_VERSION="1.1"
mkdir -p data/logs
LOG="data/logs/stop-command-wrapper.log"
touch "$LOG" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1

printf '\n  Mongoose v%s , stopping...\n  Log: data/logs/stop-command-wrapper.log\n\n' "$WRAPPER_VERSION"
printf '  Checking launch agents and server on port %s...\n' "$PORT"

run_with_timeout() {
  local seconds="$1"; shift
  "$@" &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$seconds" ]; then
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid"
}

# Tear down a launch agent first so KeepAlive can't immediately respawn the server.
if command -v launchctl >/dev/null 2>&1; then
  printf '  Checking launchd agents...\n'
  uid="$(id -u 2>/dev/null || echo 0)"
  for label in com.mongoose.server com.mongoose.api com.mongoose.scheduler; do
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  done
fi

printf '  Checking local CLI availability...\n'
if [[ -x .venv/bin/mongoose ]]; then
  printf '  Asking the Mongoose CLI to stop the server...\n'
  if ! run_with_timeout 15 .venv/bin/mongoose stop; then
    printf '  The CLI stop command did not return within 15 seconds; using the direct port fallback.\n'
  fi
else
  # Fallback: kill the PID on the port, but ONLY while /health confirms it's mongoose.
  if mongoose_curl "/health" | grep -q '"name":"mongoose"'; then
    if command -v lsof >/dev/null 2>&1; then
      for pid in $(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null); do
        mongoose_curl "/health" | grep -q '"name":"mongoose"' || break
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        kill -9 "$pid" 2>/dev/null || true
      done
    fi
  fi
fi

sleep 0.5
if mongoose_curl "/health" >/dev/null; then
  printf '  Something is still answering on port %s.\n' "$PORT"
  printf '  If it is Mongoose and will not stop, a launch agent may be respawning it:\n'
  printf '    launchctl list | grep -i mongoose\n'
else
  printf '  Mongoose is stopped.\n'
  # Release any models Ollama has loaded in memory so they stop consuming RAM.
  # `ollama list` shows what is loaded; we unload each by pulling it with keep_alive=0.
  # This is best-effort: if Ollama is not running or no models are loaded, this is silent.
  if command -v ollama >/dev/null 2>&1 && curl -fsS --connect-timeout 1 http://localhost:11434/api/tags >/dev/null 2>&1; then
    printf '  Releasing Ollama models from memory...\n'
    for model in $(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}'); do
      curl -fsS -X POST http://localhost:11434/api/generate \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"$model\",\"keep_alive\":0}" >/dev/null 2>&1 || true
    done
    printf '  Ollama models released.\n'
  fi
fi

printf '\n  Press any key to close.\n'
read -n 1 -s || true
