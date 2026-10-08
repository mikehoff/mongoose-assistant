#!/usr/bin/env bash
# ============================================================================
# Mongoose on-Mac validation pass. Run this top-to-bottom ON THE MAC.
#
# Purpose: make the 1.0-alpha (and 1.1-beta) release pass MECHANICAL, not
# archaeology. The whole stack is verified as code in CI; what cannot be
# verified anywhere except the real machine is: Ollama residency, live sync,
# real Home Assistant, the Docker containers, the .app, HTTPS mic, a real phone,
# and, the qualitative bar, whether the assistant actually FEELS right over a
# day of real data.
#
# This script AUTO-CHECKS everything a script honestly can, and clearly PROMPTS
# for the human-judgment items rather than pretending they're automatable.
#
# Usage:
#   bash scripts/validate_on_mac.sh            # alpha pass (default)
#   bash scripts/validate_on_mac.sh --beta     # alpha pass + beta connector pass
#   bash scripts/validate_on_mac.sh --soak     # include the full-day soak steps
#
# It is READ-ONLY about your data. It never sends mail, never purchases, never
# deletes. The one thing it starts is the local server/containers (which is the
# point). Re-runnable.
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

BETA=0; SOAK=0
for a in "$@"; do
  case "$a" in
    --beta) BETA=1 ;;
    --soak) SOAK=1 ;;
  esac
done

PORT="${MONGOOSE_PORT:-8765}"
# shellcheck source=scripts/mongoose_local_probe.sh
. "$(dirname "$0")/mongoose_local_probe.sh"
PASS=0; FAIL=0; MANUAL=0; SKIP=0
RESULTS=()

c_green=$'\033[32m'; c_red=$'\033[31m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

section() { echo; echo "${c_dim}========== $1 ==========${c_off}"; }

# auto_check NAME COMMAND... , runs the command, PASS if exit 0
auto_check() {
  local name="$1"; shift
  printf "  [ ] %s ... " "$name"
  if "$@" >/tmp/mongoose_val.out 2>&1; then
    printf "\r  ${c_green}[PASS]${c_off} %s\n" "$name"; PASS=$((PASS+1)); RESULTS+=("PASS  $name")
  else
    printf "\r  ${c_red}[FAIL]${c_off} %s\n" "$name"; FAIL=$((FAIL+1)); RESULTS+=("FAIL  $name")
    echo "${c_dim}        -> $(tail -n 2 /tmp/mongoose_val.out | tr '\n' ' ')${c_off}"
  fi
}

# auto_grep NAME PATTERN COMMAND... , PASS if command output matches PATTERN
auto_grep() {
  local name="$1"; local pat="$2"; shift 2
  printf "  [ ] %s ... " "$name"
  if "$@" 2>/dev/null | grep -qiE "$pat"; then
    printf "\r  ${c_green}[PASS]${c_off} %s\n" "$name"; PASS=$((PASS+1)); RESULTS+=("PASS  $name")
  else
    printf "\r  ${c_red}[FAIL]${c_off} %s\n" "$name"; FAIL=$((FAIL+1)); RESULTS+=("FAIL  $name")
  fi
}

# manual NAME PROMPT , a human-judgment check; records as MANUAL (you confirm)
manual() {
  local name="$1"; local prompt="$2"
  echo "  ${c_yel}[MANUAL]${c_off} $name"
  echo "${c_dim}        $prompt${c_off}"
  MANUAL=$((MANUAL+1)); RESULTS+=("MANUAL $name, $prompt")
}

have() { command -v "$1" >/dev/null 2>&1; }

# Resolve how to invoke the CLI: prefer the installed module in the venv; fall back to
# running from the source checkout (PYTHONPATH=src) so the checklist works whether
# Mongoose is pip-installed or run from a clone.
if python3 -c "import mongoose" >/dev/null 2>&1; then
  _MCLI_PREFIX=""
elif [ -d src ] && PYTHONPATH=src python3 -c "import mongoose" >/dev/null 2>&1; then
  _MCLI_PREFIX="src"
else
  _MCLI_PREFIX=""   # will fail loudly in the gate, which is correct
fi
mcli() {
  if [ -n "$_MCLI_PREFIX" ]; then
    PYTHONPATH="$_MCLI_PREFIX" python3 -m mongoose.cli "$@"
  else
    python3 -m mongoose.cli "$@"
  fi
}
# Export so mcli is usable inside `bash -c "..."` subshells (the soak real-data check pipes
# its output, which needs a subshell). Without this, mcli is unknown in the subshell and the
# check fails with 'mcli: command not found', exactly the live-validation bug the audit found.
export _MCLI_PREFIX
export -f mcli 2>/dev/null || true

# ----------------------------------------------------------------------------
section "0. Environment + repo gate (must be green before anything else)"
# ----------------------------------------------------------------------------
auto_check "Python 3.12 present"            bash -c 'python3.12 --version || python3 --version | grep -q "3.12"'
auto_check "venv exists"                    test -d .venv
# activate venv for the rest if present
[ -d .venv ] && source .venv/bin/activate 2>/dev/null || true
auto_check "package imports + version"      python3 -c "import mongoose, sys; print(mongoose.__version__)"
auto_check "pyflakes clean (src+tests)"     python3 -m pyflakes src tests
auto_check "full test gate (qa_repo.sh)"    bash scripts/qa_repo.sh
auto_check "connector tier importable"      bash -c 'python3 -m pytest -m connectors --co -q >/dev/null 2>&1 || true'

