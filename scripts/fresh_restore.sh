#!/usr/bin/env bash
# Fresh restore from a verified zip. Use this when the installed `mongoose` command
# is itself suspect (stale/mixed source tree), so you do NOT ask the broken install
# to repair itself. Run with: zsh scripts/fresh_restore.sh <path-to-zip>
#
# This script is intentionally comment-light in the executable body and written to be
# pasted/run as a file (not line-by-line), because the post-mortem showed pasted '#'
# comments being executed as commands in interactive zsh.
set -euo pipefail

safe_unzip() {
  python3 - "$1" "$2" <<'EOFPY'
import stat, sys, zipfile
from pathlib import PurePosixPath
zip_path, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as z:
    for info in z.infolist():
        name = info.filename
        p = PurePosixPath(name)
        if not name or name.startswith('/') or name.startswith('\\') or p.is_absolute() or '..' in p.parts:
            raise SystemExit(f"unsafe zip member path: {name!r}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise SystemExit(f"unsafe zip member symlink: {name!r}")
    z.extractall(dest)
EOFPY
}


ZIP="${1:-}"
if [ -z "$ZIP" ] || [ ! -f "$ZIP" ]; then
  echo "usage: zsh scripts/fresh_restore.sh <path-to-mongoose-zip>"
  exit 1
fi

# Default install dir is now $HOME/mongoose. If an explicit MONGOOSE_HOME is set, honour it.
# Otherwise, if no new-style dir exists yet but a legacy $HOME/mongoose-alpha install is present,
# restore into that legacy dir so an existing install keeps its .env/data in place. New installs
# land in $HOME/mongoose.
if [ -n "${MONGOOSE_HOME:-}" ]; then
  DEST="$MONGOOSE_HOME"
elif [ ! -d "$HOME/mongoose" ] && [ -d "$HOME/mongoose-alpha" ]; then
  DEST="$HOME/mongoose-alpha"   # legacy install: migrate in place
else
  DEST="$HOME/mongoose"
fi
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "Fresh restore into: $DEST"

if [ -d "$DEST" ]; then
  echo "Preserving .env, data/, secrets/ and moving old folder aside..."
  TMPSTATE="$(mktemp -d)"
  for item in .env data secrets; do
    if [ -e "$DEST/$item" ]; then
      cp -R "$DEST/$item" "$TMPSTATE/" 2>/dev/null || true
    fi
  done
  mv "$DEST" "${DEST}.broken-${STAMP}"
fi

echo "Unzipping fresh code..."
WORK="$(mktemp -d)"
safe_unzip "$ZIP" "$WORK"
INNER="$(find "$WORK" -maxdepth 2 -name pyproject.toml -print -quit | xargs -I{} dirname {})"
if [ -z "$INNER" ]; then
  echo "ERROR: no pyproject.toml found inside the zip"
  exit 1
fi
mv "$INNER" "$DEST"

if [ -d "${TMPSTATE:-/nonexistent}" ]; then
  echo "Restoring preserved state..."
  for item in .env data secrets; do
    if [ -e "$TMPSTATE/$item" ]; then
      cp -R "$TMPSTATE/$item" "$DEST/" 2>/dev/null || true
    fi
  done
fi

cd "$DEST"

echo "Verifying the tree BEFORE installing..."
grep -n "version" pyproject.toml | head -1
grep -q "benchmark" src/mongoose/cli.py && echo "benchmark command: present" || { echo "ERROR: benchmark command missing from this tree"; exit 1; }

echo "Creating a fresh venv and installing from the verified root..."
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install -e .

echo "Initialising DB and running checks..."
mongoose init-db || true
mongoose doctor --install-root

echo
echo "Done. If 'Install coherence: OK' printed above, the restore is clean."
echo "Old folder kept at: ${DEST}.broken-${STAMP} (delete once happy)."
