# Changelog

Mongoose versions are `1.MINOR.PATCH`, codename `sillygoose`. Detailed design history from earlier
development lives in [docs/history/](docs/history/).

## 1.53.361 (first public-ready release)

### New
- **Ops health that pushes, not just logs.** Failing backups, stalled jobs and similar silent failures
  now raise a notification to the owner instead of only appearing in a log.
- **Document totals with evidence.** A document's total is shown with where it came from, and
  withheld rather than guessed when the evidence is weak.
- **RTM: overdue notices.** One stage decision rendered three ways (Word letter, portal text, chaser
  email), so the surfaces can never disagree about what is overdue.
- **RTM: scheduled half-year demands.** Demand runs follow the lease's two on-account instalments.
- **RTM: registers.** Building profile, insurance, fire-risk and compliance registers, lease terms.
- **RTM: seller packs (LPE1).** Drafts every field from live data with provenance, then a two-stage
  response that matches how a careful director replies before any fee is paid.
- **RTM: accountant export.** One engine assembles the figures once (journal, bank transactions,
  budget against actual, fund positions, arrears) and renders them in several formats.
- **Leak gate.** `tools/leak_gate.py` and a pre-commit hook stop personal data from being committed:
  private term list, identifier patterns, and human review of any line about a person's body,
  health, mind, family or routine.

### Improved
- Backup disk-space guard and stronger brain preservation (the live database refuses test writes).
- Calendar and task context, chat actions and intent routing for payments and receipts.
- Correspondence copies the same parties consistently on every letter type.
- MCP server: many more tools for structured, read-first access.
- Cockpit deep links no longer double-load; chat no longer shows a duplicate reply bubble.

### Privacy
- All personal and third-party data has been removed from source, tests and docs. Packaged examples
  use fictional people, places and organisations.
