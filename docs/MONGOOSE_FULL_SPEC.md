# Mongoose Implementation Specification

## 1. Purpose

Mongoose is a local-first personal assistant stack for a Mac mini M4 with 24 GB unified memory. The aim is not to build a toy chatbot. The aim is to build a reliable local control and context layer that can speak through tablets, watches, iPads, and home devices while keeping private data under explicit control.

Mongoose pulls v1 to v3 into one implementable scope:

- Voice and personality foundation.
- Weather engine with caching, precision rules, UV and rain handling.
- Home control routing using Home Assistant first and Alexa fallback where configured.
- Notification cache, index, deduplication, and rerouting.
- Calendar and task context bus.
- Email and file search as read-first local indexing.

The system must be fun enough to use, but boringly careful where consequences matter.

## 2. Non-negotiable requirements

### 2.1 Local-first brain

Mongoose must run the local service, cache, and routing logic on the Mac mini. Cloud APIs may be used as data sources where needed, but Mongoose must not depend on a cloud LLM for its core operation.

### 2.2 Truth before personality

Mongoose may be playful, but it must not invent facts. If data is stale, partial, or missing, it must say so or degrade gracefully.

### 2.3 Cached context, not panic-fetching

Weather, notifications, calendars, tasks, email metadata, and file indexes must be cached locally. Mongoose should answer from fresh cache where possible and refresh silently when stale.

### 2.4 Explicit routing, not vague memory

Adaptive behaviour must be stored as explicit evidence in SQLite tables. Mongoose may learn routing preferences from outcomes, but it must not rely on an LLM's vague memory.

### 2.5 Private data boundaries

Alexa and shared-room speakers must not be used for private content such as email, calendar details, legal/admin evidence, adult-health questions, or file content unless the user explicitly allows a narrow case.

### 2.6 Read-first personal data

Mongoose may search and summarise email, calendar, task, notification, and file data. Mongoose must not send email, delete data, archive messages, make purchases, or modify calendars unless a future explicit permission layer is added.

## 3. Decisive stack choices

### 3.1 Runtime

Use Python 3.12, FastAPI, SQLite, httpx, and pytest.

Reason: this is enough for a robust local service with typed request models, HTTP integrations, local persistence, and a testable spine. A heavier stack would add fragility before the assistant has proved its routing and privacy behaviours.

### 3.2 Model runtime

Use Ollama as the default local model runtime, with role-based model selection.

Reason: Ollama is simple, scriptable, local, and exposes HTTP APIs. Mongoose keeps the model runtime separate from the policy/routing layer, so the assistant can later benchmark MLX-LM or LM Studio without rewriting the FastAPI/tool spine.

Current defaults for the base M4 Mac mini, 24 GB unified memory:

```text
OLLAMA_CHAT_MODEL=gemma4:26b-a4b-it-q4_K_M
# OLLAMA_TOOL_MODEL defaults to the resident chat model (Gemma) to avoid swap
# thrash on 24GB. gpt-oss:20b is an optional override, not the default:
# OLLAMA_TOOL_MODEL=gpt-oss:20b
OLLAMA_CODE_MODEL=qwen2.5-coder:14b-instruct-q4_K_M
OLLAMA_KEEP_ALIVE=-1   # keeps the resident model loaded forever (no cold starts)
```

Rationale:

- The base M4 is memory-bandwidth-bound, so dense 27B models are the wrong default even when they technically load. Small-active MoE models are a better fit.
- Gemma 4 26B-A4B is the persona/chat model because it gives warmer conversational style and MoE responsiveness.
- The tool/reasoning role defaults to the resident chat model (Gemma 4 26B-A4B) to avoid a second model load on 24GB. gpt-oss:20b is an optional override, not the default; live testing on the target machine showed resident Gemma was more useful and avoided model-swap latency.
- Qwen2.5 Coder 14B Q4_K_M remains the practical coding model.
- `OLLAMA_KEEP_ALIVE=-1` keeps the resident model loaded permanently (no cold starts), pinning ~17GB. Set `0` or `10m` to free memory between calls at the cost of reload latency.

