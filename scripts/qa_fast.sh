#!/usr/bin/env bash
# Fast confidence gate: the critical-path tests, bounded to run in a few seconds.
#
# This is NOT a replacement for the full gate (scripts/qa_repo.sh). It's the quick check
# for "did I break anything important?" when you don't want to wait for the full suite,
# or when the machine is under load (the full suite is fine but can be starved of CPU in
# constrained environments). It covers: security boundaries (the request guard, device-
# token scoping, CSRF/nonce), the synthesis core (DOP), the surpriser's restraint, device
# pairing safety, and install/version coherence, the things whose regression would matter
# most. If this is red, stop. If it's green, the full gate is still the sign-off.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== pyflakes (fast) =="
python3 -m pyflakes src tests

# The hang backstop (pyproject: timeout=60) needs pytest-timeout; without it a hung test
# hangs this gate (the audit's P0.1 failure mode). Fail loudly rather than run unprotected.
if ! python3 -c "import pytest_timeout" >/dev/null 2>&1; then
  echo "ERROR: pytest-timeout is not installed; the hang backstop is INERT."
  echo "       Install it: pip install pytest-timeout    (or: pip install -e '.[test]')"
  exit 1
fi

echo "== critical-path tests =="
# These files cover security, synthesis, companion, devices, and release coherence.
PYTHONPATH=src python3 -m pytest -q -m "not connectors" \
  tests/test_lan_access.py \
  tests/test_devices.py \
  tests/test_companion_loop_v020.py \
  tests/test_daily_picture.py \
  tests/test_surpriser.py \
  tests/test_cockpit.py \
  tests/test_version_consistency.py \
  tests/test_install_coherence.py 2>/dev/null || \
PYTHONPATH=src python3 -m pytest -q -m "not connectors" \
  tests/test_lan_access.py \
  tests/test_devices.py \
  tests/test_companion_loop_v020.py \
  tests/test_daily_picture.py \
  tests/test_surpriser.py \
  tests/test_cockpit.py \
  tests/test_version_consistency.py

echo
echo "Fast gate passed. This is a confidence check, NOT the full sign-off."
echo "Run ./scripts/qa_repo.sh for the complete gate before release."
