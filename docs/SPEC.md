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
- The tool/reasoning role defaults to the resident chat model (Gemma 4 26B-A4B) to avoid a second model load on 24GB. gpt-oss:20b is an optional override, not the default.
- Qwen2.5 Coder 14B Q4_K_M remains the practical coding model.
- `OLLAMA_KEEP_ALIVE=-1` keeps the resident model loaded permanently (no cold starts). Set `0` or `10m` to free memory between calls.

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