Do not use the older Qwen-only 8B/14B ladder as the active recommendation. That was an early baseline and is now superseded by the tech-watch MoE decision.

### 3.3 User interface

Use Open WebUI optionally, not as the core.

Reason: Mongoose needs a local API and state machine independent of any chat UI. Open WebUI is useful for chat, RAG experiments, and model interaction, but the assistant should not be architecturally trapped inside a UI.

### 3.4 Smart home control

Use Home Assistant as the canonical control plane.

Reason: it provides a local-first entity model, state verification, explicit exposed entities, automations, mobile notifications, and a mature voice pipeline. Alexa is useful but should be treated as a fallback bridge for non-sensitive household control.

### 3.5 Alexa fallback

Use a configured Alexa bridge endpoint, not scraped private Amazon APIs, as the fallback path.

Reason: there is no clean general-purpose local API that lets a private assistant directly command Alexa for arbitrary household actions. Mongoose therefore implements an adapter boundary:

```text
Mongoose -> AlexaFallbackAdapter -> user-configured bridge URL
```

That bridge can later be Node-RED, a custom Alexa skill flow, an Alexa-compatible service, or another maintained bridge. The Mongoose side is already ready.

### 3.6 Notifications

Use SQLite as the cache and index. Use ntfy as the preferred optional push layer.

Reason: ntfy is simple, HTTP-based, self-hostable, and works well with scripts and mobile devices. SQLite FTS5 is enough for the local searchable event stream.

### 3.7 Weather

Use provider aggregation with Open-Meteo as the default no-key provider, and optional Met Office DataHub, Apple WeatherKit, Tomorrow.io, or OpenWeather when credentials are available.

Reason: the default must work immediately without accounts. Precision improves when paid or authenticated providers are added. Minute-level rain timing should only be expressed when a minutely or nowcasting provider is available.

### 3.8 Calendars and tasks

Use official APIs where available:

- Google Calendar API for Google calendars.
- Microsoft Graph for Outlook calendars and Microsoft To Do.
- Fallback to partial signals if work calendar access is blocked.

Reason: source-level ingestion is more reliable than notification scraping.

### 3.9 Email and files

Use local indexing first:

- IMAP connector for generic mailboxes and Proton Mail Bridge.
- Gmail API and Microsoft Graph mail connector slots for API-based accounts.
- SQLite FTS5 for search.
- Local file indexing for chosen folders.

Reason: Mongoose should search evidence and context locally rather than free-roaming live mailboxes at voice-command speed.

## 4. Architecture

```text
Voice/tablet/watch/web request
        ↓
Mongoose API
        ↓
Intent or endpoint-specific handler
        ↓
Local SQLite cache/index
        ↓
Connectors only when needed
        ↓
Routing, summary, or action
        ↓
Audit log + adaptive learning
```

## 5. Service modules

### 5.1 Persona module

Stores the Mongoose personality contract and keeps style separate from factual data.

Required behaviours:

- short spoken-friendly replies by default;
- funny only when it helps;
- blunt adult health explanations without erotic roleplay;
- no corporate beige;
- no fake certainty;
- no accidental haunted weather app metaphors.

### 5.2 Weather module

Maintains weather cache and voice summary.

Required behaviours:

- prefetch on schedule;
- answer from cache where fresh;
- silently refresh when stale;
- identify UV peak window;
- identify rain window;
- never fake minute-level rain precision;
- rotate short phrases by condition.

### 5.3 Home router

Routes home commands.

Required behaviours:

- default route Home Assistant;
- verify state where entity ID is known;
- fallback to Alexa bridge only for non-sensitive household actions;
- log every attempt;
- adapt routing preferences using explicit evidence.

### 5.4 Notification broker

Ingests notifications and events.

Required behaviours:

- deduplicate repeated alerts;
- classify privacy and urgency;
- route to suitable device/surface;
- avoid shared-room speech for private content;
- maintain searchable history;
- learn from opened, ignored, dismissed, corrected, or escalated outcomes.

### 5.5 Calendar/task context bus

Ingests calendar and task data.

Required behaviours:

- support Google Calendar;
- support Microsoft Graph calendar;
- support Microsoft To Do;
- support work-calendar partial fallback;
- use busy/free even when details are unavailable;
- inform interruption rules and notification routing.

### 5.6 Email/file index

Provides read-first retrieval.

Required behaviours:

- index selected accounts/folders;
- store message metadata and safe snippets;
- support FTS search;
- avoid sending or modifying mail;
- index selected local folders;
- avoid indexing private folders unless explicitly configured.

## 6. Data model

SQLite is used because it is simple, local, durable, queryable, and enough for this scope.

Key tables:

```text
weather_cache
weather_consensus
phrase_history
routing_outcomes
routing_preferences
notifications
notification_fts
events
tasks
email_messages
email_fts
files
file_fts
audit_log
```

## 7. Testable spine

Mongoose must pass these tests before personal connectors are added:

1. Health endpoint returns status and DB path.
2. Weather cache stores provider payload and returns a summary.
3. Weather phrase generation avoids overlong responses.
4. Home command attempts Home Assistant first when no learned exception exists.
5. Home command falls back to Alexa bridge after Home Assistant failure.
6. Private notification routes to a private surface and is not spoken.
7. Low-risk household notification may be spoken.
8. Calendar availability returns busy for inserted event blocks.
9. Email FTS search finds manually ingested test email.
10. File FTS search finds indexed local test document.

## 8. Exclusions

These are deliberately out of scope:

- automatic email sending;
- automatic purchasing;
- banking transaction import;
- unsupervised calendar modification;
- direct use of private Amazon APIs;
- always-on iOS notification scraping;
- legal/admin voice output through Alexa;
- device-control authority for every Home Assistant entity by default.

## 9. Roadmap

Later modules:

- spending awareness;
- household supply tracking;
- basket preparation;
- tightly permissioned purchasing;
- deeper semantic search over email/files;
- Android notification listener app;
- better voice satellites;
- full MCP server wrapper for Mongoose tools.
# Architecture

## Layer view

```text
Devices and surfaces
  iPads, Android tablets, Pixel Watch, Echo devices, browser, Open WebUI
        ↓
Input/routing layer
  Siri shortcuts, HA Assist, webhooks, Mongoose API
        ↓
Mongoose local core
  FastAPI, SQLite, adapters, persona, summarisation, routing, audit log
        ↓
Control and data planes
  Home Assistant, Ollama, ntfy, calendars, email, local files, weather APIs
        ↓
Explicit outputs
  HA actions, Alexa fallback bridge, ntfy pushes, dashboard cards, spoken summaries
```

## Why the local core exists

The local core prevents the assistant from becoming a pile of independent integrations. It owns:

- cached context;
- privacy classification;
- routing decisions;
- phrase selection;
- audit history;
- adaptive learning evidence;
- connector isolation.

The LLM is not the router. The LLM may help interpret language, but the local core makes policy decisions.

## Execution model

Mongoose can run in two ways:

1. Native Python on macOS. This is the simplest development path.
2. Docker Compose. This is useful for ntfy, Open WebUI, and optional service containers.

Recommended path:

```text
Ollama native on macOS
Mongoose API native Python
ntfy via Docker
Home Assistant as existing install or separate VM/container
Open WebUI optional via Docker profile
```

## Connector model

Connectors are adapters with explicit permissions. Each adapter must declare:

- source name;
- capabilities;
- read/write level;
- privacy level;
- timeout;
- error behaviour;
- audit events.

Adapters default to read-only except the Home Assistant action adapter, which requires explicit HA credentials and exposed entity IDs.

## Routing model

For home control:

```text
Resolve target
  ↓
Check learned preference for exact room/device/action
  ↓
Try preferred route if confidence high, otherwise Home Assistant
  ↓
Verify state if possible
  ↓
Try Alexa fallback if allowed and configured
  ↓
Log outcomes
  ↓
Update routing preference
```

For notifications:

```text
Ingest event
  ↓
Dedupe
  ↓
Classify urgency and privacy
  ↓
Choose destination
  ↓
Send via ntfy/HA/app/webhook where configured
  ↓
Record outcome
```

