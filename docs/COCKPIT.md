# Mongoose Cockpit

A small local control panel that answers the four questions a local-assistant owner
actually has: is it working, what is it doing, what needs me, what did it do. It is a
VIEW LAYER over existing endpoints, no new business logic, no SPA framework, one
self-contained HTML page.

## Open it

```
mongoose serve
# then open http://127.0.0.1:8765/ui   (host/port per your settings)
```

## Tabs

- Overview: is Mongoose alive, and the status of each integration (from /doctor +
  /system/status). The headline answers "broken or just not configured?".
- Setup & Repair: capability readiness from /doctor; buttons run doctor and install
  missing capabilities (POST /setup/install-missing, the same path as
  `mongoose update --install-missing`).
- Activity: a timeline of recent bus events (/bus/list), "what changed".
- Approvals: pending bounded-autonomy actions that GENUINELY exist (WhatsApp drafts
  awaiting send). Approve/Cancel call the real /whatsapp/outbox/{id}/approve|cancel
  endpoints. No placeholder approvals are invented.
- Search: query your indexed files/email/notes (/find).
- Models: conversation owner + research worker + co-residency (/cockpit/models, which
  wraps the honest residency probe; reports unknown rather than guessing).
- Scheduler: registered jobs, schedule, last run/status (/cockpit/scheduler).

## Design principles

- Real actions only: every button calls an existing endpoint. The cockpit cannot do
  anything the CLI/API can't already do; it just makes it visible and one click away.
- No invented autonomy: Approvals reflects the real queue. If Mongoose can't do
  something autonomously, it doesn't appear as an approvable action.
- Polls, doesn't push: tabs refresh every ~15s. Minimal vanilla JS, no build step, so
  it won't rot into a maintenance burden while Mongoose evolves.
- Local-first: served by the same FastAPI app, intended for 127.0.0.1.

## Security (v0.14.13a hardening)

The cockpit displays content from WhatsApp, email, files and the bus, all
attacker-influenceable. Because the page is same-origin with privileged POSTs, that's
an injection risk. Mitigations:

- Output escaping: every untrusted value is HTML-escaped (esc()) before rendering, so
  a malicious message body / filename / snippet cannot inject script.
- Action nonce: privileged POSTs (install-missing, draft approve/cancel) require a
  per-process cockpit nonce read same-origin from /cockpit/nonce, or the API token
  (which the phone/ntfy flow sends). Injected or cross-origin script that can't read
  either secret can't forge a send or an install.
- Install is plan/confirm: the Setup tab previews WHAT will be installed and WHERE
  (GET /setup/install-missing), and only installs after you confirm (POST + nonce).
- Activity peeks: /cockpit/what-changed uses acknowledge=False, so viewing the
  cockpit never resets your "since last looked" checkpoint.

## LAN / iPad access (deliberate decision required)

By default every route except /health and the WhatsApp webhook is private, and a plain
browser opening /ui over the LAN cannot attach the API token header. So the cockpit is
a MAC-LOCAL UI by default. To use it from an iPad on your LAN, pick ONE explicitly:

1. Local-only (default, safest): use it on the Mac, or over an SSH tunnel
   (`ssh -L 8765:127.0.0.1:8765 mac`), then browse to 127.0.0.1:8765/ui on the device.
2. Reverse proxy: put the cockpit behind a proxy on your LAN that injects the token /
   terminates auth (e.g. Caddy/nginx with basic-auth + header injection).
3. Token-login session cookie: a future option (set the token once via a login form,
   stored as an http-only cookie). Not built yet, flagged so the choice is yours.

The SSH tunnel (option 1) is the zero-config way to reach it from the iPad today
without weakening the private-by-default guard.

## Using it from phones/tablets on your LAN (v0.14.15)

The cockpit now supports browser login, so any device on your home network can use it.

1. On the Mac, enable remote access and set a token, then serve on the LAN:

   ```
   export MONGOOSE_ALLOW_REMOTE=true
   export MONGOOSE_API_TOKEN=<a-long-random-string>
   mongoose serve --host 0.0.0.0
   ```