# ----------------------------------------------------------------------------
section "1. Local brain (Ollama), the non-negotiable"
# ----------------------------------------------------------------------------
if have ollama; then
  auto_grep "Ollama API reachable"   "models|name|\\[\\]"  bash -c "curl -s http://127.0.0.1:11434/api/tags"
  manual "Chat model answers within memory" \
    "Run: mongoose chat 'in one sentence, what's on today?', it should answer from the LOCAL model, no cloud, within the 24GB budget (one model at a time). Confirm it doesn't swap/stall."
  manual "Role-based model selection works" \
    "Confirm the coder vs chat roles load the expected models (mongoose doctor shows model roles)."
else
  echo "  ${c_red}[FAIL]${c_off} Ollama not installed, the local brain is the whole point."
  FAIL=$((FAIL+1)); RESULTS+=("FAIL  Ollama installed")
fi
auto_grep "cloud escalation is OFF" "disabled|off"  mcli doctor

# ----------------------------------------------------------------------------
section "2. Containers (ntfy + local SearXNG)"
# ----------------------------------------------------------------------------
if have docker && docker info >/dev/null 2>&1; then
  auto_check "docker compose config valid"  docker compose config -q
  echo "  ${c_dim}    (starting ntfy + searxng if not up...)${c_off}"
  docker compose up -d ntfy searxng >/dev/null 2>&1 || true
  sleep 4
  auto_grep "ntfy container running"     "ntfy"     bash -c "docker compose ps --services --filter status=running"
  auto_grep "searxng container running"  "searxng"  bash -c "docker compose ps --services --filter status=running"
  auto_grep "local SearXNG answers JSON"  "results|query" bash -c "curl -s 'http://127.0.0.1:8888/search?q=test&format=json'"
else
  manual "Docker not running" "Start Docker Desktop, then re-run, ntfy (phone push) and SearXNG (web search) need it."
  SKIP=$((SKIP+2))
fi

# ----------------------------------------------------------------------------
section "3. Server + the alpha readiness verdict"
# ----------------------------------------------------------------------------
if ! mongoose_curl "/health" >/dev/null; then
  echo "  ${c_dim}    (starting server on 127.0.0.1:$PORT ...)${c_off}"
  mkdir -p data/logs
  # Use the SAME resolution as mcli (source-tree or installed) rather than the bare
  # 'mongoose' console script, which may be stale, the whole point of the mcli wrapper.
  if [ -n "$_MCLI_PREFIX" ]; then
    PYTHONPATH="$_MCLI_PREFIX" nohup python3 -m mongoose.cli serve --host 127.0.0.1 --port "$PORT" >> data/logs/validate.out 2>&1 &
  else
    nohup python3 -m mongoose.cli serve --host 127.0.0.1 --port "$PORT" >> data/logs/validate.out 2>&1 &
  fi
  for _ in $(seq 1 40); do mongoose_curl "/health" >/dev/null && break; sleep 0.5; done
fi
auto_grep "health endpoint returns status+db"  "status|db|ok"  bash -c 'source scripts/mongoose_local_probe.sh; mongoose_curl "/health"'
auto_check "doctor passes"                 mcli doctor
auto_check "alpha-check verdict"            mcli alpha-check
auto_check "data integrity (read-only)"    mcli verify-data

# ----------------------------------------------------------------------------
section "4. The .app front door + onboarding"
# ----------------------------------------------------------------------------
auto_check "Mongoose.app build script runs" bash scripts/make_macos_app.sh
manual "Double-click Mongoose.app -> lands on /welcome" \
  "Open Mongoose.app from Finder (not the terminal). It should start the server and open the guided first-run flow at /welcome."
manual "Onboarding completes end-to-end" \
  "Walk /welcome: brain step turns green, connect a source, reach 'Add your phone'. Steps should tick green from live state."

# ----------------------------------------------------------------------------
section "5. Phone pairing (real QR) + multi-surface"
# ----------------------------------------------------------------------------
auto_grep "pairing code mints"  "code|pair"  mcli devices pair
manual "Scan the QR with a real phone" \
  "On /welcome (or the Devices tab), scan the QR with your phone's camera. The page should advance once the phone connects, and the device should appear in the System Map surfaces."
manual "Device token is correctly scoped" \
  "From the phone, confirm it can reach /device/* but NOT the cockpit/setup, the scoping that protects you."

# ----------------------------------------------------------------------------
section "6. Home Assistant routing + private-data boundary"
# ----------------------------------------------------------------------------
auto_grep "HA configured/reachable" "ok|reachable|configured|version" mcli doctor
manual "HA-first routing against the live Pi" \
  "Issue a home command that maps to a real entity. Confirm it tries Home Assistant FIRST."
