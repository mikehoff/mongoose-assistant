# Companion Loop

Mongoose is moving from “dashboard + tools” towards a resident local companion:

```text
fresh state → correlation → useful suggestion → explanation → optional approval/action → receipt
```

The loop is deliberately grounded. Mongoose should not produce vague motivational
insights or act without approval. It should notice fresh state, correlate it across
local sources, explain why the suggestion appeared, and leave a receipt when something
meaningful changes.

## Prepare Me cards

`mongoose prepare <topic>` and `GET /cockpit/prepare?q=<topic>` build a compact context
card for an event, person, project, task, place or situation. The card gathers local
evidence from calendar, email, files, WhatsApp and memory where available.

A card contains:

- what this is about;
- why it was generated;
- useful local context;
- open loops;
- suggested next step;
- evidence/source records;
- known limits.

No model is required to invent the card. The model may phrase it later, but the facts
are assembled deterministically first.

## Mobile / paired-device Launchpad

The phone/tablet PWA is a surface, not a second brain. After pairing, `/device/launchpad`
shows a mobile-friendly view backed by device-token routes:

- Mongoose status;
- What Changed summary;
- companion suggestions;
- approval count;
- device readiness and next useful setup step;
- share-to-Mongoose intake.

Native Android notification capture, folder indexing, widgets and share sheet are later
work. This version provides the safe Mac-side/PWA foundation.

## Device-token scope

Paired device tokens are intentionally not master API tokens. They are valid only for
`/device/*` routes such as heartbeat, status, event intake and share-to-Mongoose.
They must not unlock setup, home control, memory mutation, WhatsApp sending, build
runner actions or broad Cockpit writes.

## Visible local awareness

Always-on awareness is acceptable when it is local, scoped, visible and reversible.
This version adds foundations: device heartbeat, allowlisted device events, What
Changed device deltas, device-readiness suggestions, and receipts for meaningful state
changes. It does not add hidden surveillance or uncontrolled exfiltration.
