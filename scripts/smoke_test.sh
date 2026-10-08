#!/usr/bin/env bash
set -euo pipefail

PORT="${MONGOOSE_PORT:-8765}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -n "${MONGOOSE_API_URL:-}" ]]; then
  BASE_URL="${MONGOOSE_API_URL%/}"
  smoke_curl() {
    local path="$1"; shift || true
    curl --connect-timeout 2 --max-time 8 -fsSk "$@" "${BASE_URL}${path}"
  }
else
  # shellcheck source=scripts/mongoose_local_probe.sh
  . "$SCRIPT_DIR/mongoose_local_probe.sh"
  BASE_URL="$(mongoose_probe_base || mongoose_preferred_base)"
  smoke_curl() {
    mongoose_curl "$@"
  }
fi

pass=0; fail=0
check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "  ok   $name"; pass=$((pass+1));
  else echo "  FAIL $name"; fail=$((fail+1)); fi
}

echo "Smoke testing Mongoose at $BASE_URL"

# Allow a freshly-started uvicorn process a moment to finish startup before counting every endpoint
# as failed. HTTPS is the default, so the probe accepts the local self-signed certificate and falls
# back to HTTP only when MONGOOSE_TLS=off.
ready=0
for _ in {1..50}; do
  if smoke_curl "/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.2
done
if [[ "$ready" -ne 1 ]]; then
  echo "  FAIL readiness: $BASE_URL/health did not respond"
  exit 1
fi

check "health"                 smoke_curl "/health"
# A live API is not enough; these catch the exact failure mode where /health works but the browser UI
# crashes or is reachable only via the wrong scheme.
check "install page"           smoke_curl "/install"
check "cockpit page"           smoke_curl "/ui"
check "persona (default)"      smoke_curl "/persona"
check "persona (admin mode)"   smoke_curl "/persona?mode=admin"
check "doctor"                 smoke_curl "/doctor"
check "system status"          smoke_curl "/system/status"
check "tools catalogue"        smoke_curl "/tools/catalogue"
check "weather voice-summary"  smoke_curl "/weather/voice-summary"
check "home routes"            smoke_curl "/home/routes"
check "calendar today"         smoke_curl "/calendar/today"
check "tasks today"            smoke_curl "/tasks/today"

# Ingest then search a notification round-trips through SQLite + FTS.
check "notification ingest" smoke_curl "/notifications/ingest" \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"source":"smoke","app":"test","title":"Smoke check","body":"hello","privacy":"low","urgency":"normal"}'
check "notification search" smoke_curl "/notifications/search?q=smoke"

# LLM chat only works if Ollama is up with the models pulled; treat as optional. Use a SHORT timeout:
# the first cold load of a large model into memory can take ~60s, and we must not make the installer
# appear to hang at 100% waiting for it. If it doesn't answer quickly, we just skip, the model still
# warms up in the background and works fine once loaded.
echo "Optional (needs Ollama + models pulled):"
if smoke_curl "/llm/chat" --max-time 8 -X POST -H 'content-type: application/json' \
     -d '{"prompt":"say hi in three words","role":"chat","persist":false}' >/dev/null 2>&1; then
  echo "  ok   llm chat"
else
  echo "  skip llm chat (Ollama still warming up or model not pulled; this is fine)"
fi

echo ""
echo "Smoke test: $pass passed, $fail failed."
[[ "$fail" -eq 0 ]]
