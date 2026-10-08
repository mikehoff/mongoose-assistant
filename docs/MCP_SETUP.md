# Connecting Mongoose to Claude Code, Claude Desktop, and Home Assistant Assist

Mongoose has a shared message **bus** (briefs, tasks, alerts, replies, notes,
plus a shared `files/` surface) that other agents and you can read and write.
This guide wires up three surfaces. The bus lives on the Mac mini under
`data/bus/` and Mongoose owns it; everything else reaches in.

## The bus in 30 seconds

Each message is a human-readable Markdown file with YAML frontmatter, written
atomically (one immutable file per message, so concurrent writers never clash),
and indexed into SQLite/FTS so Mongoose can search and summarise.

```
data/bus/
  inbox/    unread messages (one .md each)
  archive/  read messages
  files/    shared attachments
```

CLI:
```
mongoose bus                         # list unread
mongoose bus --to claude_code        # filter
mongoose bus-send --from owner --to claude_code --type task --subject "Check boiler" --body "tripped overnight"
mongoose bus-read <id>               # read + archive
mongoose bus-search boiler
mongoose bus-reindex                 # rebuild index from files
```

HTTP (localhost by default; remote needs MONGOOSE_ALLOW_REMOTE + token):
`POST /bus/send`, `GET /bus/list`, `GET /bus/read/{id}`, `GET /bus/search`.

---

## 1. Claude Code and Claude Desktop (local MCP)

Mongoose ships an MCP server exposing the bus plus `control_home`, `get_weather`,
`check_availability`, `search_email`, `search_files`. It runs **locally over
stdio**, so nothing is exposed to the network: the local-first guarantee holds.

Install the SDK once (in Mongoose's venv):
```
pip install mcp
```

### Claude Desktop
Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "mongoose": {
      "command": "/Users/you/Downloads/mongoose-newer/mongoose/.venv/bin/python",
      "args": ["-m", "mongoose.mcp_server"],
      "env": {
        "MONGOOSE_DB_PATH": "/Users/you/Downloads/mongoose-newer/mongoose/data/mongoose.sqlite3",
        "PYTHONPATH": "/Users/you/Downloads/mongoose-newer/mongoose/src"
      }
    }
  }
}
```
Restart Claude Desktop. You'll see the Mongoose tools available. Now in Desktop
you can say "turn off the study desk lamp" or "leave Claude Code a task to refactor
the sync" and it goes through Mongoose (resolver, allowlist, audit, bus).

### Claude Code
```
claude mcp add mongoose -- /Users/you/Downloads/mongoose-newer/mongoose/.venv/bin/python -m mongoose.mcp_server
```
(Set the same `MONGOOSE_DB_PATH` / `PYTHONPATH` env if your shell doesn't already
have them.) Then Claude Code can read your tasks off the bus, drop you briefs,
share patch files into `data/bus/files/`, and control the home.

Note: local stdio MCP servers work in Claude Desktop and Claude Code, but NOT in
claude.ai in the browser (those require a public remote server, which would defeat
local-first). Use Desktop/Code for the private path.

---

## 2. Home Assistant Assist

Two integration directions; pick what you want.

### A. HA Assist -> Mongoose bus (simple, recommended first)
Lets you tell Assist things like "leave a note for Claude Code to check the
boiler" and have it land on the bus.

1. Copy `ha_integration/mongoose_bus/` to
   `<ha_config>/custom_components/mongoose_bus/`.
2. In `configuration.yaml`:
   ```yaml
   mongoose_bus:
     url: http://<mac-mini-ip>:8765        # Mongoose host:port
     token: !secret mongoose_api_token     # only if MONGOOSE_ALLOW_REMOTE=true
   ```
   For the Mac to accept LAN calls, set on the Mongoose side:
   `MONGOOSE_ALLOW_REMOTE=true` and `MONGOOSE_API_TOKEN=<something>` in `.env`,
   and serve on the LAN (`MONGOOSE_HOST=0.0.0.0`). Keep the token secret.
3. Restart HA. You now have a `mongoose_bus.send` service. Wire it to an Assist
   sentence or automation, e.g. a custom sentence "note for {agent} to {task}"
   that calls `mongoose_bus.send` with `to: "{{ agent }}"`, `subject: "{{ task }}"`.

### B. Mongoose as an Assist conversation agent (full version)
Have Mongoose's persona + memory + local model ANSWER Assist queries (voice or
app), with device control flowing through Mongoose's resolver. The HA component
is thin: it forwards each query to Mongoose's `/assist` endpoint and speaks back
the reply.

1. On the Mongoose side, allow LAN access (the Pi must reach the Mac):
   in `.env` set `MONGOOSE_ALLOW_REMOTE=true`, `MONGOOSE_API_TOKEN=<secret>`,
   `MONGOOSE_HOST=0.0.0.0`, and restart `mongoose serve`.
2. Copy `ha_integration/mongoose_conversation/` to
   `<ha_config>/custom_components/mongoose_conversation/`.
3. In `configuration.yaml`:
   ```yaml
   mongoose_conversation:
     url: http://<mac-mini-ip>:8765
     token: !secret mongoose_api_token
   ```
   Add `mongoose_api_token: <secret>` to `secrets.yaml`.
4. Restart HA. Go to Settings -> Voice assistants, create or edit a pipeline,
   and select **Mongoose** as the Conversation agent. Pick your STT/TTS as usual.
5. Talk to it. "What's on my calendar?" routes to Mongoose's memory/persona;
   "turn on the study desk lamp" routes through Mongoose's tool turn + resolver and
   actually controls the device. The reply is spoken back by your chosen TTS.

How routing works: `/assist` checks whether the text asks for home control. If
so it runs a Mongoose tool turn (resolver -> allowlist -> verify) and returns
`controlled: true`; otherwise it runs a persona+memory chat turn. All the
intelligence stays in Mongoose; the HA component only forwards and speaks.

Protocol note: the component implements HA's current conversation entity
interface (`ConversationEntity._async_handle_message`, `supported_languages="*"`,
`ConversationEntityFeature.CONTROL`, returning a `ConversationResult` with an
`IntentResponse`), verified against HA 2026.x developer docs.

Direction A (bus) and B (conversation agent) are independent; you can run either
or both. A is good for "leave a note for an agent"; B is for "talk to my house
with Mongoose's brain."

---

## File sharing ("network share")

The `data/bus/files/` folder is the shared surface.
- Claude Code / Desktop (on the Mac): read/write it directly, or via the
  `bus_share_file` MCP tool.
- Home Assistant (on the Pi): simplest is to POST through Mongoose; for a true
  mount, share `data/bus/files/` over SMB from macOS (System Settings -> General
  -> Sharing -> File Sharing) and mount it on the Pi. The HTTP path avoids SMB
  auth headaches and is the default recommendation.

## Security note

All bus HTTP routes are localhost-only unless you explicitly set
`MONGOOSE_ALLOW_REMOTE=true` + `MONGOOSE_API_TOKEN`. The MCP path is stdio-local
and never touches the network. Prefer the IP (not `homeassistant.local`/`.local`)
for cross-device URLs: mDNS resolution is unreliable from non-shell contexts.
