#!/usr/bin/env bash
# ── Mongoose , Status ───────────────────────────────────────────────────────────
# Double-click to check whether Mongoose is running, which version, and its address.
# Does not start or stop anything.
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
LOG="data/logs/status-command-wrapper.log"
touch "$LOG" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1

printf '\n  Mongoose v%s , status\n  Log: data/logs/status-command-wrapper.log\n\n' "$WRAPPER_VERSION"
printf '  Checking whether Mongoose is running on port %s...\n' "$PORT"

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

# Prefer the venv's CLI (authoritative: reads pidfile + settings); fall back to a plain curl.
if [[ -x .venv/bin/mongoose ]]; then
  printf '  Checking via the Mongoose CLI...\n'
  if run_with_timeout 12 .venv/bin/mongoose status; then
    rc=0
  else
    rc=$?
    if [[ "$rc" -eq 124 ]]; then
      printf '  The CLI status command did not return within 12 seconds; checking /health directly.\n'
      if mongoose_curl "/health" | grep -q '"name":"mongoose"'; then
        ver="$(mongoose_curl "/health" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
        BASE="$(mongoose_local_base)"
        LAN_BASE="$(mongoose_human_base)"
        printf '  RUNNING. Owner URL: %s/ui  (version %s)\n' "$BASE" "${ver:-?}"
        if [[ "$LAN_BASE" != "$BASE" ]]; then printf '  Phone/LAN URL after pairing: %s/device/launchpad\n' "$LAN_BASE"; fi
        rc=0
      else
        printf '  NOT running (nothing answering on port %s).\n' "$PORT"
        rc=1
      fi
    fi
  fi
else
  if mongoose_curl "/health" | grep -q '"name":"mongoose"'; then
    ver="$(mongoose_curl "/health" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
    BASE="$(mongoose_local_base)"
    LAN_BASE="$(mongoose_human_base)"
    printf '  RUNNING. Owner URL: %s/ui  (version %s)\n' "$BASE" "${ver:-?}"
    if [[ "$LAN_BASE" != "$BASE" ]]; then printf '  Phone/LAN URL after pairing: %s/device/launchpad\n' "$LAN_BASE"; fi
    rc=0
  else
    printf '  NOT running (nothing answering on port %s).\n' "$PORT"
    printf "  Start it by double-clicking  'Start Mongoose.command'\n"
    rc=1
  fi
fi

printf '\n  Press any key to close.\n'
read -n 1 -s || true
exit $rc
