#!/usr/bin/env bash
# Build "Mongoose.app": the double-click front door for a singular install.
#
# A person should not need a terminal. They double-click Mongoose.app and it:
#   1. on first run, runs bootstrap_macos.sh (Homebrew/Python3.12/Ollama, venv, install)
#      with a visible Terminal window so they can see progress and answer any prompt;
#   2. starts the local brain (ollama serve) if it isn't already up;
#   3. starts the Mongoose server;
#   4. opens the guided first-run flow (/welcome) in the browser, where they connect
#      information and pair their phone, as one path.
#
# This script GENERATES the .app bundle. It must be run on macOS (it only assembles a
# bundle; assembling is portable, but the .app only RUNS on macOS). Re-run after updates.
#
# NOTE: This is the one piece that can only be exercised on a real Mac. The launcher logic
# is straightforward shell, but a Linux CI/sandbox cannot run or verify a macOS .app. Treat
# the bundle as Mac-validated.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$REPO/Mongoose.app}"
PORT="${MONGOOSE_PORT:-8765}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Mongoose</string>
  <key>CFBundleDisplayName</key><string>Mongoose</string>
  <key>CFBundleIdentifier</key><string>uk.mongoose.app</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>mongoose-launch</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><false/>
</dict></plist>
PLIST

# The launcher. Records the repo path at build time so the bundle can find the code.
cat > "$APP/Contents/MacOS/mongoose-launch" <<LAUNCH
#!/usr/bin/env bash
set -euo pipefail
REPO="$REPO"
PORT="$PORT"
cd "\$REPO"
# shellcheck source=scripts/mongoose_local_probe.sh
. "\$REPO/scripts/mongoose_local_probe.sh"

# First run: no venv yet -> run bootstrap in a visible Terminal so the user sees progress.
if [[ ! -d .venv ]]; then
  osascript -e 'tell app "Terminal" to do script "cd '"\$REPO"' && bash scripts/bootstrap_macos.sh && echo READY_TO_CLOSE"' || {
    # Fallback: run inline if AppleScript/Terminal is unavailable.
    bash scripts/bootstrap_macos.sh
  }
  # wait for the venv to appear (bootstrap finished)
  for _ in \$(seq 1 600); do [[ -d .venv ]] && break; sleep 1; done
fi

source .venv/bin/activate 2>/dev/null || true

# Start the local brain if it isn't already running.
if command -v ollama >/dev/null 2>&1; then
  if ! curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    (ollama serve >/dev/null 2>&1 &)
  fi
fi

# Start the Mongoose server if it isn't already up on PORT. Do not hardcode --host: respect
# MONGOOSE_HOST from .env so an already-enabled phone/LAN setup remains reachable.
if ! mongoose_curl "/health" >/dev/null; then
  (mongoose serve --port "\$PORT" >> data/logs/app.log 2>&1 &)
fi

# Wait for the server and the real web page, then open the guided first-run flow.
for _ in \$(seq 1 60); do
  mongoose_curl "/health" >/dev/null && mongoose_page_ready "/welcome" "Mongoose|Welcome" && break
  sleep 0.5
done
mongoose_open_path "/welcome" >/dev/null
LAUNCH
chmod +x "$APP/Contents/MacOS/mongoose-launch"

echo "Built: $APP"
echo "Double-click it (or: open '$APP'). First run installs prerequisites in a visible"
echo "Terminal; after that it just starts Mongoose and opens the first-run flow."
