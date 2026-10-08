# Benchmarking models on your machine

`mongoose benchmark` measures candidate models on YOUR Mac mini against YOUR real
tasks, so the choice of resident model is "what my machine actually does" rather
than "what a leaderboard says." Everything runs locally; no data leaves the machine.

## How to run it

```
# Benchmark the built-in shortlist (auto-downloads any that are missing)
mongoose benchmark

# Benchmark specific models
mongoose benchmark gemma4:26b-a4b-it-q4_K_M qwen3:30b-a3b

# Faster: skip the cold-load measurement
mongoose benchmark --no-cold

# Do NOT auto-download missing models (error on them instead)
mongoose benchmark --no-pull
```

That's it. By default it will **automatically download (pull) any model in the list
that you don't already have**, streaming progress, before running. Use `--no-pull`
if you'd rather pull manually (via `scripts/pull_benchmark_models.sh` or
`ollama pull`) and have the run skip anything missing.

The default shortlist is the handful of models that fit a 24GB base M4 and run in
Ollama; pulling all of them is a large download (~60GB total), and they exist only
so the benchmark can compare them. You don't need them to run Mongoose.

## What it measures

For each model:
- **cold_load**: seconds for the first call after the model is evicted from memory
  (the "I haven't used it in a while" delay). Skip with `--no-cold`.
- **warm_med**: median latency of short conversational calls once resident.
- **tool_ok**: of several home-control prompts, how many produce a valid
  `control_home` plan (structured tool-calling reliability).
- **tasks**: how many of the REALISTIC use-case tasks passed (see below).
- **task_med**: median latency across those tasks.
- **mem**: whether memory extraction returns clean JSON facts.

### The realistic tasks

These are modelled on real Mongoose workflows, not synthetic prompts, and each has
a scorer that judges whether the answer is actually usable:

- **home_control**: "turn off the lounge lights and the study floor lamp" -> must
  produce a valid control_home plan.
- **find_intent**: "find the service charge certificate the managing agent sent" ->
  must route to a search/find tool.
- **ha_automation_review**: a conflicting-automations question -> must identify the
  conflict.
- **draft_correspondence**: draft an RTM handover note -> must include the key dates.
- **memory_extract**: a self-disclosure -> must return JSON facts.
- **brief_phrasing**: phrase a brief from confirmed signals -> must reference the
  real signals without inventing.

The output includes a per-task breakdown so you can see exactly WHICH task a model
fails, e.g. a model might be fast and chatty but fail `find_intent`, which matters
more for how you actually use Mongoose than a generic latency number.

## Like-for-like methodology (how it stays fair)

The benchmark's whole job is a fair comparison, so it actively controls the things
that otherwise make model-vs-model numbers meaningless:

- **Warm-start vs cold-start contamination.** The first timed call after a model
  loads pays a one-off warm-up tax (KV cache, first-prompt overhead) that later
  calls don't. Before EVERY timed phase, the benchmark issues discarded warm-up
  calls under that phase's own system prompt, so every phase starts warm and no
  single call is unfairly slow. Cold load is measured separately on purpose
  (evict, then time), repeated several times because a cold start is itself
  variable, and reported as a median.
- **Single-sample noise.** One timing is one draw from a noisy distribution. Each
  measurement is repeated (`--repeats`, default 3) and reported as **median +/- IQR**
  (inter-quartile range). A large spread means the samples were noisy, so you treat
  a small gap between models as a tie, not a difference.
- **Thermal drift and order effects.** A small-chassis Mac throttles under sustained
  load, so whoever runs last would look slower. Run multiple **interleaved rounds**
  (`--rounds`, round-robin) so heat is shared across models, with a **cooldown**
  between models (`--cooldown`) to shed heat. Results pool across rounds; pass/fail
  takes the majority so one flaky round can't flip a verdict.

For a real decision:
```
mongoose benchmark --rounds 3 --repeats 5
```

### What it does NOT fully control (stated honestly)

No on-device benchmark removes every confound, and the run prints these up front
rather than hiding them: sustained-load thermal throttling on a small/fanless
chassis; other processes competing for RAM/GPU; disk-cache state affecting cold
load; and Ollama-version differences. The practical advice the tool gives you:
close other apps, run at least 2 rounds, and **trust the ranking more than the
absolute milliseconds**.

## Reading the result

Lower latency is better; higher `tasks`, `tool_ok`, and `mem=Y` are better. The
benchmark does not pick for you, it prints the numbers and you decide the trade-off
(a slightly slower model that passes more of your real tasks is usually the right
resident choice). All numbers are from your machine, on your tasks.

## Tests

The benchmark's task scorers and the auto-pull are covered by fully-mocked tests
(`tests/test_benchmark_realistic.py`), so the logic is verified without a live model
or real downloads.