2. On each phone/tablet, open `http://<mac-lan-ip>:8765/login`, enter the token once,
   and tick "remember this device" (30 days). After that the cockpit and voice chat
   just work on that device.

How the auth works:
- Login exchanges the token for a signed, expiring session cookie (http-only,
  SameSite=Lax). The cookie can't be read by script or forged without the server
  secret, and it dies on expiry or when the token changes.
- Mutating actions (install, approve/cancel, voice) ALSO require the per-request
  cockpit nonce, so a cookie alone can't be used by a malicious page to forge an
  action. Cookie = "this device may use Mongoose"; nonce = "this same-origin page
  issued this action".

### Voice chat

The Voice tab records audio in the browser, posts it to the server, which transcribes
with Whisper and runs it through the conversation owner; the reply is shown and spoken
back on the device (browser speech synthesis). Needs Whisper installed on the Mac
(`mongoose setup-voice`); it says so clearly if not. Note: browsers only allow mic
access on https or localhost, so for mic capture from a tablet over plain http you may
need to add it as a trusted origin, or front the cockpit with an https reverse proxy.

### Transport

Plain HTTP on the LAN is supported (home network). The token travels in the login POST
body; on a trusted home network that's acceptable, but if you want it encrypted, put an
https reverse proxy in front. HTTPS is also what unlocks mic capture on non-localhost
devices without per-browser origin tweaks.

## Voice over LAN needs HTTPS (browser limitation)

Browsers only allow microphone capture in a "secure context": https or localhost. A
phone browsing to http://<mac-ip>:8765 is NOT localhost from its perspective, so mic
recording is blocked there over plain http. The Voice tab now detects this and says so
on the device. Your options:

- Voice on the Mac itself (localhost is a secure context), or
- Put an https reverse proxy in front (Caddy with a local CA / mkcert is the easy
  path), then voice works from any device, or
- Use the CLI voice command on the Mac as the reliable local fallback.

Everything else in the cockpit (status, setup, activity, approvals, search, models,
scheduler, and TYPED interaction) works from any LAN device over plain http after login.

## Security model (v0.14.16)

- Session cookie = "this device may use Mongoose" (signed, http-only, SameSite=Lax,
  persisted across restarts; invalidated when the API token changes).
- Cockpit nonce = "this same-origin page issued this action". REQUIRED on ALL
  cookie-authenticated remote WRITES (POST/PUT/PATCH/DELETE), enforced centrally in the
  route guard, not per-route. A header-token caller (phone/ntfy/script) is exempt
  because it already proved it holds the secret.
- Login is per-IP throttled (cooldown after repeated failures) and logs failed
  attempts without storing the supplied token.
- The voice relay caps uploads at 25MB.

## Logs tab (v0.15.1)

The Logs tab reads the durable logs in data/logs (the scheduler's output when run as a
launchd service, see docs/SERVICE.md). Pick a file to see its tail (last 64KB), with the
list refreshing periodically.

Security: the viewer can ONLY open files that are actually inside the log dir, it
enumerates them and serves by base name, so there is no path traversal (an unknown or
`../`-style name returns 404). Reads are bounded (tail, not the whole file), and log
lines are rendered as text, never HTML, so a hostile log line can't inject script.

## Setup wizard (the front door)

On first run, the cockpit opens on the Setup tab as a guided, staged wizard rather than
the status page, because a fresh install needs to be configured before the dashboards
mean anything. Once the Core stage (Home Assistant) is set up, the cockpit boots into
Overview normally and the wizard stays available under Setup & Repair.

The wizard walks four stages: Core, Your data (calendar/email), Reach you & run on its
own (push + scheduler), and Remote & cloud (optional). Each integration shows its real
setup help, the fields to fill, and a Save (and test, where a verifier exists) button
that writes .env and checks the connection inline. Per-stage counters show progress.

One source of truth: the browser wizard is driven by the SAME integration registry as
`mongoose setup` (the CLI wizard), so the two can never disagree about what exists or
what each integration needs. The browser presents it differently (staged cards, inline
verify, progress), but the integrations and their fields come from one place. Saves are
nonce-protected, and secret values are never sent to the browser (it only shows whether
a secret is set).
