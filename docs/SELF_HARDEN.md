# Self-Harden Foundation

Self-Harden is Mongoose's internal quality loop. It does **not** patch code or install updates by itself. It observes failures, maps them to user-facing promises, exports review packets, and creates approval-gated build plans.

## Core idea

A bug is not just an exception. It is often a broken claim:

- "configured" when the source is not usable
- "ready" when the running process is stale
- "local-first" when private summaries leave through a public channel
- "sent" when the send failed
- "forgotten" when the memory still exists

Self-Harden records those claim failures as observations and turns them into reproducible hardening work.

## Main commands

```bash
mongoose harden status
mongoose harden claims
mongoose harden scan
mongoose harden recent
mongoose harden observe "Setup says configured from placeholder" --claim configured_truth --severity P0
mongoose harden module-index --rebuild
mongoose harden export data/hardening_packets/packet-01
mongoose harden plan <observation_id>
```

## Scheduler integration

The scheduler includes `self_harden_scan`, a cheap local scan that records observations from scheduler errors, watchdog signals and failed audit-log actions. It does not call live connectors, edit code, send data, or run builds.

## Review packet

`mongoose harden export` writes:

- `hardening_packet.md`
- `hardening_packet.json`

These are designed to be uploaded to Claude/OpenAI or reviewed locally. They contain claim registry, observations, recent runs and module index samples. They deliberately do not include `.env` secrets.

## Build-plan integration

`mongoose harden plan <observation_id>` creates an approval-gated build plan:

1. reproduce the claim failure with a focused regression test;
2. patch the smallest safe seam;
3. run relevant gates and write an audit note.

The plan is not auto-approved and does not run until explicitly approved.

## Non-goals

Self-Harden is not a self-updater yet. It does not:

- modify live code;
- install dependencies;
- restart Mongoose;
- send reports automatically;
- call cloud models directly;
- bypass human approval.

The safe progression is: observe -> packet -> candidate plan -> gated build -> staged updater.

## v0.54 safe sequence: reingest, review, staged update

The self-hardening loop now has three safe follow-on surfaces.

### Code reingest

`mongoose harden reingest [OUT_DIR]` rebuilds the module map and writes a code-reingest packet. It maps modules to commands, routes, settings, database tables, scheduler jobs, imports, owner area, tests and claim/invariant families. This is still read-only: it indexes the codebase so future hardening plans know which module owns a promise before any patch is proposed.

### Reviewer packets

`mongoose harden review-packet [OBSERVATION_ID] --reviewer claude|openai|local` writes a packet for an external or local reviewer. It includes the observation, the relevant claim/invariant, related modules and a strict response format. It does not call an API. A reviewer proposes diagnostics, failing tests and minimal patch plans; Mongoose remains the source of truth and still runs gates.

### Staged updater

`mongoose update --stage PATH_OR_ZIP` extracts or copies a candidate into `data/update_stages/` and verifies its manifest/version without applying it. Then use:

```bash
mongoose update --stages
mongoose update --check-stage <stage_id>
mongoose update --approve-stage <stage_id>
mongoose update --apply-stage <stage_id> --yes
```

The staged updater never overwrites the live tree merely because a candidate exists. Apply uses the existing backup/rollback updater and refuses unless the stage was approved and its checks passed. This is the safe floor for a later self-updater.