For weather:

```text
Scheduled refresh
  ↓
Fetch providers
  ↓
Normalise data
  ↓
Build consensus object
  ↓
Cache summary
  ↓
Generate concise spoken response
```

## Trust and safety posture

Mongoose must not have broad authority just because an LLM produced a plausible command. The policy layer is deterministic and auditable.

Sensitive data must stay on private surfaces by default:

- Mac dashboard;
- phone/watch notification;
- local browser session;
- explicitly authorised tablet.

Low-sensitivity household actions can use shared-room speakers.
# Security Model

## Principle

Mongoose should be useful without becoming an unsupervised raccoon with your credentials.

## Permission levels

```text
read_cache        Read local cache only
read_source       Read from configured external source
write_local       Write local cache, logs, and indexes
act_home          Perform smart-home action
send_private      Send private notification to private device
speak_shared      Speak low-sensitivity content in shared room
modify_external   Modify external system, disabled
purchase          Purchase goods, excluded
```

## Permissions

Enabled by default:

- read_cache;
- write_local;
- weather fetch;
- local file indexing only for explicitly provided paths;
- manual notification ingestion;
- local search.

Enabled only after credentials:

- Home Assistant actions;
- ntfy delivery;
- IMAP sync;
- Google Calendar sync;
- Microsoft Graph sync.

Disabled:

- email send;
- email delete/archive;
- calendar modification;
- purchase;
- bank transaction import;
- direct private-data speech through Alexa.

## Privacy classes

```text
low       weather, lights, household status, generic reminders
medium    calendar titles, normal email metadata, task titles
high      email bodies, legal/admin evidence, adult-health questions, work-sensitive content
secret    tokens, passwords, bank details, private therapy/client content
```

## Output rules

- Low privacy can go to room speakers if useful.
- Medium privacy goes to private surfaces by default.
- High privacy must not be spoken through Alexa or shared speakers.
- Secret content must not be spoken, logged in plaintext, or sent to third-party services.

## Credential handling

Use `.env` for local development only. Do not commit `.env`. Later, move secrets to the macOS Keychain or a local secret manager.

## Audit log

Every external action attempt must be written to `audit_log` with:

- timestamp;
- module;
- action;
- route;
- success/failure;
- reason;
- privacy class.

## MCP caution

MCP can be added later, but it should wrap the Mongoose API rather than bypass it. That keeps tools behind the local policy layer. Do not expose raw email, filesystem, calendar, or home-control tools directly to a model without Mongoose policy checks.
# Connector Implementation Notes

## Google Calendar

Implemented module: `mongoose.connectors.google_calendar`.

Command:

```bash
python -m mongoose.cli sync-google-calendar --days-ahead 14 --calendar-id primary
```

Requires:

```text
GOOGLE_CLIENT_SECRETS_PATH=./secrets/google-client-secrets.json
GOOGLE_TOKEN_PATH=./secrets/google-token.json
```

The connector uses read-only calendar scope and writes normalised events into the local `events` table.

## Gmail

Implemented module: `mongoose.connectors.gmail`.

Command:

```bash
python -m mongoose.cli sync-gmail --query 'newer_than:30d' --limit 100
```

Requires Google OAuth client secrets. The connector uses Gmail read-only scope and writes messages into the local email FTS index. Mongoose does not send, delete, archive, label, or mark messages.

## Microsoft Graph calendar

Implemented module: `mongoose.connectors.microsoft_graph`.

Command:

```bash
python -m mongoose.cli sync-ms-calendar --days-ahead 14
```

Requires:

```text
MS_CLIENT_ID=your_public_client_app_id
MS_TENANT_ID=common
```

For a work tenant, admin policy may block delegated consent. If so, use partial fallback methods such as busy/free exports, local Outlook-derived events, or notification-derived busy blocks.

## Microsoft To Do

Command:

```bash
python -m mongoose.cli sync-ms-todo
```

Tasks are written into the local `tasks` table. Mongoose does not create or complete tasks externally.

## Microsoft mail

Command:

