#!/usr/bin/env bash
set -euo pipefail
# Generate the daily brief and drop it on the bus as a message to you.
# Run on a schedule (launchd on macOS, below) so the brief is waiting when you
# wake up, no need to ask. This is the proactive half of Mongoose's USP.
cd "$(dirname "$0")/.."
# Use the project venv's python if present, else system python.
PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
PYTHONPATH=src "$PY" -m mongoose.cli brief --post
