# Live connectors and native surfaces

## Connectors

Connectors are data sources Mongoose can read and sync. Each connector has:
- a **probe** (`connector_probes.py`) — checks whether it is configured and reachable
- a **sync** function — fetches recent data into the local SQLite store
- a **chat tool** — lets the brain query the store (read-only)

| Connector     | Probe check                          | Sync function             | Chat tool         |
| ------------- | ------------------------------------ | ------------------------- | ----------------- |
| Gmail         | `google_token_path` exists + valid   | `sync_gmail()`            | `get_email`       |
| IMAP          | host/user/password set               | `sync_imap()`             | `get_email`       |
| MS Graph      | `ms_token_cache` exists + valid      | `sync_ms_email()`         | `get_email`       |
| Google Cal    | same token as Gmail                  | `sync_gcal()`             | `get_calendar`    |
| MS Calendar   | same token as MS Graph               | `sync_ms_calendar()`      | `get_calendar`    |
| MS To Do      | same token as MS Graph               | `sync_ms_tasks()`         | `get_tasks`       |
| Home Assistant| `HA_URL` + `HA_TOKEN` reachable      | live via httpx            | `home_state`      |
| WordPress     | `WP_URL` + Application Password      | via REST API              | `wp_plugin`       |

### IMAP and voicemail

`sync_imap()` and `sync_imap_incremental()` both call `index_voicemail_from_email()` for VoIP provider notification emails, so voicemail promotion runs on all IMAP sync paths.

### Connector sync schedule

The scheduler (configured in cockpit → Scheduler) runs each connector on its own cron. The default fresh-check interval is 15 minutes for email, 30 minutes for calendar. `/connectors/status` reports which connectors are configured, their last sync time, and whether the credentials are valid.

## Native surfaces

### Web (PWA)

The main cockpit at `/ui` is a PWA. `manifest.webmanifest` and `sw.js` are served from `/manifest.webmanifest` and `/sw.js`. On mobile Chrome or Safari the page can be added to the home screen.

### Phone launchpad (paired device)

Any device that scans the QR code from Devices tab gets a paired device token. The launchpad at `/device/launchpad` is a phone-optimised two-tab surface:

- **Talk tab** — push-to-talk (hold to record, Whisper transcribes, brain replies, TTS speaks the reply), text chat input, conversation thread. Shares the same brain conversation as the Mac cockpit.
- **Status tab** — what changed, suggestions, capabilities, TTS voice picker.

Authentication: `X-Mongoose-Device-Token` header (from pairing). All `/device/*` routes accept this token.

### Android companion (planned)

A native Android companion app would use `NotificationListenerService` to relay phone notifications to Mongoose via `/device/notifications/ingest`. This endpoint:
- accepts a device token (no master API token needed)
- body: `{title, text, app, package, priority}`
- stores as a device event and pushes to the notification inbox

The PWA push notification path (via Web Push) is available today; the native notification relay endpoint is scaffolded and ready for the Android companion.

### Voice

Push-to-talk uses the browser's `MediaRecorder` API. HTTPS is required for microphone access. Set up HTTPS from cockpit Settings → Voice on phones (HTTPS) — this generates a local CA certificate (mkcert) and walks you through the one-time trust step on the device. Plain HTTP still works for text chat.

Whisper transcription is auto-detected from PATH. Run `mongoose setup-voice` or use cockpit Settings → Voice transcription → Install.
