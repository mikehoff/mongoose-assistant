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
