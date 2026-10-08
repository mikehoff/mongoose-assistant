# Background tasks, push-to-phone, and the phone file agent

Three capabilities that let Mongoose work on its own and reach you, all local-first.

## 1. Push notifications TO your phone (ntfy)

Mongoose can send a notification to your phone via **ntfy**, a tiny, free,
self-hostable push service. Install the **ntfy app** on your phone, subscribe to a
topic name (e.g. `mongoose-owner`), and anything Mongoose pushes to that topic
arrives as a phone notification in seconds. No native app, no Apple/Google push
fees, and you can run the ntfy server yourself (on the Mac or Pi) so nothing goes
through a third party.

Configure:
```
NTFY_URL=http://<your-ntfy-host>:8090      # or https://ntfy.sh for the public server
NTFY_TOPIC=mongoose-owner                    # subscribe the phone app to this
NTFY_TOKEN=                                 # optional, if your ntfy needs auth
```
Send one:
```
mongoose push "Tribunal reminder" --body "Hearing at 9am" --priority high --tags calendar,warning
```
Over HTTP (`POST /notifications/push`) and via Claude's `send_to_phone` MCP tool too.
`mongoose doctor` shows whether ntfy is configured.

This is distinct from notification ROUTING (phone/HA notifications coming INTO
Mongoose, see NOTIFICATIONS.md). This one is Mongoose -> you.

## 2. The background scheduler

By default Mongoose only acts when you run a command. The scheduler makes it run
recurring jobs on its own:

- **daily_brief**: generates the cross-domain brief, drops it on the bus, AND
  pushes it to your phone. Fires once a day at `MONGOOSE_BRIEF_TIME` (default 07:30
  local).
- **sync_email**: IMAP sync (also indexes attachments) every
  `MONGOOSE_EMAIL_SYNC_MINUTES` (default 30), if IMAP is configured.
- **reindex_files**: re-indexes your file roots every `MONGOOSE_REINDEX_MINUTES`
  (default 360), from `MONGOOSE_INDEX_ROOTS`.

Run it:
```
mongoose scheduler                 # the loop (Ctrl-C to stop)
mongoose scheduler --once          # run any due jobs once and exit (good for cron)
mongoose scheduler --status        # show each job's schedule and last run
```
Configure:
```
MONGOOSE_BRIEF_TIME=07:30
MONGOOSE_EMAIL_SYNC_MINUTES=30
MONGOOSE_REINDEX_MINUTES=360
MONGOOSE_INDEX_ROOTS=/Users/you/Documents:macbook,/Users/you/SyncPhone:phone
```
Each job persists its last run, so a restart never double-fires or forgets. Jobs
are isolated: one failing job is logged and never stops the others or the loop.

### Running it permanently (macOS launchd)

To keep it alive across reboots on the Mac mini, wrap `mongoose scheduler` in a
launchd agent (a `~/Library/LaunchAgents/com.mongoose.scheduler.plist` with
`KeepAlive=true`), or run `mongoose scheduler --once` from cron every few minutes.
The `--once` + cron approach is simplest and equally restart-safe because due-ness
is computed from persisted last-run times.

## 3. Phone file agent (alternative to Syncthing)

Two ways to make your phone's files searchable from the Mac:

- **Syncthing** (see FINDING_FILES.md): mirrors the file BYTES to the Mac, then
  Mongoose indexes them. Use this if you also want the actual files on the Mac.
- **Push agent** (`scripts/android_file_agent.py`): pushes only the phone's INDEX
  (paths, names, safe text snippets) to Mongoose, no byte copy. Lighter; use this
  if you only want searchability, not duplicate storage.

The agent is stdlib-only, so it runs in a bare Termux:
```
# on the phone, in Termux
termux-setup-storage
python3 android_file_agent.py \
  --server http://<mac-ip>:8765 \
  --token  <your MONGOOSE_API_TOKEN> \
  --root   /sdcard \
  --device phone
```
It POSTs batches to `/device-files/ingest`, which writes into the SAME file index
the Mac uses (tagged `device=phone`), so `mongoose search-files` spans both. It
skips `Android/data` and `Android/obb` (unreachable without root) and never uploads
file bytes. Re-running refreshes the index.

Remote access (the phone reaching the Mac) needs, on the Mac:
```
MONGOOSE_HOST=0.0.0.0
MONGOOSE_ALLOW_REMOTE=true
MONGOOSE_API_TOKEN=<secret>
```
and the agent sends `X-Mongoose-Token: <secret>`.
