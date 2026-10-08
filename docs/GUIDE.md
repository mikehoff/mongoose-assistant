# Mongoose: the complete guide

Mongoose is a local-first, privacy-first household assistant. It runs on your own
Mac (a base M4 Mac mini is the reference machine), uses a local language model by
default, and keeps your data on your hardware. Its reason to exist is **cross-domain
synthesis**: looking across your calendar, email, files, home, and notifications at
once and telling you the one thing that matters, which no single-purpose app can do.

This guide covers what it does, how to set it up, and how to use every part.

---

## 1. What Mongoose is (and isn't)

It IS: a local assistant that reads your calendar, email, files, and home state;
searches everything in one place; writes you a daily brief; runs background jobs;
pushes to your phone; and can be driven from a CLI, an HTTP API, or Claude (via
MCP). It runs one model at a time on modest hardware.

It is NOT: a cloud service. Nothing leaves your machine unless you explicitly turn
on cloud escalation. It is not a replacement for your apps; it's the layer that sees
across them.

The guiding principles, in plain terms:
- **Make it real, not a demo.** Every feature is tested; audits are a hard gate.
- **Local-first is the whole point.** Cloud is opt-in, capped, and rare.
- **The brief is the USP.** Cross-domain synthesis is what justifies it existing.
- **Don't make you babysit it.** It runs on its own and reaches you.
- **Stay quiet about what isn't configured** rather than breaking.

---

## 2. Quick start

```
# 1. Set up (guided, staged wizard)
mongoose setup

# 2. Check what's working
mongoose doctor

# 3. Run the API (for the dashboard, phone, HA, MCP)
mongoose serve

# 4. Run background jobs (daily brief, sync, reindex)
mongoose scheduler
```

The wizard walks you through, in stages: Core (Home Assistant), Your data
(calendar/email), Reach you & run on its own (phone push + scheduler), and Remote &
cloud (LAN access + optional cloud). It ends with a readiness check so you see what
actually works.

---

## 3. The daily brief (the core feature)

The brief looks across every domain and surfaces what matters today, leading with
anything time-sensitive, conflicting, actionable, or household-relevant.

```
mongoose brief
```

It computes signals DETERMINISTICALLY first (so the model can't invent a conflict),
then has the local model phrase them. Signals include: calendar clashes, weather vs
your events, overdue/due tasks, unread agent messages, actionable email, urgent
notifications (with the device they came from), memory facts that bear on today, and
selected home state. It also makes CROSS-domain links no single app can: an email
from someone you're meeting today, heating off before bad weather, a recently-edited
file tied to today's agenda.

Privacy is built in: private email/notification content is counted but never read
into the prompt; a sensitive memory fact can't be pulled in by a private email.

The scheduler can generate the brief each morning and push it to your phone.

---

## 4. Finding things: one search across everything

The single most useful command. It searches files (on every device), email
attachments, and email text at once, each result labelled by kind:

```
mongoose find "service charge certificate"
```
```
[file      ] sc_notes.md
             macbook: /Users/you/Documents/sc_notes.md
[file      ] sc_scan.md
             phone: /sdcard/Documents/sc_scan.md
[attachment] service_charge_cert.pdf
             email from agent@block.com — Certificate
[email     ] Service charge demand
             imap/INBOX from agent@block.com
```

Email is treated as a searchable document, its subject is the title, its body the
content, so it shows up alongside files. Restrict with `--kinds file,email` if you
want. Private email/attachments still appear (so you know they exist) but masked.

Under the hood this pulls from three indexes:

### Files (local + cross-device)
```
mongoose index-files ~/Documents --device macbook
mongoose search-files "tax return"
```
Each file is tagged with its device. Text files are read directly; PDFs and Word
docs have their TEXT extracted (with `pypdfium2` + `python-docx` installed) so you
can search inside them, not just by name.

### Phone files (two options)
- **Syncthing**: mirror a phone folder to the Mac, then `index-files` it. The actual
  files land on the Mac. See FINDING_FILES.md.
- **Push agent**: `scripts/android_file_agent.py` runs in Termux and pushes just the
  index (no byte copy). See BACKGROUND_TASKS.md.
Either way the phone's files become searchable with `device=phone`.

### Email attachments
```
mongoose sync-imap --mailbox INBOX
mongoose search-attachments "bundle"
```
Every attachment's filename, sender, subject, date, and (for PDF/Word) its text is
indexed, with a link to the email it came in.

---

## 5. Notifications

### Into Mongoose (phone / tablet / HA)
Route notifications from your devices into Mongoose so the brief sees them. Each is
tagged with its source device. The device-side wiring (Android via Macrodroid/Tasker,
HA via a `rest_command`) is in NOTIFICATIONS.md. iOS can't capture every notification
(an OS limit).

### Out to your phone (ntfy)
Mongoose pushes to your phone via ntfy (install the ntfy app, subscribe to a topic):
```
mongoose push "Tribunal reminder" --body "Hearing 9am" --priority high --tags warning
```
Configure `NTFY_URL`, `NTFY_TOPIC`, optional `NTFY_TOKEN`. Run your own ntfy server
for fully local.

---

## 6. Home Assistant

### Control
Mongoose can control your home through HA (the `control_home` path). Configure
`HA_URL` + `HA_TOKEN` in setup.

### Named assistant prompts
The same conversation starters HA offers (Automation review, Energy report, Setup
guide, etc.), but run through Mongoose's brain so answers are grounded in your setup
and stay local:
```
mongoose ha-prompts                       # the menu
mongoose ha-prompt automation_review
mongoose ha-prompt troubleshoot_device --extra "the hall sensor is unavailable"
```

---

## 7. Background tasks (the scheduler)

Makes Mongoose run on its own:
```
mongoose scheduler            # the loop
mongoose scheduler --once     # run due jobs once (for cron)
mongoose scheduler --status   # schedules + last runs
```
Jobs: the daily brief (pushed to your phone), email sync, and file reindex. Each
job's last run is persisted so restarts never double-fire. Configure timing with
`MONGOOSE_BRIEF_TIME`, `MONGOOSE_EMAIL_SYNC_MINUTES`, `MONGOOSE_REINDEX_MINUTES`,
and the folders to keep indexed with `MONGOOSE_INDEX_ROOTS`. Keep it alive across
reboots with launchd or cron (see BACKGROUND_TASKS.md).

---

## 8. Talking to Mongoose

- **CLI**: `mongoose chat "..."` for one-shot; the commands above for specific tasks.
- **Voice**: `mongoose setup-voice` then the voice session (needs sox/ffmpeg/whisper).
- **HTTP API**: `mongoose serve`, then the dashboard and endpoints.
- **Claude (MCP)**: a local MCP server exposes Mongoose's tools (find, search
  attachments, control home, HA prompts, push to phone, etc.) to Claude
  Desktop/Code, all local. See ASK_MONGOOSE.md.
