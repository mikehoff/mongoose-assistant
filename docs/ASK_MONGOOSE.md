# The three "ask Mongoose" surfaces: what works and how to turn it on

Run `mongoose doctor` and look at the `ask_mongoose` block: it tells you the
readiness of each surface and exactly what's missing.

## 1. "Hey Mongoose" via Home Assistant Assist  (WORKS, needs LAN config)

HA's Assist forwards your spoken/typed request to Mongoose's `/assist` endpoint,
which routes home-control to a tool turn and everything else to a persona+memory
chat turn, then speaks the reply back through HA. The intelligence stays on the
Mac; the HA component is thin.

Because the Pi reaches the Mac over the LAN, and `/assist` is a private route,
you must open remote access (it's closed by default):

On the Mac (.env), then restart `mongoose serve`:
```
MONGOOSE_HOST=0.0.0.0
MONGOOSE_ALLOW_REMOTE=true
MONGOOSE_API_TOKEN=<a long random string>
```
In Home Assistant, install the `mongoose_conversation` component and configure it
with the Mac's URL and the SAME token:
```
url: http://<mac-ip>:8765
token: <the same MONGOOSE_API_TOKEN>
```
Then set it as a conversation agent in HA's Assist pipeline. Without the token (or
with `ALLOW_REMOTE` off), HA gets a 403, this is the most common failure, and
`mongoose doctor` now flags it.

## 2. "Ask Mongoose" from Claude  (WORKS, local, no network)

Claude Desktop and Claude Code can call Mongoose's tools (the bus, home control,
weather, availability, email/file search) via a local MCP server over stdio,
nothing is exposed to the network. Register it in Claude Desktop/Code's MCP config
(see `MCP_SETUP.md`). Then in Claude you can send Mongoose a bus message, ask it to
check your calendar, search your files, etc.

Note: this is Claude DESKTOP / CODE, not claude.ai in a browser. A browser
connector would require exposing Mongoose publicly through Anthropic's cloud, which
is declined on principle (local-first). The local MCP path keeps everything on your
machine.

## 3. "Alexa, ask Mongoose"  (NOT SUPPORTED, by design)

You cannot say "Alexa, ask Mongoose..." to an Echo. That would require publishing
an Alexa skill to Amazon and hosting an endpoint Amazon's cloud calls, routing your
voice through Amazon's servers, which is exactly what local-first refuses. Alexa is
only an OUTPUT fallback: Mongoose can speak a result through an Alexa speaker (gated
by privacy so it never reads private content aloud), but Alexa is never an input
path. If you want hands-free voice INTO Mongoose, use `mongoose voice --session` on
the Mac, or HA Assist (surface 1) through an HA-connected mic/speaker.
