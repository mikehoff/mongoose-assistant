# Bringing the whole system into chat , an elegant tool-layer design

## The seam already exists, so we extend it (do not parallel it)

Chat already has the right machine in `tools.py`:

- `TOOLS = name -> {executor, args, description}`  (the allowlist; the policy boundary)
- `READ_TOOLS`  : safe, read-only, can run freely
- `ACTING_TOOLS`: can change the world; blocked after a read tool unless the user's
  ORIGINAL prompt explicitly asked for it (the prompt-injection guard)
- `run_tool_turn(prompt)`: planner loop, one tool at a time, audited per step

The subsystems you asked about split **cleanly along the read/act line that already exists**:

- **Status queries** ("how's the scheduler?", "what's diverging?", "is HBO blocking on?",
  "what does the flat owe?") are READS. They are safe, high-value, and the 80% of what
  plain-language access is actually for.
- **Commands that mutate** ("apply the staged update", "rebuild the RTM pack",
  "run a hardening scan", "restart the scheduler") are ACTS.

So the design is two moves, no new mechanism:

1. Add a generous set of **read-only status tools** (just thin wrappers over functions that
   already exist: `doctor.system_status`, `self_harden.status`, `claim_consistency.run_builtin`,
   `scheduler.job_status`, `rtm_management.format_status/arrears`, `updater.list_update_stages`,
   benchmark results, DDNS state, HA reachability).
2. Add a **small** set of acting tools behind a NEW, stricter third tier `SENSITIVE_TOOLS`
   (state-mutating, never silently triggered, always confirmed), because `ACTING_TOOLS` today
   only contemplates home control and these are heavier.

## The three tiers (the whole policy, on one screen)

| Tier | Examples | Rule |
|---|---|---|
| `READ_TOOLS` | all `*_status`, `consistency_check`, `rtm_status`, `system_status` | run freely; safe to chain |
| `ACTING_TOOLS` | `control_home` | blocked after a read tool unless user explicitly asked (existing injection guard) |
| `SENSITIVE_TOOLS` (new) | `apply_update`, `run_hardening_scan`, `restart_scheduler`, `rebuild_rtm_pack` | NEVER auto-run: tool returns a *proposal* + a confirmation token; a second turn with explicit user "yes" actually executes. Inherits the same gates as the CLI. |

`SENSITIVE_TOOLS` deliberately reuses the CLI's existing approval gates (e.g. `apply_update_stage`
already requires an approved+checked stage and `--yes`). The chat tool does not get a private,
weaker path , it calls the same gated function. (This is the claim-consistency lesson applied to
permissions: one definition of "is this allowed", reused, not a second looser one in chat.)

## The status-query grammar is just one tool

Rather than 12 bespoke status tools, most status questions route through ONE tool,
`system_status`, that takes an optional `area`:

    system_status(area?: "all" | "scheduler" | "hardening" | "consistency" |
                  "updates" | "connectivity" | "rtm" | "benchmark" | "home")

- `area` omitted or "all" -> a compact health roll-up (doctor.system_status + a one-line
  summary per subsystem).
- `area="consistency"` -> claim_consistency.run_builtin() (who's diverging).
- `area="rtm"` -> rtm_management.format_status().
- etc.

This keeps the model's tool list short (short lists plan better) while covering every subsystem.
Bespoke tools are added only where the query is richer than a status read (e.g. `rtm_statement`
for a specific flat, `search_*` which already exist).

## Other plain-language capabilities worth adding (came to mind)

Read (safe, add freely):
- `system_status` (the roll-up above) , the single biggest win.
- `consistency_check` , "is anything making the same claim two different ways?" (the new module).
- `rtm_statement(flat)` / `rtm_arrears` , "what does flat 3 owe / who's behind?"
- `update_status` , "is there a staged update / is it safe to apply?" (list_update_stages + checks).
- `connectivity_status` , DDNS current IP + last sync + LAN-reachable + ntfy/searxng up.
- `benchmark_status` , latest local model benchmark + the second-lane surpriser pick.
- `recent_receipts` , "what have you done lately?" (receipts/audit; the receipt_truth claim).

Sensitive (proposal + confirm):
- `run_hardening_scan` , "check yourself" (scan_runtime; evidence-only, but heavier, so gated).
- `check_update`/`apply_update` , staged-update check (with runtime_smoke) then gated apply.
- `restart_scheduler` , bounce a wedged background loop.
- `rebuild_rtm_pack` , regenerate the compliance/export pack.

## Why this is elegant

- One registry, one planner, one audit trail , no second dispatch path.
- The read/act/sensitive tiers map exactly onto the safety distinction that already exists; the
  new tier is the minimal addition.
- Status is one tool with an `area` arg, so the model's choice space stays small and reliable.
- Sensitive tools call the SAME gated functions the CLI does , chat gets no weaker path.
- Everything is a thin wrapper over functions that already exist; almost no new logic, so almost
  no new bugs.
