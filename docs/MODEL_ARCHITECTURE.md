# Model architecture: one speaker, silent workers

Mongoose runs local models one (or two) at a time on a 24GB base M4. Three design
notes converged on a single principle, and this is what Mongoose now enforces:

> Only the conversation owner speaks to the user. Other models are silent workers
> that return structured evidence, which the owner phrases.

This protects single-conversation coherence. If two models took turns producing
final user-facing prose in one thread, you would get drift: different tone, different
weighting of the same reference, subtly reinterpreted assumptions. So Mongoose has no
"any model may answer" mode. There is one owner; everything else works behind it.

## Roles

- `chat` / conversation owner: the ONE model that speaks. Default
  `gemma4:26b-a4b-it-q4_K_M`, the best all-rounder on the task benchmark.
- `research` worker: a silent specialist (e.g. `qwen3.5:9b`) that searches/analyses
  and returns JSON notes (findings, sources, confidence, gaps). It never addresses
  the user. The owner turns its notes into the reply.
- `tool`: tool-call planning (JSON), defaults to the chat model.
- `code`: coding worker, on demand.

## Config (env)

```
MONGOOSE_CONVERSATION_OWNER=gemma4:26b-a4b-it-q4_K_M   # who speaks
OLLAMA_RESEARCH_MODEL=qwen3.5:9b                       # silent worker (default: =chat)
MONGOOSE_BIG_FALLBACK=gemma4:26b-a4b-it-q4_K_M         # high-stakes synthesis
MONGOOSE_SPEAKER_POLICY=owner_only                     # enforced; the only mode
```

Defaults change nothing: owner, research and tool all resolve to Gemma 26B until you
deliberately set a separate worker. So a stock install is exactly today's behaviour.

## The three viable configurations

- Big Gemma only (default): simplest, most coherent, best all-round benchmark result.
- Big Gemma owner + `qwen3.5:9b` silent research worker: reliability-first; adds a
  research brain without touching the voice.
- `gemma4:e2b` owner + `qwen3.5:9b` worker, Gemma 26B fallback: the speed experiment;
  a snappy speaker with a silent research brain. Caveat: e2b dropped tasks
  (correspondence, home_control) on the benchmark, so keep the big fallback for
  high-stakes synthesis.

## Residency is measured, not assumed

The pair architecture is only real if both models actually STAY loaded together on
your machine. That cannot be inferred from weight sizes; it depends on weights + KV
cache + Ollama + macOS + Mongoose + Docker/WAHA all sharing 24GB. So:

```
mongoose doctor --models
```

probes Ollama `/api/ps` and reports `yes` / `no` / `unknown`. It reports `unknown`
when Ollama is unreachable, and never guesses `yes`. The trustworthy test:

```
ollama run gemma4:e2b "say ready"
ollama run qwen3.5:9b "say ready"
ollama run gemma4:e2b "one word: e2b"
ollama run qwen3.5:9b "one word: qwen"
ollama ps          # are BOTH still listed after alternating?
mongoose doctor --models
```

If `doctor --models` says `yes` after alternating turns, the pair holds and the
e2b+Qwen architecture is worth using. If it says `no`, keep a single owner: a pair
that evicts under pressure gives you routing complexity AND cold-load stalls, the
worst of both.

## What is deliberately NOT built yet

A turn-by-turn router that decides per message which worker to invoke, and a two-stage
"worker drafts, owner phrases" pipeline wired into the chat loop. Those are worth
adding once residency is confirmed on the Mac. Until then this release ships the
enforceable parts: the owner-only guarantee, the role/config surface, the silent
worker that returns evidence, and the honest residency probe.

## The worker is a TOOL, not a second agent (prior art)

The mature pattern for this (LangGraph/LlamaIndex supervisor + specialist agents
exposed as tools, MCP-style tool contracts) is: one supervisor that speaks, specialists
called like tools, structured handoffs, one final speaker. Mongoose implements the
shape NATIVELY rather than adopting a framework (local-first, low overhead): the
research worker is registered in the normal tool catalogue as a read-only `research`
tool. The conversation owner's existing tool-planning loop calls it like any other
tool, gets structured notes back, and phrases the reply. Because it's a READ tool it
participates in the same acting-tool guard as file/email search, so it can never be a
backdoor to sending or acting. This deliberately avoids the multi-agent group-chat
shape (researcher/planner/critic all speaking), which is exactly the drift to avoid.

## Web search in the research worker (SearXNG, opt-in)

The `research` tool retrieves LOCAL sources first (files/attachments/email). If local
is thin (< 2 hits) and SearXNG is configured, it augments with web snippets. Web is a
fallback, not a default: a well-answered local query never touches the network.

Enable by self-hosting SearXNG and setting:

```
MONGOOSE_SEARXNG_URL=http://192.168.1.50:8080   # your LAN instance
```

Then in SearXNG's settings.yml enable the JSON API (it's off by default):

```
search:
  formats:
    - html
    - json
```

Best-practice/safety notes:
- Opt-in: empty default, no public instance is assumed (avoids leaking queries).
- Snippets only: SearXNG's `content` field; no result pages are fetched.
- Network guard: LAN/private URLs are allowed over http because you set them
  explicitly; public hosts must be https. Redirects are not followed.
- Grounded: web results become real source records (title, url, snippet) cited by
  index, same as local sources; `used_web` and `grounded` flags show provenance.
- `mongoose doctor` reports whether web search is configured.
