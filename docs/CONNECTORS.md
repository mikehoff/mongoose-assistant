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
