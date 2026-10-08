# Model roles

Mongoose does not run one model for everything. It uses a different local model for different
jobs, so each job gets a model suited to it, and none of them competes with the others for the
Mac's memory unless it needs to. This page explains the roles, where each role's model comes
from, and how to change it.

You can see all of this live in the Cockpit: **System -> Models -> Model roles**. Each role
shows its purpose, the model actually in effect, whether that model is installed, and a badge
saying where the model came from.

## The four roles

| Role | What it does | Why it matters |
|------|--------------|----------------|
| **Chat** | Everyday conversation and your assistant's voice. The default for anything not routed to another role. | This is the model you talk to most, so it sets the feel and speed of Mongoose. |
| **Tools and actions** | Chooses which tool to run and reasons through a request (routing, planning, automations). | Decides whether an action is done correctly, so accuracy here protects your data and home. |
| **Code** | Writes and edits code. | Quality of any coding help. |
| **Research** | Longer multi-step research and synthesis. | Depth and coherence on bigger questions. |

Tools, Code, and Research **inherit the Chat model** unless you set one explicitly. So out of
the box you effectively run one good general model everywhere, and you only split roles when you
have a reason to (for example, a dedicated coding model).

## Where a role's model comes from (provenance)

For any role, the model actually in effect is resolved in this order, first match wins:

1. **Benchmark-tuned** (badge: gold). Mongoose benchmarks your installed models overnight
   (see [BENCHMARKING.md](BENCHMARKING.md)). When it finds a clearly better and safe setup it
   can apply a reversible override for that role. This never rewrites your `.env`: it is a
   separate override layer, so it can always be undone.
2. **Your setting** (badge: green). A model you pinned yourself in `.env` (see below). This is
   your explicit choice and Mongoose respects it.
3. **Inherits Chat** (badge: grey, Tools/Code/Research only). No explicit model for this role,
   so it uses your Chat model.
4. **Built-in default** (badge: grey). The shipped default. Chat defaults to a capable general
   model; Code defaults to a coding-specialised model.

The Cockpit shows the badge next to each role so you always know which of these is in play.

## How to change a role's model

There are three ways, from least to most manual:

- **Let Mongoose tune it.** Leave overnight benchmarking on. When it has evidence, it either
  applies a better model automatically (only when the win is large and safe) or suggests one you
  can apply with one tap. Both are reversible from the same card ("revert" drops the override
  and returns to your base).
- **Pin it in `.env`.** Set the variable for the role and restart Mongoose:
  - `OLLAMA_CHAT_MODEL`
  - `OLLAMA_TOOL_MODEL`
  - `OLLAMA_CODE_MODEL`
  - `OLLAMA_RESEARCH_MODEL`
  A pinned value shows as "Your setting" and wins over defaults (a benchmark override still wins
  over it, because you asked Mongoose to tune; revert the override to get your pin back).
- **Ask in chat.** You can ask Mongoose about your current setup and ask it to apply a pending
  benchmark recommendation.

## Common gotcha: a role pointed at a model you have not pulled

A role can name a model that is not installed on this machine (for example, the shipped Code
default names a coding model you may never have pulled). Ollama then cannot serve that role and
it falls back or fails. The Model roles card flags this with a red **not installed** marker next
to the model name. Fix it by either pulling that model (`ollama pull <model>`) or pinning the
role to a model you do have via its `.env` variable.

## Related

- [BENCHMARKING.md](BENCHMARKING.md) , how the overnight benchmark (Lane 2) evaluates models and
  proposes better setups.
- [CONFIGURATION.md](CONFIGURATION.md) , all `.env` settings.
