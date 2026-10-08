#!/usr/bin/env bash
# ── Mongoose , Set Up RTM (full auto) ───────────────────────────────────────────
# Double-click this AFTER you've run "Install Mongoose.command" once.
#
# What it does, in order:
#   1. Collects your Example Court mailbox + OpenAI + (optional) SMTP credentials.
#   2. Arms the RTM modules and live mailbox monitoring.
#   3. Runs the DRESS REHEARSAL , exercises every module on synthetic data, sends nothing.
#   4. Shows you the report.
#   5. Asks ONE explicit question before enabling live SENDING of approved replies.
#
# The deliberate guardrail (even though you chose full auto): live SENDING is armed only at the end,
# after the rehearsal passes and you confirm. Everything else is set up automatically. This is the
# one thing standing between "install finished" and "a draft goes to real leaseholders", so it gets
# a human tap. The Terminal stays open so you can see what's happening.
# ────────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
ENV_FILE="$REPO/.env"

clear
printf '\n  Mongoose , RTM setup\n  This sets up Example Court email monitoring and draft responses.\n\n'

if [[ ! -d .venv ]]; then
  echo "  Run \"Install Mongoose.command\" first (it sets up the base system)."
  echo "  Press any key to close." ; read -n 1 -s ; exit 1
fi
source .venv/bin/activate 2>/dev/null || true

# --- helper: write or replace a key in .env ---
set_env() {
  local key="$1" val="$2"
  touch "$ENV_FILE"
  # remove any existing line, then append
  grep -v "^${key}=" "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
}

echo "  ── Step 1 of 5: credentials ───────────────────────────────────────────"
echo "  These are stored locally in $ENV_FILE and never leave your Mac."
echo ""

read -r -p "  Example Court IMAP password (info@example-court.example): " -s IMAP_PW ; echo ""
[[ -n "$IMAP_PW" ]] && set_env "RTM_IMAP_PASSWORD" "$IMAP_PW"

read -r -p "  OpenAI API key (for voicemail transcription, optional , Enter to skip): " -s OAI ; echo ""
[[ -n "$OAI" ]] && set_env "OPENAI_API_KEY" "$OAI"

echo ""
echo "  For sending approved replies, Mongoose needs SMTP details."
read -r -p "  SMTP host (e.g. ssl0.ovh.net, Enter to skip sending setup): " SMTP_HOST
if [[ -n "$SMTP_HOST" ]]; then
  set_env "RTM_SMTP_HOST" "$SMTP_HOST"
  read -r -p "  SMTP port [587]: " SMTP_PORT ; SMTP_PORT="${SMTP_PORT:-587}"
  set_env "RTM_SMTP_PORT" "$SMTP_PORT"
  read -r -p "  SMTP username [info@example-court.example]: " SMTP_USER ; SMTP_USER="${SMTP_USER:-info@example-court.example}"
  set_env "RTM_SMTP_USER" "$SMTP_USER"
  read -r -p "  SMTP password: " -s SMTP_PW ; echo ""
  [[ -n "$SMTP_PW" ]] && set_env "RTM_SMTP_PASSWORD" "$SMTP_PW"
fi

