from __future__ import annotations

from typing import Any

from .. import profile as _profile


# Plan names and the WordPress file paths below carry the site's RTM slug, and step tasks name the RTM
# and its mailbox, so a plan is created with the install's own identity. The step lists keep
# {{placeholders}} (filled from the site profile when a plan is created); plan_name() is the one place a
# persisted plan name is built.
def plan_name(kind: str) -> str:
    """Persisted build-plan name for this install: '<rtm slug>-<kind>', e.g. '<slug>-management'."""
    return f"{_profile.rtm_slug()}-{kind}"


def _reserve_fraction_example() -> str:
    """'reserve / sch1 total = fraction' from the RTM property budget, for the trust plan's maths step."""
    try:
        from .rtm_example_court import load_property
        budget = load_property().get("budget", [])
        sch1 = sum(float(b["annual"]) for b in budget if str(b.get("schedule")) == "sch1")
        reserve = sum(float(b["annual"]) for b in budget
                      if str(b.get("schedule")) == "sch1" and "reserve" in str(b.get("label", "")).lower())
        if sch1 and reserve:
            def money(x: float) -> str:
                return str(int(x)) if float(x).is_integer() else f"{x:.2f}"
            return f"{money(reserve)}/{money(sch1)} = {reserve / sch1:.5f}"
    except Exception:
        pass
    return "reserve_annual/sch1_total"


