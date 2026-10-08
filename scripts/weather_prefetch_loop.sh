#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${MONGOOSE_PORT:-8765}"
INTERVAL="${WEATHER_PREFETCH_INTERVAL_SECONDS:-300}"
# shellcheck source=scripts/mongoose_local_probe.sh
. "scripts/mongoose_local_probe.sh"

while true; do
  mongoose_curl "/weather/refresh" -X POST >/dev/null || true
  sleep "$INTERVAL"
done