manual "Alexa fallback after HA failure" \
  "Force an HA-miss (entity HA can't serve) and confirm it falls back to the Alexa bridge where configured."
manual "Private content NOT spoken on shared speaker" \
  "Ask something private (email/calendar detail). Confirm it routes to a private surface and is NOT spoken aloud on a shared-room speaker. This is the 2.5 boundary, it must hold."

# ----------------------------------------------------------------------------
section "7. Voice over HTTPS"
# ----------------------------------------------------------------------------
manual "Browser mic works over HTTPS on the LAN" \
  "Run: mongoose serve --host 0.0.0.0 --tls, open the cockpit over https from another device, and confirm the mic/voice relay works (browsers require HTTPS for mic)."

# ----------------------------------------------------------------------------
section "8. The day-in-the-life soak (run with --soak)"
# ----------------------------------------------------------------------------
if [ "$SOAK" = "1" ]; then
  auto_check "scheduler service status"   mcli service status
  manual "Leave the scheduler ON for a full day" \
    "Let launchd run the daily brief, mail sync, and reindex on their real cadence for ~24h."
  manual "Daily brief actually generated" \
    "Next morning: mongoose daily, confirm the DOP produced a real, useful picture from REAL data, with true cross-domain links (not seeded)."
  manual "Surpriser earned its interruptions" \
    "Review: mongoose receipts, did the Surpriser surface things that were genuinely worth it, and stay quiet otherwise? This is the qualitative bar."
  # Real-data anti-noise verdict. Pin the hour to 14:00 so this tests SHAPE suppression,
  # not quiet hours (a night-time run would otherwise report a false 'healthy'). If the
  # real DB isn't present yet, record a manual prompt rather than failing cryptically.
  if [ -f data/mongoose.sqlite3 ]; then
    auto_check "real-data anti-noise verdict" \
      bash -c "mcli simulate-surpriser --days 14 --hour 14 --source-db data/mongoose.sqlite3 --no-seed | tail -3"
  else
    manual "real-data anti-noise verdict (no DB yet)" \
      "data/mongoose.sqlite3 not found. Once Mongoose has synced real data, run: mongoose simulate-surpriser --days 14 --hour 14 --source-db data/mongoose.sqlite3 --no-seed, and confirm the verdict is 'healthy' (not 'inconclusive')."
  fi
else
  manual "Day-in-the-life soak (skipped)" "Re-run with --soak to include the full-day scheduler soak + real-data Surpriser judgment."
fi

# ----------------------------------------------------------------------------
if [ "$BETA" = "1" ]; then
section "BETA. Live personal-data connectors (the least-validated surface)"
  manual "Google OAuth: consent + token refresh + revoke" \
    "Run: mongoose authorize google, complete real consent. Confirm refresh works next day, and revocation is honored."
  manual "Microsoft OAuth: consent + refresh + revoke" \
    "Run: mongoose authorize microsoft, same checks."
  auto_grep "mail sync status reports providers" "google|microsoft|imap|never|synced" mcli mail status
  manual "Real Gmail/Graph sync AT VOLUME" \
    "Sync a real account with thousands of messages: mongoose sync-gmail / sync-ms-mail. Watch pagination, rate limits, partial failures, and privacy classification on REAL messages, not a handful of seeded rows."
  manual "WhatsApp real link + draft->approve->send" \
    "Link real WhatsApp. Exercise the trust-sensitive loop: draft a reply, APPROVE it, confirm it sends; draft another, CANCEL, confirm it does NOT send. Check the receipts ledger says exactly what happened."
  manual "Surpriser judged GOOD on real correlations over DAYS" \
    "The real bar: over several days of real use, are the Surpriser's correlations genuinely good (not just 'not noisy' on a synthetic seed)? Only real use clears this."
  manual "Stability/perf under a real corpus" \
    "FTS over thousands of emails/files, DB growth, sync cadence not thrashing the machine. Confirm it stays responsive."
fi

# ----------------------------------------------------------------------------
section "RESULT"
# ----------------------------------------------------------------------------
echo
echo "  ${c_green}PASS:   $PASS${c_off}   ${c_red}FAIL: $FAIL${c_off}   ${c_yel}MANUAL (confirm yourself): $MANUAL${c_off}   skipped: $SKIP"
echo
if [ "$FAIL" -gt 0 ]; then
  echo "  ${c_red}NOT READY${c_off}: $FAIL automated check(s) failed. Fix these first:"
  for r in "${RESULTS[@]}"; do [[ "$r" == FAIL* ]] && echo "    - ${r#FAIL  }"; done
else
  echo "  ${c_green}All automated checks passed.${c_off}"
fi
echo
echo "  The MANUAL items are NOT optional, they are the things a script honestly cannot"
echo "  verify (real hardware, real data, and human judgment of whether it FEELS right)."
echo "  1.0-alpha is reached when: all automated checks pass AND every MANUAL item is"
echo "  confirmed by you. 1.1-beta additionally requires the --beta connector items."
echo
echo "  Full result log: the RESULTS above. Re-run any time; this is read-only about data."