def _for_this_site(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from ..settings import render_profile_copy
    extra = {"{{RESERVE_FRACTION_EXAMPLE}}": _reserve_fraction_example()}

    def walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return render_profile_copy(obj, extra=extra)
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        return obj
    return walk(steps)

# The Example Court leaseholder-maintenance triage system, decomposed from the
# EPA spec into independently-checkable build steps for the overnight runner.
#
# This is PRODUCTION-INTENT and triages real safety-critical requests, so the
# decomposition is deliberately conservative:
#   - the safety rules engine (Layer 1) comes FIRST and is the most heavily
#     tested; nothing depends on the ML model for safety routing;
#   - every step has a concrete check (tests pass / file exists / endpoint
#     responds), so a step only counts as done when it demonstrably works;
#   - the ML model trains on SYNTHETIC data in this plan. Real leaseholder data
#     requires the DPIA, anonymisation sign-off, and a human gate that an
#     overnight run must NOT bypass. Training on real data is intentionally out
#     of scope for the autonomous build.
#
# Each step's `task` is the instruction handed to Claude Code. Each `check` runs
# in the repo dir and must exit 0.

EXAMPLE_COURT_STEPS: list[dict[str, Any]] = [
    {
        "id": "01-scaffold",
        "title": "Project scaffold + tooling",
        "task": ("Create a Python 3.12 project for an RTM maintenance-request triage "
                 "service. Set up pyproject.toml (package name 'pc_triage'), a src/ layout, "
                 "pytest, ruff, and a Makefile with 'test' and 'lint' targets. Add a README "
                 "stating this triages real leaseholder requests and that the safety layer is "
                 "deterministic. No application logic yet."),
        "verify": {"type": "file_exists", "path": "pyproject.toml"},
        "retries": 1,
    },
    {
        "id": "02-safety-rules",
        "title": "Layer 1 deterministic safety rules engine",
        "task": ("Implement src/pc_triage/safety_rules.py: a deterministic safety-critical "
                 "classifier using regex pattern matching WITH negation detection, covering "
                 "gas, flooding, structural cracking, lift entrapment, fire-door obstruction, "
                 "and asbestos (RICS emergency category). It must return the matched rule as a "
                 "citable reason. No ML. Write thorough tests in tests/test_safety_rules.py "
                 "including negation cases (e.g. 'no gas smell') and each hazard category. "
                 "This is the most safety-critical component: aim for exhaustive tests."),
        "verify": {"type": "pytest", "target": "tests/test_safety_rules.py"},
        "depends_on": ["01-scaffold"],
        "retries": 2,
    },
    {
        "id": "03-data-pipeline",
        "title": "Data pipeline on synthetic data (anonymise, dedupe, quality asserts)",
        "task": ("Implement src/pc_triage/data.py: load request records, strip PII (regex + "
                 "spaCy NER if available, else regex-only with a clear TODO), dedupe by TF-IDF "
                 "cosine>0.95, and enforce quality asserts (null checks, type checks, positive "
                 "length). Include a tests/synthetic_requests.py generator of fake requests "
                 "(NO real data). Tests must run entirely on synthetic data."),
        "verify": {"type": "pytest", "target": "tests/test_data.py"},
        "depends_on": ["01-scaffold"],
        "retries": 2,
    },
    {
        "id": "04-features",
        "title": "Feature pipeline (MiniLM embeddings + domain features as one sklearn Pipeline)",
        "task": ("Implement src/pc_triage/features.py: a single scikit-learn Pipeline combining "
                 "(a) a custom transformer wrapping all-MiniLM-L6-v2 sentence embeddings (lazy "
                 "import; if model unavailable, fall back to a deterministic hashing embedding "
                 "so tests run offline) and (b) hand-crafted domain features: building-element "
                 "matcher, urgency keyword flags, cyclical seasonal features, message metadata. "
                 "The safeguarding flag must be computed but EXCLUDED from training features "
                 "(monitoring only). Tests check output shape and the safeguarding exclusion."),
        "verify": {"type": "pytest", "target": "tests/test_features.py"},
        "depends_on": ["03-data-pipeline"],
        "retries": 2,
    },
    {
        "id": "05-responsibility-matrix",
        "title": "Responsibility rules matrix (demise vs communal)",
        "task": ("Implement src/pc_triage/responsibility.py: a structured rules matrix keyed to "
                 "asset zone and demise-vs-communal indicators that constrains what the system "
                 "may recommend (RTM, leaseholder, or review-required). When evidence is "
                 "insufficient it MUST return review-required rather than guessing. Unit-test "
                 "the demise/communal boundary cases and the review-required fallback."),
        "verify": {"type": "pytest", "target": "tests/test_responsibility.py"},
        "depends_on": ["01-scaffold"],
        "retries": 2,
    },
    {
        "id": "06-model",
        "title": "LightGBM classifier training on synthetic data (MLflow logging)",
        "task": ("Implement src/pc_triage/model.py: train a LightGBM multi-class classifier on "
                 "the synthetic feature pipeline output, log to MLflow if available (else local "
                 "json metrics), and persist the model. Predicts issue category and urgency "
                 "band. Keep training fast on synthetic data. Test that training runs, a model "
                 "file is produced, and predict() returns valid labels. Real-data training is "
                 "OUT OF SCOPE and must be gated behind the DPIA."),
        "verify": {"type": "pytest", "target": "tests/test_model.py"},
        "depends_on": ["04-features"],
        "retries": 2,
    },
    {
        "id": "07-api",
        "title": "FastAPI /predict (Layer 1 then Layer 2, Pydantic schema, audit log)",
        "task": ("Implement src/pc_triage/api.py: a FastAPI app with POST /predict validated by "
                 "a Pydantic schema. Flow: run Layer 1 safety rules FIRST; if a safety rule "
                 "matches, return Critical with the cited rule and SKIP the ML model. Otherwise "
                 "run features -> LightGBM -> responsibility matrix, returning category, "
                 "urgency, responsibility, confidence, and a rationale trail. Every prediction "
                 "is written to an audit log with timestamp. Add bearer-token auth, input "
                 "sanitisation, and rate limiting (30/min). Test the safety short-circuit, the "
                 "ML path, schema rejection of bad input, and that auth is enforced."),
        "verify": {"type": "pytest", "target": "tests/test_api.py"},
        "depends_on": ["02-safety-rules", "06-model", "05-responsibility-matrix"],
        "retries": 2,
    },
    {
        "id": "08-monitoring",
        "title": "Monitoring + security scaffolding (Evidently config, bias hooks)",
        "task": ("Implement src/pc_triage/monitoring.py: an Evidently-based drift config (lazy "
                 "import) and a quarterly bias-evaluation hook over the safeguarding subgroup "
                 "(monitoring only, never a training feature). Add a SECURITY.md summarising "
                 "the assessment (auth, sanitisation, rate limit, HTTPS-only, no PII in the "
                 "pipeline). Tests check the config loads and the bias hook runs on synthetic "
                 "data."),
        "verify": {"type": "pytest", "target": "tests/test_monitoring.py"},
        "depends_on": ["07-api"],
        "retries": 1,
    },
    {
        "id": "09-integration",
        "title": "End-to-end integration test + full suite green",
        "task": ("Add tests/test_integration.py exercising the whole path on synthetic "
                 "requests: a safety request short-circuits to Critical; a routine request "
                 "gets a category/urgency/responsibility; an ambiguous request returns "
                 "review-required. Ensure the FULL test suite passes and ruff is clean. Write a "
                 "DEPLOYMENT.md noting that going live requires: DPIA sign-off, real-data "
                 "training under the gate, and director acceptance/override UI wiring."),
        "verify": {"type": "pytest"},
        "depends_on": ["08-monitoring"],
        "retries": 2,
    },
]


def build_example_court_plan():
    """Create (but do not run) the Example Court triage build plan."""
    from .builder import create_plan
    return create_plan(plan_name("triage"), _for_this_site(EXAMPLE_COURT_STEPS))


# Example Court Intake MVP , the deeper product (WordPress + embeddings/classifier). Ported from
# the v0.37 fork. This is the named plan the overnight builder runs to build the operational RTM
# product end to end, with live adapters disabled. Separate from example-court-triage (the
# classifier-only plan); this is the full intake-to-approval spine plus the WordPress/embeddings
# scaffold the audit pointed to.
EXAMPLE_COURT_INTAKE_MVP_STEPS: list[dict[str, Any]] = [
    {
        "id": "01-scaffold",
        "title": "RTM intake service scaffold and schema",
        "task": "Create a production-intent RTM intake project scaffold for {{rtm.name}}. Define data models for inbound email, voicemail transcript, case/thread, classification, draft response, receipt, and approval status. No live mail credentials, no real resident data in tests. Include fixtures only.",
        "verify": {"type": "file_exists", "path": "pyproject.toml"},
        "retries": 1,
    },
    {
        "id": "02-safety-rules",
        "title": "Deterministic urgency and responsibility rules",
        "task": "Implement deterministic rules for urgent safety issues, responsibility triage and review-required fallbacks. Safety rules must run before embeddings/ML. Include gas/fire/flood/electrical/lift/structural/security cases, negation tests, and evidence/rationale output.",
        "verify": {"type": "pytest", "target": "tests/test_safety_rules.py"},
        "depends_on": ["01-scaffold"],
        "retries": 2,
    },
    {
        "id": "03-importers",
        "title": "Email and voicemail importers",
        "task": "Implement importers for indexed {{site.rtm_email}} email and the HA voicemail add-on JSON/TXT outputs. They must be idempotent, never mark/move/delete upstream email, and preserve source handles for audit.",
        "verify": {"type": "pytest", "target": "tests/test_importers.py"},
        "depends_on": ["02-safety-rules"],
        "retries": 2,
    },
    {
        "id": "04-case-store",
        "title": "Case/thread store and search",
        "task": "Group related intakes into case/thread records, provide search over emails, voicemail transcripts and cases, and produce a timeline suitable for WordPress/admin display. Include idempotency and privacy tests.",
        "verify": {"type": "pytest", "target": "tests/test_cases.py"},
        "depends_on": ["03-importers"],
        "retries": 2,
    },
    {
        "id": "05-embeddings",
        "title": "Embeddings search/classifier scaffold",
        "task": "Add local embeddings for RTM corpus search with a deterministic hashing fallback so tests run offline. Keep classifier suggestions advisory; deterministic safety rules remain authoritative.",
        "verify": {"type": "pytest", "target": "tests/test_embeddings.py"},
        "depends_on": ["04-case-store"],
        "retries": 2,
    },
    {
        "id": "06-drafts",
        "title": "Draft-only response workflow",
        "task": "Generate draft email responses from classification and templates. Drafts must never send email. Add approve/cancel state, receipts, and tests proving cancelling does not send and approving only records approval.",
        "verify": {"type": "pytest", "target": "tests/test_drafts.py"},
        "depends_on": ["04-case-store"],
        "retries": 2,
    },
    {
        "id": "07-push-approvals",
        "title": "Phone/Home Assistant approval push",
        "task": "Push pending draft approval cards to ntfy/Home Assistant notification surfaces with safe links and receipts. No email send path. Tests must mock push transport and verify failures leave the draft pending.",
        "verify": {"type": "pytest", "target": "tests/test_approval_push.py"},
        "depends_on": ["06-drafts"],
        "retries": 2,
    },
    {
        "id": "08-wordpress-plugin",
        "title": "WordPress admin plugin scaffold",
        "task": "Create a minimal WordPress plugin scaffold that calls the FastAPI backend: dashboard list, case detail, draft detail, approve/cancel buttons, and status/error surfaces. No production secrets. Include PHP unit/static smoke tests or file checks.",
        "verify": {"type": "file_exists", "path": "wordpress-plugin/{{rtm.slug}}-intake.php"},
        "depends_on": ["07-push-approvals"],
        "retries": 2,
    },
    {
        "id": "09-live-adapters-disabled",
        "title": "Live adapters behind disabled flags",
        "task": "Add disabled-by-default live adapters for IMAP/Graph sync hooks, voicemail folder watch, and WordPress backend URL. Going live must require explicit config and a human acceptance step. Document the DPIA/data-protection gate.",
        "verify": {"type": "pytest"},
        "depends_on": ["08-wordpress-plugin", "05-embeddings"],
        "retries": 2,
    },
]


def build_example_court_intake_plan():
    """The named overnight plan for the deeper Example Court product (WordPress + embeddings/
    classifier), built with live adapters disabled."""
    from .builder import create_plan
    return create_plan(plan_name("intake-mvp"), _for_this_site(EXAMPLE_COURT_INTAKE_MVP_STEPS))


# Example Court Governance/Bookkeeping , the company-secretary + trust-accounting product. Ported
# from the v0.45 governance fork. Separate from the intake plan; this is the build spec for the
# WordPress + governance/accounting surface. Tracks/reconciles/exports only , no live filing or
# banking (those seams ship disabled).
EXAMPLE_COURT_GOVERNANCE_STEPS: list[dict[str, Any]] = [
    {
        "id": "01-governance-schema",
        "title": "Company secretary and bookkeeping schema",
        "task": "Create the RTM governance schema: company profile, compliance obligations, deadlines, guidance artefacts, service-charge trust ledger, reserve-fund trust ledger, weekly bank balances, transactions, transfers and reconciliation findings. Seed synthetic fixtures only; no real HMRC/Companies House credentials or bank feeds.",
        "verify": {"type": "pytest", "target": "tests/test_governance_schema.py"},
        "retries": 2,
    },
    {
        "id": "02-compliance-engine",
        "title": "Virtual company secretary deadline engine",
        "task": "Implement a deterministic compliance engine for Companies House confirmation statements, accounts, HMRC Corporation Tax payment/return reminders, optional VAT/PAYE watches, statutory registers and service-charge accounting pack reminders. Missing source facts must produce setup-needed items, not fake deadlines. Include guidance steps and completion receipts.",
        "verify": {"type": "pytest", "target": "tests/test_company_secretary.py"},
        "depends_on": ["01-governance-schema"],
        "retries": 2,
    },
    {
        "id": "03-bookkeeping-ledgers",
        "title": "Service charge and reserve fund trust ledgers",
        "task": "Implement a bookkeeper module that records opening balances, weekly bank balances, income, expenses and inter-fund transfers separately for service-charge trust and reserve-fund trust. Running balances must be fund-specific. Add reconciliation from previous/latest bank balance plus transactions. Export bookkeeper-ready CSV/JSON, explicitly not signed statutory accounts.",
        "verify": {"type": "pytest", "target": "tests/test_rtm_bookkeeper.py"},
        "depends_on": ["01-governance-schema"],
        "retries": 2,
    },
    {
        "id": "04-weekly-workflow",
        "title": "Weekly director prompts and receipts",
        "task": "Add a weekly workflow asking for current balances, expenses, transfers, unreconciled items and reimbursements. The workflow should create receipts for balance capture, reconciliation mismatch, and export. It must never connect to a bank or move money.",
        "verify": {"type": "pytest", "target": "tests/test_weekly_bookkeeping_flow.py"},
        "depends_on": ["02-compliance-engine", "03-bookkeeping-ledgers"],
        "retries": 2,
    },
    {
        "id": "05-cockpit-and-wordpress",
        "title": "Cockpit and WordPress admin surfaces",
        "task": "Expose the governance/bookkeeping status in Cockpit and scaffold a WordPress admin plugin view: compliance calendar, step-by-step filing artefacts, trust-fund balances, reconciliation warnings, ledger export buttons, and director approval receipts. No production secrets.",
        "verify": {"type": "file_exists", "path": "wordpress-plugin/{{rtm.slug}}-governance.php"},
        "depends_on": ["04-weekly-workflow"],
        "retries": 2,
    },
    {
        "id": "06-live-edges-disabled",
        "title": "Disabled live filing/banking adapters and validation",
        "task": "Add disabled-by-default seams for Companies House/HMRC lookup, bank-statement imports and accountant exports. Live edges must require explicit configuration and human acceptance. Add validate-governance to verify reminders, ledgers, exports and no-live-edge defaults.",
        "verify": {"type": "pytest"},
        "depends_on": ["05-cockpit-and-wordpress"],
        "retries": 2,
    },
]


def build_example_court_governance_plan():
    """The named overnight plan for the Example Court governance/bookkeeping product (company
    secretary + trust ledgers + WordPress admin), live filing/banking seams disabled."""
    from .builder import create_plan
    return create_plan(plan_name("governance"), _for_this_site(EXAMPLE_COURT_GOVERNANCE_STEPS))


EXAMPLE_COURT_MANAGEMENT_STEPS: list[dict[str, Any]] = [
    {
        "id": "01-demands-ledger-schema",
        "title": "Demand register and flat ledger schema",
        "task": "Implement flat reference rows for 18 flats, immutable demand register rows, corrected/reissued demand links, and per-flat running balances. Amounts in pence, ISO dates, corrections as reversing entries. Seed synthetic fixtures only.",
        "verify": {"type": "pytest", "target": "tests/test_rtm_management.py"},
        "retries": 2,
    },
    {
        "id": "02-metro-reconciliation",
        "title": "Metro Bank CSV reconciliation and review queue",
        "task": "Import Metro Bank CSV credits, exact-match payment references + amounts to open demands, auto-allocate exact matches only, and put partial/unmatched/duplicate credits into a review queue. Support split allocation and multiple payments per demand.",
        "verify": {"type": "pytest", "target": "tests/test_rtm_management_reconcile.py"},
        "depends_on": ["01-demands-ledger-schema"],
        "retries": 2,
    },
    {
        "id": "03-arrears-and-recovery-pack",
        "title": "Arrears schedule, reminder drafts and debt pack",
        "task": "Implement overdue status refresh, configurable day+1/+14/+28/+56/+90 escalation, review-only reminder document generation, release logging, and a debt-referral evidence pack. No automatic sending and no forfeiture/court wording beyond safe placeholders.",
        "verify": {"type": "pytest", "target": "tests/test_rtm_arrears.py"},
        "depends_on": ["02-metro-reconciliation"],
        "retries": 2,
    },
    {
        "id": "04-payables-budget-cashflow",
        "title": "Supplier payables, commitments, budget-vs-actuals and cashflow",
        "task": "Add outgoing payables, recurring commitments, approval/paid states, budget-line mapping, spent/committed/variance views and 1/3/6 month cashflow. Payment posting must be reviewable and reconcile to bank statements.",
        "verify": {"type": "pytest", "target": "tests/test_rtm_budget_payables.py"},
        "depends_on": ["03-arrears-and-recovery-pack"],
        "retries": 2,
    },
    {
        "id": "05-evidence-and-wordpress-admin",
        "title": "Evidence store and WordPress admin scaffold",
        "task": "Layer evidence attachments over demands, reminders, budget lines, suppliers and approvals, then scaffold a WordPress admin interface for demand register, payment review, arrears, payables, budget actuals and evidence retrieval. Live sending/payment remains disabled.",
        "verify": {"type": "file_exists", "path": "wordpress-plugin/{{rtm.slug}}-rtm-admin.php"},
        "depends_on": ["04-payables-budget-cashflow"],
        "retries": 2,
    },
]


def build_example_court_management_plan():
    """Named overnight build plan for the full RTM management-system surface."""
    from .builder import create_plan
    return create_plan(plan_name("management"), _for_this_site(EXAMPLE_COURT_MANAGEMENT_STEPS))



# --- Example Court RTM. Receipts and trust reconciliation (turnstile model) ---
# Records and checks only; never moves money. Extends rtm_management + rtm_bookkeeper; adds the
# turnstile account model, reserve maths, receipts, weekly reconciliation, and transfer monitoring.
EXAMPLE_COURT_RTM_TRUST_STEPS = [
    {
        "id": "01-account-model",
        "title": "Turnstile account model",
        "task": ("Three ledger accounts (running, service_charge_trust, reserve_fund_trust) with trust "
                 "tagging and per-account balance. running is the only world-facing account and is "
                 "target-zero. Trust accounts never face the outside and never exchange directly with "
                 "each other. The reserve account reuses the existing reserve_fund_trust; 'reserve_trust' "
                 "is an input alias. running is kept out of the trust-only bookkeeper so trust money "
                 "cannot commingle at the data layer."),
        "verify": {"type": "pytest", "target": "tests/test_rtm_trust_accounts.py"},
    },
    {
        "id": "02-reserve-maths",
        "title": "Reserve fraction and reserve liability",
        "task": ("reserve_fraction = reserve_annual / sch1_total from the live budget ("
                 "{{RESERVE_FRACTION_EXAMPLE}}; sch1 total includes the reserve line). Per-receipt reserve portion on the "
                 "Schedule 1 part only; Schedule 2 contributes zero. Track reserve collected, transferred "
                 "and in_transit cumulatively."),
        "verify": {"type": "pytest", "target": "tests/test_rtm_trust_reserve.py"},
    },
    {
        "id": "03-receipts",
        "title": "Receipts: build, render, store",
        "task": ("Generate a receipt only when a demand reaches zero outstanding (partials update the "
                 "statement only). Receipt number RCPT-26-{flat}, demand branding (olive headings, ochre "
                 "motto footer, Tel/email header, boxed outstanding). Combined PDF is receipt first then "
                 "the original demand. Store via the evidence system, kind service_charge_receipt, linked "
                 "to flat and demand."),
        "verify": {"type": "pytest", "target": "tests/test_rtm_trust_receipts.py"},
    },
    {
        "id": "04-weekly-reconcile",
        "title": "Weekly reconciliation across three feeds",
        "task": ("Per-account CSV ingest with SHA-256 de-duplication, drift, an unmatched-line review "
                 "queue, and a report with status ok/attention/overdue. Trust accounts: any drift is "
                 "attention. running: a transient non-zero balance is not an error; the signal is trust "
                 "money resting here. Reserve balance is never shown as operating cash."),
        "verify": {"type": "pytest", "target": "tests/test_rtm_trust_reconcile.py"},
    },
    {
        "id": "05-transfer-monitor",
        "title": "Transfer monitoring of the four sweeps",
        "task": ("Recognise a sweep when a debit and a matching same-amount credit appear on two "
                 "different accounts within a day or two; tag the pair. Monitor protect/reserve/fund-"
                 "operating/fund-works sweeps. Flags: trust exposure (7-day grace then overdue), reserve "
                 "raid (reserve->running followed by an operating-cost payment), unfunded supplier "
                 "payment, half-open transfer (transfer_incomplete, never auto-resolved). Plus an "
                 "exposure window report."),
        "verify": {"type": "pytest", "target": "tests/test_rtm_trust_transfers.py"},
    },
    {
        "id": "06-cli-endpoints",
        "title": "CLI, endpoints and the moves-no-money guarantee",
        "task": ("Wire the rtm receipt/reserve/reconcile/transfers CLI subcommands and the cockpit-"
                 "nonce/token endpoints (none public). Every surface records and checks only. Assert the "
                 "guarantee: no function, CLI, or endpoint initiates, confirms, or simulates a bank "
                 "instruction."),
        "verify": {"type": "pytest", "target": "tests/test_rtm_trust_transfers.py"},
    },
]


def build_example_court_rtm_trust_plan():
    """Named build plan for Example Court RTM receipts and trust reconciliation. Records and checks
    only; never moves money."""
    from .builder import create_plan
    return create_plan(plan_name("rtm-trust-reconciliation"), _for_this_site(EXAMPLE_COURT_RTM_TRUST_STEPS))
