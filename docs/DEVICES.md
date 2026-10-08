# Devices: pairing phones and tablets (v0.17 groundwork)

Mongoose's Mac mini is the brain. Phones and tablets become paired *surfaces*: a place
to approve actions, use voice, and (in later versions) feed notifications and selected
files. This version ships the Mac-side groundwork: a device registry, QR pairing, LAN
discovery, and per-device management. The Android Companion app itself is a later
version; today you pair the device's browser (PWA) or, when the app exists, the app.

## Pair a device

In the cockpit, open **Devices → Add phone** (or Add tablet). A QR code appears. On the
device, scan it with the camera (or open the shown URL). Name the device and confirm.
That's it: no IPs to type, no tokens to copy, no .env editing.

From the terminal instead: `mongoose devices pair` prints a code and the URL.

## How pairing is kept safe

- A pairing code can only be minted by an **authenticated** cockpit user (the button is
  nonce-gated). Someone else on your network cannot mint codes.
- Codes are **single-use** and **expire in 5 minutes**.
- The device receives a per-device token; Mongoose stores only its **hash**, never the
  token itself.
- A paired device authenticates with that token (header `X-Mongoose-Device-Token`) and
  only works when remote access is enabled (`MONGOOSE_ALLOW_REMOTE=true`).
- **Revoke** a device any time (Devices tab, or `mongoose devices revoke --id <id>`); its
  access stops on the very next request.

## Discovery

When the server runs, it advertises itself on the LAN as `_mongoose._tcp.local` so a
companion app using Android's Network Service Discovery can find the Mac automatically.
If mDNS is blocked or `zeroconf` isn't installed, pairing by QR or manual address still
works, the app should always tell you *why* discovery failed rather than leave you
guessing.

## Device profiles

- **Phone** (primary device): approvals, voice, notifications, share sheet, selected
  folders, push.
- **Tablet** (home surface): cockpit, voice, search, approvals, logs, and by default it
  does *not* capture notifications.

These are starting capability sets; later versions let you tune them per device.

## Optional dependencies

QR rendering and mDNS need `qrcode` and `zeroconf`: `pip install -e '.[devices]'`. Both
are soft, the core pairing flow works without them (you just type the address and lose
auto-discovery).

## What this version is NOT

This is the pairing and registry foundation. It does not yet include the native Android
Companion app, notification ingest, or folder indexing, those build on this foundation
in later versions. The honest shape: the Mac can now pair, list, and revoke devices
safely; the phone-side app comes next.

## What each device can do (not just "is it paired")

The registry encodes what Android is *for* in Mongoose. Each capability has a state:

- **enabled** (✓): the device is doing this now.
- **available** (○): the device's role supports this, but it isn't turned on yet. Mongoose
  shows a next step to enable it.
- **off by design** (—): deliberately not for this role. A home tablet, for example, does
  not capture personal notifications by design, this is shown so you know it's a choice,
  not a gap.

So Mongoose can say things like "Motorola notifications are not enabled yet" versus
"Honor tablet has no personal notifications by design", and "this device has not checked
in for 3 days". The Devices tab shows per-capability chips, a freshness state, and a
clear next useful step per device.

A device reports the capabilities it has actually enabled (after the user grants a
permission in the companion app) via `POST /device/capabilities` using its own device
token; off-by-design capabilities for the role are ignored.

## Pairing receipts

Every pairing, capability change, and revocation leaves a receipt (Devices tab, bottom).
Receipts are a core Mongoose trust feature: you can always see what happened and when.

## v0.20 companion-loop additions

Paired devices are now scoped Mongoose surfaces rather than broad API clients. A paired
phone/tablet token is accepted only on `/device/*` routes. Use the master API token for
admin, setup, home-control, indexing and other privileged routes.

### Device routes

- `GET /device/whoami` returns safe metadata for the paired device: role/profile,
  capability states, freshness and next useful setup step.
- `POST /device/heartbeat` updates `last_seen` and returns current device state.
- `GET /device/summary` powers the mobile/PWA Launchpad with Mongoose status, What
  Changed, suggestions, approvals count and device readiness.
- `POST /device/event` accepts allowlisted state events from a future Android companion
  or PWA: `device_checkin`, `pwa_installed`, `capability_update`, `voice_available`,
  `notifications_permission_missing`, `folders_not_configured`,
  `share_intake_created`, `ambient_presence_update`, and `offline_queue_flushed`.
- `POST /device/share` is the PWA precursor to Android Share Sheet: it saves a bounded
  text/link note into Mongoose's local bus as a message from the device.
- `GET /device/launchpad` is a public static shell. It embeds no private data; it reads
  the device token stored by the pairing page and calls `/device/*` APIs with that
  token.

### Capability states

Device capability states are product-facing, not just internal flags:

- `enabled` means the device is currently contributing that capability.
- `available` means the capability can be enabled later.
- `missing_permission` means the role expects it but the device has not enabled it yet.
- `off_by_design` means the role intentionally does not use it, for example personal
  notification capture on a shared home tablet.
- `not_supported_yet` marks future companion capabilities.

This lets Mongoose say “your Motorola phone has not enabled notification capture yet”
without treating the Honor tablet the same way if notifications are intentionally off.
