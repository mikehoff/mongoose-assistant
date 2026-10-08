#!/usr/bin/env bash
# ── Trust Mongoose ──────────────────────────────────────────────────────────────
# Double-click this ONCE if macOS says the other Mongoose files are from an
# "unidentified developer" and won't open.
#
# macOS quarantines files that came inside a downloaded zip. This removes that
# quarantine flag from every Mongoose file in this folder, so the Start / Stop /
# Status / Install buttons open normally.
#
# (This file itself may still ask once , click Open. We can't sign these without a
# paid Apple Developer ID, which a downloaded build doesn't include.)
# ────────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"
if command -v xattr >/dev/null 2>&1; then
  echo "Removing the macOS quarantine flag from the Mongoose files in:"
  echo "  $(pwd)"
  xattr -dr com.apple.quarantine . >/dev/null 2>&1 || true
  echo "Done. The Start / Stop / Status / Install Mongoose files should now open without a warning."
else
  echo "xattr isn't available; nothing to do."
fi
echo ""
echo "You can close this window."