```bash
python -m mongoose.cli sync-ms-mail --folder inbox --limit 100
```

Messages are indexed locally. Mongoose does not send, delete, archive, move, flag, or mark messages.

## IMAP and Proton Mail Bridge

Command:

```bash
python -m mongoose.cli sync-imap --mailbox INBOX --limit 50
```

For Proton Mail, run Proton Mail Bridge and use the local IMAP settings displayed in Bridge.

## Alexa fallback

Mongoose implements the Alexa fallback adapter as a bridge URL:

```text
ALEXA_FALLBACK_URL=http://your-bridge/command
```

The bridge is deliberately separate from Home Assistant so it can still work when HA fails. It must only be used for low-sensitivity household actions. Mongoose does not use private Amazon APIs by default.
# Requirements Traceability

| Conversation requirement | Mongoose implementation | QA note |
|---|---|---|
| Local LLM on M4 Mac mini | Ollama default, Open WebUI optional | Model layer is separate from policy/routing layer |
| Mongoose personality | `docs/PERSONA_CARD.md`, `src/mongoose/persona.py` | Keeps style separate from facts |
| Adult educational bluntness | Persona adult-health mode | Non-erotic, harm-reduction focused |
| Weather must be concise and varied | Weather phrase bank and rotation | Tests check short response length |
| Weather must prioritise precise APIs | Provider pool and cache | Open-Meteo default, optional minutely providers |
| Hourly UV | Weather normaliser extracts UV peak | Later providers can add richer hourly UV |
| Rain down to minutes | Only when minutely provider is present | Mongoose refuses fake minutely precision |
| Cache weather, not prompt-fetch | SQLite weather cache and consensus table | TTL-driven refresh |
| Home Assistant first | Home router default route | State verification attempted |
| Alexa fallback | Bridge adapter | Disabled unless configured, low privacy only |
| Adaptive route learning | `routing_outcomes`, `routing_preferences` | Explicit evidence, not LLM memory |
| Notifications from devices/sources | Notification ingest endpoint and FTS cache | Source APIs preferred, Android listener later |
| Reroute notifications | Privacy/urgency router and ntfy publisher | Private content not spoken |
| Google calendar | Google Calendar connector | Read-only OAuth |
| Outlook calendar | Microsoft Graph calendar connector | Subject to tenant policy |
| Work calendar blocked | Partial busy/free fallback in spec | No bypassing admin policy |
| Microsoft To Do | Microsoft Graph To Do connector | Read-only |
| Gmail/Outlook/IMAP | Gmail, Microsoft mail, IMAP connectors | Read-only |
| File search | Local file FTS index | Explicit folder paths only |
| Spending awareness later | Roadmap only | No banking |
| Ordering supplies later | Roadmap only | No purchases |
# Test Plan

## Test levels

### Unit tests

Test pure logic:

- weather summary selection;
- route preference update;
- notification classification;
- privacy surface selection;
- FTS insertion and search;
- calendar availability overlap.

### Integration tests

Test local service boundaries:

- FastAPI health endpoint;
- SQLite persistence;
- mocked Home Assistant call;
- mocked Alexa bridge call;
- mocked ntfy push;
- mocked weather provider.

### Manual acceptance tests

Test with real devices only after local logic passes:

- call a known Home Assistant entity;
- route a low-sensitivity notification to phone/watch;
- confirm private content does not go to Alexa;
- sync one IMAP mailbox with a small limit;
- index one test folder;
- test Siri Shortcut or Android voice webhook.

## Acceptance criteria

Mongoose is acceptable when:

- `/health` works;
- weather summary works from cache and refresh;
- home command logs HA attempt and fallback outcome;
- notifications are deduped and searchable;
- private notifications are not marked speakable;
- calendar availability returns expected busy blocks;
- IMAP/manual email search works;
- file search works;
- tests pass through `./scripts/qa_repo.sh`.

## Regression checklist

Before adding a new connector:

- Does it expose private content?
- Does it modify external systems?
- Can it be disabled through `.env`?
- Does it log errors without leaking tokens?
- Does it respect privacy class routing?
- Is it covered by a unit or integration test?
