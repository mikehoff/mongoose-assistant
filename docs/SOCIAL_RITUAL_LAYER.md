# The social + ritual layer

This layer makes Mongoose a low-friction support presence: it helps you do the
useful embodied and social things you already know help, and leaves a trustworthy
trace. The principle: help you do the useful thing at the right moment, with minimum
friction, and never act socially on your behalf without your say-so.

## Useful rituals

Short, concrete micro-practices, not vibe quests. Each has a fixed shape (name,
trigger phrase, duration, script, completion marker, cooldown) and Mongoose tracks
completions so it learns which ones you actually finish.

```
mongoose ritual                    # list them
mongoose ritual physio             # start one, get the script
mongoose ritual "breathing minute" # natural phrase works too
mongoose ritual --done 7           # mark a started ritual complete
mongoose ritual --stats            # completion stats
```

Built in: physio, proprioception, breathing, journal, resilience, friend_checkin.
The scripts are plain and embodied (e.g. physio: "Rib over pelvis, slow breath,
shoulders softening. Do the boring useful thing."). A too-soon repeat is flagged,
not blocked, so Mongoose can say "you did this 20 min ago" rather than nag.

## What changed?

Turns noise into meaningful deltas since you last looked:

```
mongoose what-changed          # and advance the checkpoint
mongoose what-changed --peek   # look without resetting
```

It compares new WhatsApps, new email, newly-indexed files, and new notifications,
then tells you only what's new ("Sam replied, your phone indexed 42 new files, and
tomorrow's 10am moved. Nothing else needs your nervous system."). Acknowledging
advances the checkpoint so the next call is genuinely fresh.

## Mongoose diary

A local, readable activity log (not emotional journalling unless you ask):

```
mongoose diary          # today
mongoose diary --week   # weekly roll-up: counts by kind + ritual completions
```

## Receipts

After a piece of work, Mongoose leaves a short honest summary (and logs it to the
diary), so the system feels solid: "Done. Indexed 318 phone files, found 12 new
attachments, skipped 2 unreadable PDFs." The daily brief leaves a receipt naming
which domains fed it.

## WhatsApp (bidirectional, approval-gated)

Mongoose indexes your WhatsApp messages locally (searchable, brief-relevant,
privacy-classed) and can DRAFT replies, but it never sends without your explicit
approval.

```
mongoose whatsapp search "parcel refund"      # search indexed messages
mongoose whatsapp from Sam                  # recent messages with a contact
mongoose whatsapp waiting                   # who you haven't replied to
mongoose whatsapp draft Sam "thinking of you, hope today's being kind"
mongoose whatsapp drafts                    # pending drafts
mongoose whatsapp approve 3                 # send draft #3 (explicit)
mongoose whatsapp cancel 3
```

### The safety boundary (important)

This is a hard architectural rule, not a setting:

- Mongoose **may** draft messages, suggest who to check in with, and summarise
  incoming messages locally.
- Mongoose **must not** send without explicit approval of a specific draft.

There is deliberately no function, and no MCP tool, that both composes and sends in
one step. The model-facing surface exposes `whatsapp_draft` (creates a pending
draft) but no send tool. Sending only happens via `approve_and_send(draft_id)` with
a draft id a human chose, and a draft can't be sent twice or after cancellation. The
actual transport (an unofficial WhatsApp route) is isolated as a swappable `send_fn`
so a broken route can't corrupt the safety model or the local index.

The brief gains a SOCIAL line surfacing who's waiting on a reply (the name and that
they're waiting, never the message content).

## How this shows up in conversation

Through the MCP tools (`ritual`, `what_changed`, `whatsapp_search`, `whatsapp_draft`)
and the brief's social signal, natural requests work: "what did Sam say?", "draft a
soft check-in to Sam" (drafts, doesn't send), "who have I not replied to?", "what
changed?", "physio check".

## Honest limits

The WhatsApp transport itself isn't bundled (unofficial routes are fragile and
account-specific); the bridge is built to receive indexed messages and to send only
via an approved draft through a `send_fn` you wire to your chosen route. Until a
transport is configured, `approve` marks a draft approved without transmitting. The
ritual scripts are fixed text, not adaptive coaching. "What changed" is delta-by-
checkpoint, not a semantic importance ranker.

## WhatsApp transport: WAHA local sidecar (v0.14.1)

The transport is now real. WAHA (devlikeapro/waha) is a self-hosted WhatsApp HTTP API
that runs as a local Docker sidecar, links your phone by QR, sends via REST, and
delivers inbound messages by webhook. Mongoose speaks only HTTP to it; the WhatsApp
Web session lives entirely inside the container.

Set up:
```
mongoose setup --capability whatsapp     # checks Docker, generates an HMAC key, writes .env
docker compose -f docker-compose.whatsapp.yml up -d
# open http://127.0.0.1:3000 and scan the QR (WhatsApp > Linked Devices)
mongoose whatsapp status                 # want state WORKING
```

Architecture:
- **Provider abstraction** (`whatsapp_providers/`): a `WhatsAppProvider` interface and
  a normalised message model, so nothing in Mongoose depends on WAHA's raw payloads.
  Baileys can be a second provider later without touching the index or brief.
- **Inbound** arrives at `POST /whatsapp/webhook/waha`, which is HMAC-verified
  (`WHATSAPP_HOOK_HMAC_KEY`) or token-verified, and never accepts an unverified post.
  Messages are normalised, deduped by provider message id, indexed, and raise a
  notification that feeds what-changed, the brief, and friend check-ins. Media is
  metadata-only by default.
- **Outbound** safety boundary is unchanged: draft -> approve -> send. With a provider
  configured, `approve_and_send(draft_id)` transmits via the provider's `send_text`;
  the model still has no send path (only `whatsapp_draft` over MCP).
- **Freshness is webhook-based** (event-driven); historical completeness is provider-
  dependent and not overpromised (honest "from linking onward").
- **doctor** reports the session state (SCAN_QR_CODE / WORKING), not just "container up".

Conservative posture for the unofficial route: no bulk messaging, no cold outreach,
no unsupervised sending, no raw model send authority, human approval for every
outgoing message, WhatsApp content defaults to privacy=high, WAHA bound to 127.0.0.1
only. The transport image isn't in the zip (you pull it via Docker); the compose file
and `setup --capability whatsapp` wire it.
