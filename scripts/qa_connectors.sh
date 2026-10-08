#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Connector QA: proves the Gmail/Microsoft attachment indexing actually works,
# using MOCKED API responses (no real Google/Microsoft account needed). These are
# kept out of the fast core QA because they need optional libraries; this script
# installs them into the ACTIVE interpreter first, then runs the tier.

PY="${PYTHON:-python3}"

echo "Installing connector dependencies into the active interpreter..."
"$PY" -m pip install --quiet google-api-python-client google-auth google-auth-oauthlib msal || {
  echo "ERROR: could not install connector dependencies."
  echo "Try: $PY -m pip install -e '.[connectors]'"
  exit 1
}

echo "Running connector contract tests (mocked API, no credentials needed)..."
PYTHONPATH=src "$PY" -m pytest -q -m connectors

echo
echo "Connector QA passed. Gmail/Microsoft attachment indexing verified against mocked API payloads."
