# The System Map: what Mongoose IS

The receipts ledger (Activity tab) answers "what did Mongoose DO?". The System tab answers
"what IS Mongoose, right now?", the transparency layer that completes the ecosystem. It's
built for a non-developer to verify the privacy claim instead of being asked to trust a
slogan.

See it: the cockpit System tab, `mongoose system-map`, or GET /cockpit/system-map.

## What it shows

- **What leaves this machine** (shown first, because it matters most). Computed from real
  settings: the on-device model is local by default and cloud escalation is OFF, so your
  content goes only to the local model. Every genuine external flow you've turned on
  (calendar/email sync to fetch your own data, phone push, web search) is listed with
  exactly where it sends data. A clean install honestly says "nothing about your content
  leaves this machine."
- **What it holds**, the real categories of data with ACTUAL row counts (email, WhatsApp,
  calendar, tasks, files, notifications, memory, conversations). No more than the truth.
- **Where it lives**, the real path to the single SQLite file and its size. You can see
  it, back it up, or delete it yourself.
- **Surfaces**, this Mac (the brain that holds your data and does the thinking) and each
  paired device with the capabilities it actually has enabled.

## Honest by construction

Everything is computed from real state, not asserted. Row counts are real counts. The
egress map only flags a connection as "leaving" if it's genuinely configured and points
somewhere external, a localhost service or an unconfigured default is correctly shown as
local/absent, so it neither cries wolf nor hides real egress. If you enable cloud
escalation, the model flow immediately shows as leaving; the map will not hide it.

This closes the "fully transparent ecosystem" goal: a person can see, in plain terms,
what's connected, what each surface can see, where their data lives, and what (if
anything) leaves.

## Web search is on by default (and disclosed)

As of v0.24.0, web search ships ON by default, pointed at a LOCAL SearXNG container that
runs on your machine (docker compose service `searxng`, bound to 127.0.0.1:8888). This is
local-first: your search terms route through SearXNG on your own box rather than a
third-party server. But searching the web inherently reaches the internet, so the System
Map discloses web search as an outbound path that fires ONLY when you actually search. The
"nothing leaves" claim is therefore stated precisely: nothing leaves on its own; web search
reaches search engines only when you use it. To turn web search off entirely, set
MONGOOSE_SEARXNG_URL empty.