echo ""
echo "  ── Step 2 of 5: arming modules ────────────────────────────────────────"
# Arm mailbox monitoring (reads only). Live SEND stays OFF until the end.
set_env "RTM_LIVE_MAILBOX" "true"
set_env "RTM_LIVE_SEND" "false"
echo "  Mailbox monitoring: ARMED (reads info@example-court.example)."
echo "  Live sending: held OFF until after the rehearsal (see step 5)."
echo ""
echo "  Substantive drafts (nuanced/sensitive cases) can be written by a model. Safety and"
echo "  routine cases always use the local template either way. Options:"
echo "    1) local template only (default, fully private, nothing leaves the Mac)"
echo "    2) local Ollama model (stays on the Mac, no API cost)"
echo "    3) Anthropic API (external, per-draft cost, strong at measured correspondence)"
echo "    4) OpenAI API (external, per-draft cost)"
read -r -p "  Choose draft tier [1]: " TIER ; TIER="${TIER:-1}"
case "$TIER" in
  2) set_env "RTM_DRAFT_MODEL_PROVIDER" "local" ;;
  3) set_env "RTM_DRAFT_MODEL_PROVIDER" "anthropic"
     read -r -p "  Anthropic API key: " -s ANTH ; echo ""
     [[ -n "$ANTH" ]] && set_env "ANTHROPIC_API_KEY" "$ANTH" ;;
  4) set_env "RTM_DRAFT_MODEL_PROVIDER" "openai"
     # OpenAI key may already be set from voicemail step; only prompt if missing
     if ! grep -q "^OPENAI_API_KEY=" "$ENV_FILE" 2>/dev/null; then
       read -r -p "  OpenAI API key: " -s OAIK ; echo ""
       [[ -n "$OAIK" ]] && set_env "OPENAI_API_KEY" "$OAIK"
     fi ;;
  *) grep -v "^RTM_DRAFT_MODEL_PROVIDER=" "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
     mv "$ENV_FILE.tmp" "$ENV_FILE" 2>/dev/null || true
     echo "  Using local template only (fully private)." ;;
esac

echo ""
echo "  ── Step 3 of 5: dress rehearsal ───────────────────────────────────────"
echo "  Exercising every module on synthetic data. This sends nothing."
echo ""
# Run the rehearsal with live mailbox temporarily masked so the rehearsal stays hermetic.
RTM_LIVE_MAILBOX=false RTM_LIVE_SEND=false mongoose rehearse
REHEARSAL_RC=$?

echo ""
echo "  ── Step 4 of 5: result ────────────────────────────────────────────────"
if [[ $REHEARSAL_RC -ne 0 ]]; then
  echo "  A module failed the rehearsal (see above). Live sending will NOT be enabled."
  echo "  Fix the failure, then re-run this. Mailbox monitoring stays armed; nothing sends."
  echo ""
  echo "  Press any key to close." ; read -n 1 -s ; exit 1
fi
echo "  All modules passed. Mailbox monitoring is armed. Nothing has been sent."

echo ""
echo "  ── Step 5 of 5: enable live sending? ──────────────────────────────────"
echo "  This is the only irreversible switch. With it ON, a draft you APPROVE on your"
echo "  phone will be actually emailed to the sender. Drafts still never auto-send ,"
echo "  every send needs your explicit approval tap. But approving will now really send."
echo ""
echo "  Recommended: watch one real voicemail/email come in and produce a draft FIRST,"
echo "  approve nothing, confirm it looks right, then re-run this and say yes."
echo ""
read -r -p "  Enable live sending of approved replies now? (yes/no): " CONFIRM
if [[ "$CONFIRM" == "yes" ]]; then
  set_env "RTM_LIVE_SEND" "true"
  echo ""
  echo "  Live sending ENABLED. Approved drafts will now send. You can turn this off any"
  echo "  time by setting RTM_LIVE_SEND=false in $ENV_FILE and restarting."
else
  echo ""
  echo "  Left OFF (the safe choice). Drafts will be created and pushed to your phone, but"
  echo "  approving them will not send until you enable RTM_LIVE_SEND. Re-run this when ready."
fi

echo ""
echo "  ── Done ───────────────────────────────────────────────────────────────"
echo "  Restarting Mongoose to apply settings..."
# bounce the server so the new .env takes effect
pkill -f "mongoose serve" >/dev/null 2>&1 || true
sleep 1
mkdir -p data/logs
( mongoose serve --port "${MONGOOSE_PORT:-8765}" >> data/logs/app.log 2>&1 & ) || true
echo "  Mongoose is running. RTM intake is active."
echo ""
echo "  From here: new mail to info@example-court.example becomes a case, gets a draft, and the"
echo "  draft is pushed to your phone to approve, edit, or cancel."
echo ""
echo "  Press any key to close this window (Mongoose keeps running)."
read -n 1 -s
