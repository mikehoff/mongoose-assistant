# Configuration

Mongoose reads runtime configuration from environment variables, normally loaded from `.env`. `configs/mongoose.yaml.example` is documentation-only and should be kept in sync with `.env.example`; it is not parsed by the code.

## Core runtime

```env
MONGOOSE_DB_PATH=./data/mongoose.sqlite3
MONGOOSE_HOST=127.0.0.1
MONGOOSE_PORT=8765
MONGOOSE_TIMEZONE=Europe/London
MONGOOSE_HOME_LAT=51.5074
MONGOOSE_HOME_LON=-0.1278
```

## Local model runtime

Current base-M4 24 GB defaults:

```env
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_CHAT_MODEL=gemma4:26b-a4b-it-q4_K_M
# OLLAMA_TOOL_MODEL defaults to the resident chat model (Gemma) to avoid swap
# thrash on 24GB. gpt-oss:20b is an optional override, not the default:
# OLLAMA_TOOL_MODEL=gpt-oss:20b
OLLAMA_CODE_MODEL=qwen2.5-coder:14b-instruct-q4_K_M
OLLAMA_KEEP_ALIVE=-1
```

`OLLAMA_KEEP_ALIVE=-1` keeps the resident model loaded permanently (no cold starts) and pins ~17GB on a 24GB machine. By default the tool role reuses the resident chat model, so only one model is resident at a time; set `0` or `10m` to free memory between calls at the cost of reload latency.

`OLLAMA_THINK=false` is the default. Gemma 4 and some other models support a "thinking" mode that writes reasoning into a separate `thinking` field and can leave `content` empty, which makes replies look silent and run slower. With thinking off, replies are faster and the answer reliably lands in `content`. Mongoose also falls back to the `thinking` field if `content` is ever empty, so it never returns silence even when thinking is on. Set `OLLAMA_THINK=true` if you want deeper reasoning on hard questions and accept slower, sometimes-quieter replies.

## Cloud escalation

Cloud escalation is off by default:

```env
CLOUD_ESCALATION_ENABLED=false
CLOUD_ESCALATION_ORDER=anthropic,openai,google
CLOUD_TURN_BUDGET_PENNIES=2.0
CLOUD_MAX_OUTPUT_TOKENS=600
```

Set the relevant API keys only when you want opt-in escalation. The default order honours Claude-first; set `CLOUD_ESCALATION_ORDER=google,anthropic,openai` for cheapest-first.

## Smart home

```env
HA_URL=
HA_TOKEN=
ALEXA_FALLBACK_URL=
ALEXA_FALLBACK_TOKEN=
```

Home Assistant is the primary control plane. Alexa is only a fallback bridge for low-sensitivity household actions.

## Connectors

Connector credentials are optional and read-first. Missing credentials should not prevent the local server, `mongoose doctor`, weather, notifications, calendar cache or file indexing from running.

## Conversation memory and long-term facts

Mongoose remembers context in two ways, both stored locally in SQLite:

Short-term (within a conversation): each `mongoose chat` turn is saved and the
recent history is replayed into the model on the next turn. The CLI uses one
persistent thread by default, so back-to-back chats remember each other. Use
`--conversation <id>` for separate threads, `--no-memory` to disable for a turn,
and `mongoose new-conversation` to clear the default thread. Over the API, pass
`conversation_id` in the `/llm/chat` body, and `POST /conversation/{id}/clear`.

Long-term (across all chats): durable facts are stored and injected into the
system prompt when relevant. Say "remember that ..." in a chat, or use
`mongoose remember "..."`. Facts are also auto-extracted from high-confidence
patterns (e.g. "my name is ..."). Manage them with `mongoose memory` (list),
`mongoose forget "..."`, or the `/memory` GET/POST/DELETE endpoints. Everything
is local; nothing leaves the machine.

## Guided setup (wizard and browser)

Two ways to connect Home Assistant, Alexa, Google/Microsoft calendar and tasks,
email (IMAP), and the cloud LLM providers, instead of editing `.env` by hand:

Terminal wizard: `mongoose setup` walks each integration, shows where to get
each credential, writes your answers to `.env`, and verifies live where it can
(e.g. it actually pings Home Assistant and the LLM APIs). Secrets are entered
hidden. `mongoose setup --status` prints the current state as JSON.

Browser page: start the server (`mongoose serve`) and open
`http://127.0.0.1:8765/setup`. Each integration is a card with instructions and
a save-and-verify button. Values are written to your local `.env`; nothing is
sent anywhere except the service being verified. After saving, reload the API
(or restart) for changes to take effect, and run `mongoose doctor` to confirm.

## Updating Mongoose

To move from an older version to a newer one without losing your config or
memory, from inside your current install run:

    ./scripts/update.sh /path/to/new-version-folder-or-zip
    # or, avoiding shell quirks:
    mongoose update /path/to/new-version.zip

The updater backs up your whole install first, replaces only code (src, scripts,
docs, configs templates, pyproject), and never touches `.env`, `data/` (your
database and memory), or `secrets/`. It reinstalls dependencies, runs the
idempotent database migration (which only adds new tables), and verifies with
`mongoose doctor`. The backup path is printed so you can roll back if needed.

## Model-assisted memory

Auto-extraction now runs on the resident chat model (no model swap) and only on messages that plausibly state a durable fact (a cheap regex pre-filter), so ordinary turns stay fast and only self-disclosing turns pay the extra inference. It is on by default. Beyond that (e.g. "my name is X"), you can let the
local model propose durable facts from a piece of text, then confirm each before
it is stored:

    mongoose remember-from "I walked my dog Rex around the park today"

It proposes facts and asks y/N for each (use --yes to store all). Proposed facts
are never stored without confirmation, so model guesses don't pollute memory.

## Updated defaults (v0.6.2)

The shipped defaults now reflect what works well in real use on a 24GB base M4:
- OLLAMA_KEEP_ALIVE=10m (keeps a model warm for snappy back-to-back chat). Set
  to 0 if you ever hit memory thrash during chat/tool role switches.
- OLLAMA_THINK=false (faster, no empty-content silences).
- MEMORY_AUTO_EXTRACT=true (the model proposes durable facts after chat turns and
  stores high-confidence ones automatically; the CLI `remember-from` stays
  confirmation-gated).

## Update behaviour: new keys and rollback

The updater preserves your `.env` but a new version may introduce new settings.
It auto-merges those: any key present in the new `.env.example` but missing from
your `.env` is appended (with its comment and default), and your existing values
are never overwritten. Run it standalone any time with `mongoose merge-env`.

Rollback: every update makes a timestamped backup of the whole install. Restore
the most recent one with `./scripts/update.sh --rollback` (it saves your current
state first), or list them with `./scripts/update.sh --list-backups`.

## Completing Google / Microsoft sign-in (OAuth)

Calendar, Tasks, and Gmail use OAuth, which needs a one-time consent. You can
complete it during setup instead of waiting for the first sync:

- In `mongoose setup`, after entering Google/Microsoft details, it offers to
  authorise now (Google opens a browser; Microsoft shows a device code + URL).
- Or run it directly: `mongoose authorize google` / `mongoose authorize microsoft`.
- Or use the "Authorise" button on the browser setup page (note: consent runs on
  the machine hosting Mongoose, so the browser window / device code appears on
  the Mac mini).

Once authorised, the token is stored locally and the connectors use it silently.
