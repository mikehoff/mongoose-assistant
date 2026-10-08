#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python3 -m compileall -q src tests

# Lint: fail the QA run on any pyflakes finding (unused imports/names, etc.).
# pyflakes is mandatory: a fresh install gets it via requirements.txt.
if ! python3 -m pyflakes --version >/dev/null 2>&1; then
  echo "ERROR: pyflakes is not installed (it is in requirements.txt). Run: pip install pyflakes"
  exit 1
fi
echo "Running pyflakes..."
python3 -m pyflakes src tests

# The per-test timeout backstop (pyproject: timeout=60) only works if pytest-timeout is
# installed. Without it a hung test silently hangs the whole gate (the audit's P0.1 failure
# mode). Verify it's present and fail loudly with the fix if not, rather than running an
# unprotected gate that can hang.
if ! python3 -c "import pytest_timeout" >/dev/null 2>&1; then
  echo "ERROR: pytest-timeout is not installed, so the hang backstop is INERT."
  echo "       A hung test would hang this entire gate. Install it (it is in the 'test' extra):"
  echo "       pip install pytest-timeout    (or: pip install -e '.[test]')"
  exit 1
fi

# Core tier: everything EXCEPT connector-marked tests (those need optional libs and
# run in scripts/qa_connectors.sh). Deselecting keeps core QA fast and dependency-free.
PYTHONPATH=src python3 -m pytest -q -m "not connectors"

echo
echo "Core QA passed."
echo "Connector QA not run here. Run ./scripts/qa_connectors.sh before claiming"
echo "Gmail/Microsoft attachment support is signed off."
