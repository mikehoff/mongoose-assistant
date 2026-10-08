# Routing notifications into Mongoose (phones, tablets, Home Assistant)

Mongoose can ingest notifications from your phones, tablets, and Home Assistant,
tag each with the device it came from, and surface what matters in the daily brief
(privacy-gated, so private content is counted, not read aloud). Everything below
is local-first: your devices POST directly to Mongoose on your LAN, nothing goes
through a third-party cloud.

The ingest endpoint is:
```
POST http://<mac-ip>:8765/notifications/ingest
Content-Type: application/json
{
  "source": "android",        // android | ios | home_assistant | ...
  "app": "WhatsApp",           // the app that raised it
  "title": "Sam: dinner?",
  "body": "7pm?",              // optional
  "privacy": "medium",         // low | medium | high | secret (gates speaking it)
  "urgency": "normal",         // normal | high | critical (high/critical surface in the brief)
  "device": "pixel_phone"      // which device this came from
}
```
Because `/notifications/ingest` is a private route, remote devices must send the
token: set `MONGOOSE_ALLOW_REMOTE=true`, `MONGOOSE_API_TOKEN=<secret>`,
`MONGOOSE_HOST=0.0.0.0` on the Mac, and add header `X-Mongoose-Token: <secret>` to
each request. `mongoose doctor` flags this if it's not set.

## 1. Android phone / tablet

The clean local-first route is an automation app that fires an HTTP POST when a
notification arrives. No custom app to build.

**Option A: Macrodroid or Tasker (recommended).**
1. Trigger: "Notification received" (you can scope to specific apps or all).
2. Action: HTTP POST to `http://<mac-ip>:8765/notifications/ingest` with the JSON
   above, mapping the notification's app/title/text into the fields, and set
   `device` to a label for this phone/tablet (e.g. `pixel_phone`, `ipad_mini`).
3. Add the `X-Mongoose-Token` header.

In Tasker the notification trigger exposes `%evtprm()` / `%antitle` / `%antext`
variables you map into the body. In Macrodroid use the "Notification" trigger and
the "HTTP Request" action.

**Option B: Home Assistant Companion app.** If you already run the HA Companion app
on the phone, you can mirror device notifications into HA and let HA forward them
(see section 3), avoiding a second automation app.

**Privacy:** set `privacy: high` for sensitive apps (banking, health, messaging) so
their content is counted but never spoken; `low` only for things safe to announce.

## 2. iOS (iPhone / iPad)

iOS doesn't expose a global "notification received" trigger to third-party apps, so
the options are narrower:
- **Shortcuts automation** for specific triggers (e.g. when a particular app or
  focus changes) firing a "Get Contents of URL" POST.
- **HA Companion app** mirroring, as in Option B above.
Full per-notification capture isn't possible on iOS the way it is on Android; this
is an OS limitation, not a Mongoose one.

## 3. Home Assistant notifications

Have HA POST to Mongoose whenever something noteworthy happens. Add a
`rest_command` and call it from automations (or wrap it as a `notify` target).

In `configuration.yaml`:
```yaml
rest_command:
  mongoose_notify:
    url: "http://<mac-ip>:8765/notifications/ingest"
    method: POST
    headers:
      Content-Type: application/json
      X-Mongoose-Token: !secret mongoose_api_token
    payload: >
      {
        "source": "home_assistant",
        "app": "{{ app | default('HA') }}",
        "title": "{{ title }}",
        "body": "{{ message | default('') }}",
        "privacy": "{{ privacy | default('medium') }}",
        "urgency": "{{ urgency | default('high') }}",
        "device": "home_assistant"
      }
```
Then in any automation's actions:
```yaml
- service: rest_command.mongoose_notify
  data:
    title: "Back door unlocked"
    message: "Unlocked for 10 minutes"
    urgency: high
```
Add `mongoose_api_token: <secret>` to `secrets.yaml`. Now door/leak/alarm/battery
automations land in Mongoose and show up in the brief.

## Seeing them

- The daily brief surfaces high/critical notifications and counts the rest, with
  private content masked. `mongoose brief`.
- Search: `GET /notifications/search?q=...`, or via Claude's MCP tools.
- Claude (and HA, via MCP) can also PUSH a notification with the `push_notification`
  tool.
- Each notification carries its `device`, so you can tell a phone alert from a
  tablet alert from an HA event.
