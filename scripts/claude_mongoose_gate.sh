#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data/logs
LOG="data/logs/claude-code-release-gate.last.log"
exec > >(tee "$LOG") 2>&1

echo "== Mongoose Claude Code release gate =="
echo "Root: $ROOT"
echo "Started: $(date)"

if [[ ! -f pyproject.toml || ! -d src/mongoose ]]; then
  echo "ERROR: not at a Mongoose project root."
  exit 2
fi

# Preflight (root cause of the 2026-07-05 incident): the targeted tests can spawn a real
# `mongoose serve` that races for port 8765 and hijacked the LIVE household server onto a pytest
# temp DB. Refuse to run while a live server is listening, unless explicitly forced.
GATE_PORT="${MONGOOSE_PORT:-8765}"
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$GATE_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if [[ "${MONGOOSE_GATE_FORCE:-0}" != "1" ]]; then
    echo "ERROR: a server is LIVE on port $GATE_PORT. The gate's tests can spawn servers that would"
    echo "hijack it (this happened on 2026-07-05, pointing the live server at a pytest temp DB)."
    echo "Stop the live server first, or run on a spare machine. To override (NOT on the real Mac):"
    echo "  MONGOOSE_GATE_FORCE=1 scripts/claude_mongoose_gate.sh"
    exit 2
  fi
  echo "WARNING: MONGOOSE_GATE_FORCE=1 set; running the gate while a server is live on $GATE_PORT."
fi

# Defense-in-depth (register T6): even if a test forgets its own isolation, everything this
# gate runs inherits an ISOLATED temp brain, an ephemeral port, and the foreign-brain allow
# flag (the serve guard would otherwise rightly refuse temp brains). A leaked server can
# therefore never sit on 8765 or touch the household brain.
GATE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/mongoose-gate.XXXXXX")"
export MONGOOSE_DB_PATH="$GATE_TMP/gate.sqlite3"
export MONGOOSE_DATA_DIR="$GATE_TMP/data"
export MONGOOSE_ALLOW_FOREIGN_BRAIN=1
export MONGOOSE_PORT="$(python3 - <<'PYEOF'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PYEOF
)"
LIVE_8765_BEFORE="$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null | sort || true)"

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x .venv/bin/python ]]; then
    PY=".venv/bin/python"
  elif command -v python3.12 >/dev/null 2>&1; then
    PY="$(command -v python3.12)"
  else
    PY="$(command -v python3 || true)"
  fi
fi

if [[ -z "$PY" ]]; then
  echo "ERROR: no Python found."
  exit 2
fi

echo "Python: $($PY -c 'import sys; print(sys.executable, sys.version.replace(chr(10), " "))')"

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "WARNING: ANTHROPIC_API_KEY is set in this shell; interactive Claude Code may not use subscription billing."
fi

$PY - <<'PYCODE'
import json, pathlib, sys
for p in [pathlib.Path('.claude/settings.json'), pathlib.Path('.claude/settings.local.json')]:
    if p.exists():
        try:
            json.loads(p.read_text())
        except Exception as e:
            raise SystemExit(f"Invalid JSON in {p}: {e}")
print('Claude settings JSON: OK')
PYCODE

if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
  echo "ERROR: pytest is not installed in this interpreter."
  echo "Run: $PY -m pip install -e '.[test,devices]'"
  exit 2
fi

echo "Compile gate..."
"$PY" -m compileall -q src tests

if "$PY" -c 'import pyflakes' >/dev/null 2>&1; then
  echo "Pyflakes gate..."
  "$PY" -m pyflakes src
else
  echo "Pyflakes not installed; skipping. Install with: $PY -m pip install -e '.[test]'"
fi

candidate_tests=(
  tests/test_bootstrap_claude_code_and_recovery_note.py
  tests/test_claude_code_integration.py
  tests/test_cli_install_release_gate.py
  tests/test_cockpit_js_parses.py
  tests/test_google_split_and_install_reset.py
  tests/test_install_and_ntfy_fixes.py
  tests/test_install_coherence.py
  tests/test_install_flow_integrity.py
  tests/test_install_no_false_success.py
  tests/test_install_phase_state_machine.py
  tests/test_install_preloader_handoff.py
  tests/test_install_progress.py
  tests/test_install_progress_weighting.py
  tests/test_install_sequence.py
  tests/test_install_terminal_browser_handoff.py
  tests/test_installer_shell_symbols.py
  tests/test_ddns_full_route_and_pwa_handoff.py
  tests/test_pwa_install.py
)
existing_tests=()
for t in "${candidate_tests[@]}"; do
  [[ -f "$t" ]] && existing_tests+=("$t")
done

if [[ "${#existing_tests[@]}" -eq 0 ]]; then
  echo "No targeted tests found; running full pytest as fallback."
  "$PY" -m pytest -q
else
  echo "Targeted installer/browser/Claude Code gate (${#existing_tests[@]} files)..."
  "$PY" -m pytest -q "${existing_tests[@]}"
fi

echo "== PASS: Mongoose Claude Code release gate =="
# Postflight (register T6): the 2026-07-05 incident was a leaked test server occupying
# 8765. If ANY new listener appeared on 8765 during this run, it was leaked by the gate:
# kill it and FAIL loudly rather than leave the household port hijacked.
LIVE_8765_AFTER="$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null | sort || true)"
LEAKED="$(comm -13 <(echo "$LIVE_8765_BEFORE") <(echo "$LIVE_8765_AFTER") | tr -d ' ' || true)"
if [[ -n "$LEAKED" ]]; then
  echo "GATE FAILURE: a test leaked a server onto port 8765 (pids: $LEAKED). Killing it."
  echo "$LEAKED" | xargs -r kill -9
  exit 3
fi
rm -rf "$GATE_TMP" 2>/dev/null || true
echo "Log: $LOG"