- **HA Assist**: ask Mongoose through Home Assistant's voice/chat.

---

## 9. Setup, doctor, and update

### Setup
`mongoose setup` is the staged wizard. Re-run it any time to add integrations; it
never overwrites what you've set. `mongoose setup --status` prints integration state
as JSON.

### Doctor
`mongoose doctor` is the readiness report: database, model, weather, Home Assistant,
connectors, the search index (files/devices/attachments), content extraction, push,
and the scheduler. Run it whenever something seems off; it tells you exactly what's
configured and what isn't.

### Update
`mongoose update <path-or-zip>` is guided: it shows from/to versions, backs up your
install, swaps in the new code while keeping your `.env`/data/memory, then merges any
new settings keys, applies database migrations, and runs a readiness check. If
anything looks wrong, `mongoose update --rollback`. Use `--raw` for the bare script.

---

## 10. Privacy and security model

- **Local by default.** The model runs locally; data stays on your machine. Cloud
  escalation (Claude/OpenAI/Gemini) is opt-in, per-turn cost-capped, and used only
  when a local answer is uncertain.
- **Private content is masked, not hidden from you.** Private email/notifications are
  counted and findable but their content isn't read into prompts or spoken aloud.
- **Remote access is gated.** The API is local-only unless you set
  `MONGOOSE_ALLOW_REMOTE=true` + a token; remote devices must send
  `X-Mongoose-Token`. Only `/health` is public.
- **No attachment bytes are copied** unless you opt in; the indexes hold metadata and
  extracted text, so you find where something lives before deciding to store it.
- **Builds need a human gate.** Overnight build plans must be approved before they run.

---

## 11. Hardware and models

Reference machine: base Apple M4 Mac mini, 24GB, models run ONE AT A TIME. Defaults:
chat/tool model `gemma4:26b-a4b-it-q4_K_M` (kept resident), code model
`qwen2.5-coder:14b-instruct-q4_K_M`. Ollama with `OLLAMA_KEEP_ALIVE=-1`. The brief
falls back to a deterministic summary if the model is down, so it's useful even then.

---

## 12. Command reference (most-used)

```
mongoose setup [--status]          Guided staged setup wizard
mongoose doctor                    Readiness report
mongoose serve [--host --port]     Run the API server
mongoose scheduler [--once|--status]  Background jobs
mongoose update <src> [--rollback|--raw]  Guided update

mongoose brief                     The cross-domain daily brief
mongoose find "<q>" [--kinds ...]  Search files + attachments + email
mongoose search-files "<q>"        Files across devices
mongoose search-attachments "<q>"  Email attachments
mongoose index-files <dir> --device <name>   Index a folder

mongoose push "<title>" [--body --priority --tags --click]   Push to phone
mongoose ha-prompts                List HA assistant prompts
mongoose ha-prompt <key> [--extra] Run one

mongoose sync-imap [--mailbox --limit]       Pull email (+ attachments)
mongoose sync-gmail / sync-ms-mail           Pull from Gmail / Outlook
mongoose chat "<prompt>"           One-shot to the local assistant
```

For deeper topics see the focused docs: FINDING_FILES.md (cross-device + content
search), NOTIFICATIONS.md (routing in), BACKGROUND_TASKS.md (push, scheduler, phone
agent), HA_PROMPTS.md, ASK_MONGOOSE.md, and CHANGELOG_SINCE_0_6_6.md (full history).
