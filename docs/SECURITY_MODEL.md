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

## Tool execution (agentic tool turns)

When `/llm/chat` resolves a turn to the `tool` role, Mongoose runs a
plan -> validate -> execute -> phrase loop:

1. The tool model proposes a single JSON tool call.
2. Mongoose validates the tool name against a fixed allowlist in `tools.py`.
   A name not on the list is rejected and the turn degrades to a normal
   persona reply. The model cannot invent or reach tools that are not
   registered.
3. The executor runs against the existing in-process service functions.
4. The persona model phrases the raw result; it is told to state only what
   the result supports.

The allowlist is the policy boundary. It exposes read-only search over email,
files and notifications, read-only calendar availability, weather, and exactly
one acting tool: `control_home`. There is no email send/delete/archive, no
calendar write, no purchase. Home control inherits the existing home_router
privacy gate, so Alexa fallback stays blocked for non-low-privacy commands
even when the model requests it. Every plan and execution is written to
`audit_log`.

### Multi-step tool chaining

A tool turn may now call more than one tool, seeing each result before the
next call. This adds capability (e.g. check availability, then act) but is
bounded by hard guardrails that the model cannot override:

- `MAX_STEPS` (default 4): the loop stops after this many calls regardless of
  what the model wants, so a confused or adversarial plan cannot run away.
- Dedupe: the exact same (tool, args) call cannot run twice in one turn.
- Allowlist still applies on every step; an unknown tool stops the loop.
- Every step is written to `audit_log` with its step number and args.

The response includes a `steps` list and a `stop_reason`
(`model_finished`, `max_steps`, `repeat_call`, or `rejected_tool`) so a surface
can show exactly what ran. There is still no destructive tool available, so
chaining cannot compose its way to send, delete, or purchase. Acting is limited
to `control_home`, which keeps its privacy gate on every call.

## Cloud escalation and the per-turn cost cap

Mongoose is local-first. A turn only reaches a paid API when both are true:
the request sets `allow_cloud=true`, and the local reply looks uncertain
(e.g. "I don't know"). Escalation order is Anthropic instant (Haiku) first,
then OpenAI nano / configured instant fallback.

The cost guarantee is enforced before any call, not by trusting the model:

- Each call's worst-case cost is estimated up front as full input tokens plus
  the capped `CLOUD_MAX_OUTPUT_TOKENS` of output, priced from a local table.
- The estimate is checked against the remaining per-turn budget
  (`CLOUD_TURN_BUDGET_PENNIES`, default 2p). If it does not fit, that provider
  is skipped; if neither fits, no cloud call happens and the local reply stands.
- The budget is charged the worst-case estimate, not the actual usage, so the
  turn can never silently exceed the cap even if a response is longer than
  expected. Output is hard-capped via max_tokens so the ceiling is real.
- Token estimates deliberately over-count, and unknown model ids price at a
  conservative default, so the gate always errs toward refusing.

At current pricing a typical 2k-input/600-output Haiku turn costs about 0.4p,
well inside the 2p cap; the cap mainly guards against runaway long-context
calls. Every cloud call, skip, and failure is written to `audit_log`.
