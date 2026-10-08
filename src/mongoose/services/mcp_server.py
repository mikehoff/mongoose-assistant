"""Mongoose MCP server: exposes a set of Mongoose tools to an external MCP client
(Claude.ai custom connector) over JSON-RPC 2.0. Reachable at POST /mcp via the Cloudflare tunnel,
bearer-token gated in app.py. This is now a FULL read AND write surface: alongside the read-only
RTM/reminder reporting it can set reminders, run a Mongoose chat turn, file a bug, ingest a file, and
search across everything. Every write goes through the same real Mongoose functions the Cockpit and
chat loop use, so it carries the same effects, not a parallel path.

The route in app.py runs handle() inside a worker thread (asyncio.to_thread), so _call_tool is SYNC.
Any async Mongoose function is wrapped with asyncio.run(...) here. Every tool is best-effort: on
error it returns an isError text result and never raises.

Protocol: MCP 2025-11-25. Implements initialize, tools/list, tools/call, ping; ignores notifications.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any

PROTOCOL_VERSION = "2025-11-25"


def _server_info() -> dict[str, Any]:
    try:
        from .. import __version__ as ver
    except Exception:
        ver = "0"
    return {"name": "mongoose", "version": str(ver)}


# ── Tool catalogue (READ-ONLY) ───────────────────────────────────────────────
def _render_specs(obj: Any) -> Any:
    from ..settings import render_profile_copy
    if isinstance(obj, str):
        return render_profile_copy(obj)
    if isinstance(obj, list):
        return [_render_specs(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _render_specs(v) for k, v in obj.items()}
    return obj


_RENDERED_SPECS: dict[Any, list[dict[str, Any]]] = {}


def tool_specs() -> list[dict[str, Any]]:
    """The connector's tool list with the site-profile placeholders in its copy filled in (the RTM's
    name, example flat ids), cached per profile."""
    from ..settings import profile_copy_key
    key = profile_copy_key()
    hit = _RENDERED_SPECS.get(key)
    if hit is None:
        hit = _render_specs(_raw_tool_specs())
        _RENDERED_SPECS.clear()
        _RENDERED_SPECS[key] = hit
    import copy
    return copy.deepcopy(hit)  # callers may annotate their copy; the cache stays clean


def _raw_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "describe_system",
            "title": "Describe Mongoose and the plugin (capabilities + data schema)",
            "description": ("A LIVE self-description so you can understand what Mongoose and the "
                            "{{rtm.name}} WordPress plugin can do and hold. Returns: every Mongoose capability "
                            "(tool name, kind, purpose), this connector's own tools, the full SQLite data "
                            "schema (every table and its columns, read live so it always reflects the "
                            "current shape, including the flats' primary + alternate emails and addresses), "
                            "a short RTM data-model note, and the plugin's own live capability map (routes, "
                            "tables, admin pages). Read-only. Call this first to learn the system before "
                            "reasoning about its data."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_files",
            "title": "List ingested RTM documents",
            "description": ("List the files ingested into Mongoose's RTM document store: id, filename, "
                            "type, flat, a short summary, and key facts (total owed, sort code, account) "
                            "where they could be extracted. Use it to find a document's id before "
                            "retrieving it with retrieve_file."),
            "inputSchema": {"type": "object", "properties": {
                "doc_type": {"type": "string", "description": "optional filter, e.g. invoice, receipt, contract"},
                "flat_id": {"type": "string", "description": "optional flat id, e.g. {{RTM_BLOCK_A}}F3"},
                "limit": {"type": "integer", "description": "max documents (default 50)"}}},
        },
        {
            "name": "retrieve_file",
            "title": "Retrieve a file's content",
            "description": ("Retrieve one ingested document by id: its metadata, extracted text, and (for "
                            "files under ~10MB) the raw bytes base64-encoded so you can read or reuse the "
                            "actual file. Get the id from list_files or unified_search."),
            "inputSchema": {"type": "object", "properties": {
                "id": {"type": "integer", "description": "the document id"},
                "include_bytes": {"type": "boolean", "description": "include base64 file content (default true)"}},
                "required": ["id"]},
        },
        {
            "name": "retrieve_attachment",
            "title": "Retrieve an email attachment's content",
            "description": ("Retrieve one EMAIL ATTACHMENT by id: metadata, the REAL extracted text, and "
                            "(under ~10MB) the raw bytes base64-encoded, so you read the ACTUAL file (e.g. "
                            "an invoice PDF and its true amount, sort code and account number) rather than a "
                            "summary that could be wrong. Get the id from unified_search: attachment hits "
                            "show 'attachment_id=<n>'. Use this for invoices/documents that arrived as email "
                            "attachments and were never uploaded to the RTM document store (retrieve_file "
                            "only reaches uploaded documents, not attachments)."),
            "inputSchema": {"type": "object", "properties": {
                "id": {"type": "integer", "description": "the attachment id (from unified_search)"},
                "include_bytes": {"type": "boolean", "description": "include base64 file content (default true)"}},
                "required": ["id"]},
        },
        {
            "name": "confirm_payee_bank",
            "title": "Confirm a payee's bank details",
            "description": ("Record that the owner has VERIFIED a payee's bank details (e.g. confirmed by "
                            "phone). Once confirmed, any future invoice from that payee whose details DIFFER "
                            "is flagged as a possible redirection-fraud attempt. Give the payee (supplier "
                            "name or email/domain), the 6-digit sort code and the 8-digit account number. "
                            "Does not move money."),
            "inputSchema": {"type": "object", "properties": {
                "payee": {"type": "string", "description": "supplier name or email/domain, e.g. 'ISE' or accounts@liftinspect.example"},
                "sort_code": {"type": "string", "description": "6-digit sort code, e.g. 01-02-03"},
                "account_number": {"type": "string", "description": "8-digit account number"},
                "account_name": {"type": "string", "description": "optional account name"}},
                "required": ["payee", "sort_code", "account_number"]},
        },
        {
            "name": "rtm_status",
            "title": "RTM service-charge status",
            "description": ("{{rtm.name}} RTM service-charge summary: total demanded vs paid, how many "
                            "flats are outstanding, and the arrears list. Read-only."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rtm_flat_statement",
            "title": "RTM flat statement",
            "description": ("One flat's service-charge statement (balance, position, demands, receipts). "
                            "Resolve by flat id (e.g. {{RTM_BLOCK_A}}F3) or by leaseholder name (e.g. 'Petrov'). Read-only."),
            "inputSchema": {
                "type": "object",
                "properties": {"flat": {"type": "string", "description": "flat id or leaseholder name"}},
                "required": ["flat"],
            },
        },
        {
            "name": "rtm_unpaid_demands",
            "title": "RTM unpaid demands",
            "description": "List the flats with an outstanding (unpaid) service-charge demand. Read-only.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rtm_knowledge",
            "title": "RTM knowledge base",
            "description": ("Authoritative read-only {{rtm.name}} RTM facts, the same knowledge base "
                            "Mongoose's own assistant uses: the receiving BANK ACCOUNT printed on every "
                            "demand, the budget, the flats/leaseholders and external parties (solicitors, "
                            "managing agent, insurer, tribunal), live legal matters, suppliers and "
                            "contracts, the calendar of deadlines, and history. Use for RTM FACTUAL "
                            "questions such as 'what bank account is on the demands', 'who are the "
                            "solicitors', 'what is the reserve line'. Grounded in canonical records, never "
                            "invented. action: index (default, lists sections) | search (+query) | "
                            "section (+section name) | semantics | budget."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "index | search | section | semantics | budget (default index)"},
                    "query": {"type": "string", "description": "search terms, for action=search"},
                    "section": {"type": "string", "description": "section name, for action=section"},
                },
            },
        },
        {
            "name": "rtm_dashboard",
            "title": "RTM management dashboard",
            "description": ("The one-call state of the RTM: fund balances, arrears, allocation-review queue, "
                            "payables due soon, budget-vs-actual with over-budget lines, evidence gaps, and a "
                            "prioritised next-actions list. Read-only. Ask this first for 'how is the RTM "
                            "doing / what needs doing'."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rtm_reconciliation",
            "title": "RTM bank reconciliation",
            "description": ("How the bank reconciles: recorded credits with match status, expenditure lines "
                            "with mapped category or a suggestion, per-category spend-vs-budget, and book-vs-bank "
                            "balances. Read-only (no raw statement data). For 'where is the money / does the "
                            "bank reconcile'. Optional year (e.g. 2027)."),
            "inputSchema": {"type": "object", "properties": {"year": {"type": "string"}}},
        },
        {
            "name": "rtm_arrears",
            "title": "RTM arrears + recovery",
            "description": ("Overdue and part-paid demands with days-overdue and a per-flat recovery next-action "
                            "(the actionable version of rtm_unpaid_demands). Read-only. For 'who is behind and "
                            "what is the next step'."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rtm_finances",
            "title": "RTM finances (parametrized)",
            "description": ("Financial detail reads, choose a view: funds (per-fund balances) | budget "
                            "(budget-vs-actual per line) | cashflow (projection) | payables (supplier bills) | "
                            "fund_reconciliation (fund movements vs bank) | subaccount_audit (reserve/service "
                            "split reconciles to budget) | collection (demanded vs collected). Read-only. For "
                            "'are we on budget', 'how much is in reserve', 'what do we owe suppliers'."),
            "inputSchema": {"type": "object", "properties": {
                "view": {"type": "string", "description": "funds | budget | cashflow | payables | fund_reconciliation | subaccount_audit | collection"}}, "required": ["view"]},
        },
        {
            "name": "rtm_trust",
            "title": "RTM trust-money safety",
            "description": ("Leaseholder trust-money safety, choose a view: transfer_status (per obligation "
                            "in_sync|due|overdue plus section-5 flags) | exposure (trust money sitting in the "
                            "operating account and oldest unswept age) | reserve_position (reserve collected vs "
                            "transferred vs held). Read-only. For 'are the trust transfers current, is any "
                            "leaseholder money exposed'."),
            "inputSchema": {"type": "object", "properties": {
                "view": {"type": "string", "description": "transfer_status | exposure | reserve_position"}}, "required": ["view"]},
        },
        {
            "name": "rtm_bookkeeper",
            "title": "RTM trust cash book",
            "description": ("The trust cash book, choose a view: summary | reconcile (book-vs-bank per fund) | "
                            "ledger (running balances; optional fund). Read-only. For 'show me the cash book / "
                            "does it reconcile'."),
            "inputSchema": {"type": "object", "properties": {
                "view": {"type": "string", "description": "summary | reconcile | ledger"},
                "fund": {"type": "string", "description": "optional fund filter for ledger"}}, "required": ["view"]},
        },
        {
            "name": "rtm_evidence",
            "title": "Evidence behind a bank line",
            "description": ("Given a reconciled bank line (a demand document number, or a flat, or a payee "
                            "description) return the best-matching invoices/receipts/contracts on file, "
                            "strong-link-first. Read-only. For 'show me the invoice behind this payment'."),
            "inputSchema": {"type": "object", "properties": {
                "document_number": {"type": "string"}, "flat": {"type": "string"}, "description": {"type": "string"}}},
        },
        {
            "name": "rtm_ontology",
            "title": "RTM meaning layer",
            "description": ("Canonical meaning of RTM terms so answers do not guess: describe | explain (a term) "
                            "| audit | value. Read-only. For 'what exactly is a sub-account vs a schedule', 'the "
                            "reserve split rule'. Pass the term."),
            "inputSchema": {"type": "object", "properties": {
                "action": {"type": "string", "description": "describe | explain | audit | value"},
                "term": {"type": "string"}}},
        },
        {
            "name": "rtm_recent_activity",
            "title": "What Mongoose did lately",
            "description": ("What Mongoose has actually done for the RTM recently, from receipts/audit, including "
                            "failures and skips. Read-only. For 'what have you done recently'. Optional limit."),
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
        {
            "name": "rtm_portal_status",
            "title": "Leaseholder portal status",
            "description": ("Whether the WordPress leaseholder portal is published/ready and what is missing "
                            "(demand PDFs, receipts). Read-only. For 'is the portal live and correct'."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rtm_roster",
            "title": "Flats + leaseholders registry",
            "description": ("The flats/leaseholders registry: flat id, leaseholder name, correspondence address, "
                            "apportionment band, payment reference. Read-only. For 'who is the leaseholder of "
                            "flat 3', 'list the flats'."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rtm_referral_pack",
            "title": "RTM referral / FTT bundle",
            "description": ("A solicitor/tribunal evidence bundle scaffold for a flat (or a specific demand): "
                            "demands, recovery actions, the per-flat ledger, and evidence. Read-only. For "
                            "'assemble the arrears evidence for flat 3'."),
            "inputSchema": {"type": "object", "properties": {
                "flat": {"type": "string"}, "document_number": {"type": "string"}}},
        },
        {
            "name": "rtm_seller_pack",
            "title": "LPE1 seller-pack draft",
            "description": ("Draft the LPE1 management-information pack for a flat being sold: the fields "
                            "answered live from Mongoose (each with provenance: read/derived/assumed) and a "
                            "DIRECTOR REVIEW SHEET of what still needs deciding (freeholder, buildings "
                            "insurance, lease terms, the sealed-track disputes disclosure). Includes the "
                            "insurance-vs-budget and seller-identity reconciliation checks. Read-only DRAFT: "
                            "produces no filled PDF and sends nothing. For 'draft the seller pack for flat 7'. "
                            "Give the flat (id or leaseholder)."),
            "inputSchema": {"type": "object", "properties": {"flat": {"type": "string"}}, "required": ["flat"]},
        },
        {
            "name": "rtm_registers_status",
            "title": "Seller-pack registers status",
            "description": ("What the seller-pack data registers hold versus still need populating: building "
                            "profiles, insurance register, FRA/compliance, lease terms, company register. "
                            "Read-only. For 'what does the seller pack still need to be complete'."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rtm_first_stage_response",
            "title": "LPE1 first-stage response",
            "description": ("Produce the STAGE-1 reply to an LPE1 request: the branded cover letter (fee, "
                            "bank details, what is released on payment, notice-of-assignment/deed fees, "
                            "membership) plus the statutory Schedule 11 administration-charge summary that "
                            "must accompany it. Returns download links + the text + a director checklist. "
                            "ARTIFACTS ONLY: it produces documents, SENDS nothing, and never releases the "
                            "completed form or enclosures (that is Stage 2, only after the fee is paid). "
                            "Human sign-off before issue is mandatory. Pass the flat (id or leaseholder); "
                            "optionally the conveyancer/seller details from the request (set save=true to "
                            "remember them on the job). For 'draft the first-stage LPE1 reply for flat 7'."),
            "inputSchema": {"type": "object", "properties": {
                "flat": {"type": "string"},
                "seller_name": {"type": "string"}, "conveyancer_firm": {"type": "string"},
                "conveyancer_contact": {"type": "string"}, "conveyancer_email": {"type": "string"},
                "their_ref": {"type": "string"}, "request_date": {"type": "string",
                    "description": "date of the LPE1 request, e.g. 2026-09-09"},
                "save": {"type": "boolean", "description": "persist the supplied details on the job (default false)"}},
                "required": ["flat"]},
        },
        {
            "name": "rtm_export",
            "title": "Export accountant pack",
            "description": ("Build the multiformat accountant export (Xero/QuickBooks/GnuCash CSVs, QIF/OFX, an "
                            "xlsx workbook, a branded PDF report, and an evidence folder of the source "
                            "invoices/receipts) and return a download link. Produces a file; sends nothing. For "
                            "'give my accountant everything'. Optional formats, include_evidence (default true), year."),
            "inputSchema": {"type": "object", "properties": {
                "formats": {"type": "string", "description": "space/comma list, e.g. 'xero pdf xlsx'; omit for all"},
                "include_evidence": {"type": "boolean"}, "year": {"type": "string"}}},
        },
        {
            "name": "get_demand_pdf",
            "title": "Fetch a flat's demand PDF",
            "description": ("Return the service-charge DEMAND PDF for a flat (the one printed with the bank "
                            "account and reference), rebuilding it if not already stored. Returns a download "
                            "link (and optionally the bytes). Read-only artifact. For 'get flat 7's demand'. "
                            "Give the flat (id or leaseholder), optional period (e.g. 2026-H2)."),
            "inputSchema": {"type": "object", "properties": {
                "flat": {"type": "string"}, "period": {"type": "string"},
                "include_bytes": {"type": "boolean", "description": "also return base64 bytes (default false)"}}, "required": ["flat"]},
        },
        {
            "name": "rtm_generate_document",
            "title": "Generate a branded RTM document",
            "description": ("Produce a branded document from real data and return a download link (or, for a "
                            "cover email, the DRAFT text). Templates: payment_receipt, welcome, "
                            "cover_email_receipt_with_demand. Never sends anything. For 'make the receipt for "
                            "{{RTM_BLOCK_B}}F9 as a document'. Give template and flat."),
            "inputSchema": {"type": "object", "properties": {
                "template": {"type": "string", "description": "payment_receipt | welcome | cover_email_receipt_with_demand"},
                "flat": {"type": "string"}, "recipient_email": {"type": "string"}}, "required": ["template"]},
        },
        {
            "name": "reminders_list",
            "title": "Reminders and alarms",
            "description": "The upcoming reminders and alarms Mongoose is holding. Read-only.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "set_reminder",
            "title": "Set a reminder",
            "description": ("Create a reminder Mongoose owns and will fire (phone push). Give the thing to be "
                            "reminded and a natural-language time. Writes real state."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "what to be reminded of, e.g. 'call the surveyor'"},
                    "when": {"type": "string", "description": "when, natural language ('tomorrow 9am', 'in 2 hours') or an ISO datetime"},
                },
                "required": ["text", "when"],
            },
        },
        {
            "name": "unified_search",
            "title": "Search everything",
            "description": ("Cross-source search over Mongoose's indexed files, attachments, emails, voicemails, "
                            "notifications and imported knowledge. Read-only."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "what to search for"},
                    "limit": {"type": "integer", "description": "max hits to return (default 10)"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "mongoose_chat",
            "title": "Ask Mongoose",
            "description": ("Ask Mongoose's own assistant a question and get its answer. Runs a full local chat "
                            "turn with household context, memory and tools, so it can act as well as answer."),
            "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "the message to send to Mongoose"}},
                "required": ["message"],
            },
        },
        {
            "name": "report_bug",
            "title": "Report a bug",
            "description": ("File a bug or piece of feedback for Mongoose. Returns a tracking id and surfaces it to "
                            "the owner. Writes real state."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "one-line summary of the bug or feedback"},
                    "detail": {"type": "string", "description": "optional longer detail / repro steps"},
                },
                "required": ["summary"],
            },
        },
        {
            "name": "upload_file",
            "title": "Upload a file to Mongoose",
            "description": ("Ingest a file into Mongoose's RTM document store: it is stored, text-extracted, "
                            "indexed into unified search and correlated to flats/demands/emails/voicemails. "
                            "Send the bytes base64-encoded. Writes real state."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "the file name, e.g. 'receipt.pdf'"},
                    "content_base64": {"type": "string", "description": "the file bytes, base64-encoded"},
                    "doc_type": {"type": "string", "description": "one of: receipt, invoice, contract, ticket, "
                                 "feedback, complaint, photo_evidence, other_evidence (default other_evidence)"},
                    "flat_id": {"type": "string", "description": "optional flat id, e.g. {{RTM_BLOCK_A}}F3"},
                    "note": {"type": "string", "description": "optional note stored with the document"},
                },
                "required": ["filename", "content_base64"],
            },
        },
        {
            "name": "rtm_mark_paid",
            "title": "Mark a flat's demand paid",
            "description": ("Record a service-charge payment RECEIVED for a flat (a bank credit + allocation), "
                            "clearing the balance, flipping the demand to Paid and auto-generating the branded "
                            "receipt. Never moves money. Idempotent. Resolve by flat id (e.g. {{RTM_BLOCK_A}}F3) or "
                            "leaseholder name (e.g. 'Barnes'). Defaults: full outstanding amount, bank "
                            "transfer, received today unless a date is given. Writes real state."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "flat": {"type": "string", "description": "flat id or leaseholder name"},
                    "amount": {"type": "string", "description": "optional amount, e.g. '1075.16' (default: full outstanding)"},
                    "received_date": {"type": "string", "description": "optional ISO date the money arrived (default: today)"},
                },
                "required": ["flat"],
            },
        },
        {
            "name": "rtm_send_receipt",
            "title": "Email a leaseholder their receipt",
            "description": ("Email a leaseholder their payment receipt (already on file) with a branded cover "
                            "note, sent from the RTM address via WordPress. Resolve by flat id or leaseholder "
                            "name. The receipt must already exist (mark the demand paid first). Sends real email."),
            "inputSchema": {
                "type": "object",
                "properties": {"flat": {"type": "string", "description": "flat id or leaseholder name"}},
                "required": ["flat"],
            },
        },
        {
            "name": "rtm_subaccount_split",
            "title": "Reserve/service sub-account split",
            "description": ("The canonical reserve-fund vs service-charge trust split for a flat's half-year "
                            "demand (the two sub bank accounts): how much of a payment belongs in the reserve "
                            "trust vs the service-charge trust. Read-only, penny-exact from the deed."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "flat": {"type": "string", "description": "flat id, e.g. {{RTM_BLOCK_A}}F3"},
                    "year": {"type": "string", "description": "optional budget year (default: latest)"},
                },
                "required": ["flat"],
            },
        },
        {
            "name": "task_add",
            "title": "Keep a task / to-do",
            "description": ("Capture an outstanding task or to-do Mongoose owns (e.g. an RTM action). Give the "
                            "task text; tag it RTM by passing list='RTM'. Writes real state."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "the task, e.g. 'respond to window cleaning quote people'"},
                    "list": {"type": "string", "description": "optional list name, e.g. 'RTM' (default: Tasks)"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "task_list",
            "title": "Outstanding tasks",
            "description": "List outstanding tasks / to-dos. Pass rtm=true to show only RTM-tagged tasks. Read-only.",
            "inputSchema": {
                "type": "object",
                "properties": {"rtm": {"type": "boolean", "description": "only RTM-tagged tasks"}},
            },
        },
        {
            "name": "v3_bus_read",
            "title": "Read the v3 peer bus",
            "description": ("Read messages on the shared v3 agent bus addressed to a NAMED PEER. Pass "
                            "peer='chat' to read chat's inbox, 'code' for code's. Without this, a peer "
                            "asking Mongoose about the bus received MONGOOSE's inbox and was told "
                            "'no unread' while a message addressed to it sat there unread. Read-only."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "peer": {"type": "string",
                             "description": "whose inbox to read: chat, code, mongoose or all"},
                    "limit": {"type": "integer", "description": "max messages, newest last (default 20)"},
                },
                "required": ["peer"],
            },
        },
        {
            "name": "v3_bus_post",
            "title": "Post to the v3 peer bus",
            "description": ("Append a message to the shared v3 agent bus. `sender` is recorded as the "
                            "author, so a peer posting through Mongoose is attributed to ITSELF, not to "
                            "Mongoose. Use it to reply to something read with v3_bus_read."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sender": {"type": "string", "description": "who is writing: chat, code or mongoose"},
                    "to": {"type": "string", "description": "recipient: chat, code, mongoose or all"},
                    "topic": {"type": "string", "description": "short topic slug"},
                    "body": {"type": "string", "description": "the message"},
                    "ack_of": {"type": "string", "description": "msg_id this replies to, if any"},
                },
                "required": ["sender", "to", "body"],
            },
        },
    ]


# ── Tool implementations ─────────────────────────────────────────────────────
def _describe_system() -> str:
    """Live self-description of Mongoose + the WordPress plugin: capabilities, the full data schema, and
    the plugin surface. Everything is read from the live registry/DB, so it reflects the current schema
    (including newly added columns) with no hardcoded copy to drift out of date. Read-only."""
    import json
    out: dict[str, Any] = {"mongoose_version": _server_info().get("version")}

    # Mongoose capabilities: the local chat/act tool loop (reachable through mongoose_chat).
    try:
        from ..tools import READ_TOOLS, SENSITIVE_TOOLS, rendered_tools
        caps = []
        for name, spec in sorted(rendered_tools().items()):
            kind = "read" if name in READ_TOOLS else ("sensitive" if name in SENSITIVE_TOOLS else "act")
            caps.append({"name": name, "kind": kind, "description": (spec.get("description") or "")[:300]})
        out["mongoose_capabilities"] = caps
    except Exception as e:
        out["mongoose_capabilities_error"] = str(e)

    # This connector's own tools.
    out["mcp_tools"] = [{"name": t["name"], "description": (t.get("description") or "")[:200]}
                        for t in tool_specs()]

    # Live data schema: every table and its columns, so new columns appear automatically.
    try:
        from ..db import connect
        schema: dict[str, list[str]] = {}
        with connect() as con:
            tbls = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for t in tbls:
                schema[t] = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        out["data_schema"] = schema
        out["data_schema_note"] = (
            "Live SQLite schema of the Mongoose brain. rtm_mgmt_flats now holds up to three emails "
            "(leaseholder_email is the primary that is on file, leaseholder_email_alt1/alt2 are "
            "alternatives such as a second address or a managing agent) and two postal addresses "
            "(correspondence_address primary, correspondence_address_alt1). rtm_mgmt_demands is the "
            "demand register; rtm_demand_sends is the batch demand-mailing audit log; "
            "rtm_mgmt_ledger_entries and rtm_mgmt_allocations hold payments; rtm_trust_* is the trust "
            "accounting and receipts; portal_demand_pdfs/portal_receipt_pdfs are the generated documents.")
    except Exception as e:
        out["data_schema_error"] = str(e)

    # Plugin self-description, fetched live over the signed sync channel, with a graceful fallback.
    try:
        from . import rtm_portal
        from .. import profile as _profile
        r = rtm_portal._wp_call("GET", _profile.wp_route("capabilities"))
        if r.get("ok") and isinstance(r.get("data"), dict):
            out["plugin"] = r["data"]
        else:
            h = rtm_portal._wp_call("GET", _profile.wp_route("health"))
            out["plugin"] = {
                "version": (h.get("data") or {}).get("version"),
                "note": "live capabilities endpoint not available (plugin below 1.0.40); install it for the live plugin map",
                "known_routes": ["portal/send-demand", "portal/send-receipt", "sync/doc-requests",
                                 "sync/doc-requests-ack", "sync/director-summary", "sync/flat-accounts",
                                 "health", "capabilities"],
                "known_tables": ["pc_rtm_snapshot", "pc_rtm_pdfs", "pc_rtm_doc_requests",
                                 "pc_rtm_payment_marks", "pc_rtm_member_changes", "pc_rtm_send_log"],
                "admin_pages": ["Service Charges", "Flats and Members", "Access requests", "Sent mail log"]}
    except Exception as e:
        out["plugin_error"] = str(e)

    return json.dumps(out, ensure_ascii=False, indent=1)


def _list_files(doc_type: str, flat_id: str, limit: Any) -> str:
    """List ingested RTM documents (id + metadata + summary + key facts), for the connector to discover
    files before retrieving them."""
    import json
    from . import rtm_docs
    try:
        n = int(limit) if limit else 50
    except Exception:
        n = 50
    docs = rtm_docs.list_documents(doc_type=(doc_type or None), flat_id=(flat_id or None),
                                   limit=max(1, min(n, 200)))
    out = [{"id": d["id"], "filename": d["filename"], "doc_type": d["doc_type"], "flat_id": d.get("flat_id"),
            "description": d.get("description", ""), "created_at": d.get("created_at"), "summary": d.get("summary", ""),
            "key_facts": d.get("key_facts", {})} for d in docs]
    return json.dumps({"count": len(out), "documents": out}, ensure_ascii=False, indent=1)


def _retrieve_file(doc_id: Any, include_bytes: bool = True) -> str:
    """Retrieve one ingested document by id: metadata + extracted text + (for files under ~10MB) the raw
    bytes base64-encoded, so the connector can read or reuse the actual file."""
    import base64
    import json
    import os
    from . import rtm_docs
    try:
        did = int(doc_id)
    except Exception:
        return "retrieve_file needs a numeric document id (see list_files)."
    d = rtm_docs.get_document(did)
    if not d:
        return f"No document with id {did}."
    path = str(d.get("stored_path") or "")
    res: dict[str, Any] = {
        "id": d["id"], "filename": d.get("filename"), "doc_type": d.get("doc_type"),
        "mime": d.get("mime"), "flat_id": d.get("flat_id"), "created_at": d.get("created_at"),
        "extracted_text": (d.get("extracted_text") or "")[:20000]}
    if include_bytes and path and os.path.exists(path):
        sz = os.path.getsize(path)
        if sz <= 10 * 1024 * 1024:
            with open(path, "rb") as f:
                res["content_base64"] = base64.b64encode(f.read()).decode()
            res["size_bytes"] = sz
        else:
            res["note"] = f"file is {sz // (1024 * 1024)}MB, too large to inline; extracted_text is provided"
    return json.dumps(res, ensure_ascii=False)


def _retrieve_attachment(att_id: Any, include_bytes: bool = True) -> str:
    """Retrieve one EMAIL ATTACHMENT by id: metadata + the REAL extracted content_text + (under ~10MB) the
    raw bytes base64-encoded. This reads the genuine stored file, so figures like an invoice's amount, sort
    code and account number come from the actual document, never a model's guess."""
    import json
    from . import attachments as A
    try:
        aid = int(att_id)
    except Exception:
        return "retrieve_attachment needs a numeric attachment id (from unified_search attachment hits)."
    rec = A.get_attachment_full(aid)
    if not rec:
        return f"No attachment with id {aid}."
    text = rec.get("content_text") or ""
    res: dict[str, Any] = {
        "attachment_id": rec.get("id"), "filename": rec.get("filename"), "mime": rec.get("mime_type"),
        "sender": rec.get("sender"), "subject": rec.get("subject"), "email_date": rec.get("email_date"),
        "content_indexed": bool(rec.get("content_indexed")),
        "extracted_text": text[:20000],
    }
    b64 = rec.get("payload_b64")
    if not text and not b64:
        res["note"] = ("This attachment predates content storage: no extracted text and no stored bytes. "
                       "Do NOT infer its contents; open it from the source email instead.")
    if include_bytes and b64:
        approx = (len(b64) * 3) // 4
        if approx <= 10 * 1024 * 1024:
            res["content_base64"] = b64
            res["size_bytes"] = approx
        else:
            res["note"] = f"file is ~{approx // (1024 * 1024)}MB, too large to inline; extracted_text is provided"
    # Trusted-payee bank-details check: if this is an invoice, diff its details against the registry so the
    # caller sees new-payee / unconfirmed / CHANGED-since-confirmed (redirection fraud) rather than treating
    # the printed numbers as automatically safe.
    try:
        from . import rtm_management as RM
        bc = RM.assess_invoice_bank_details(text, rec.get("sender") or "")
        if bc:
            res["bank_check"] = bc
    except Exception:
        pass
    return json.dumps(res, ensure_ascii=False)


def _confirm_payee_bank(payee: str, sort_code: str, account_number: str, account_name: str = "") -> str:
    """Record the owner's phone-verified confirmation of a payee's bank details into the trusted-payee registry."""
    import json
    from . import rtm_management as RM
    if not (payee or "").strip():
        return "Give me the payee (supplier name or email/domain)."
    res = RM.record_payee_bank(payee, sort_code, account_number, account_name=account_name,
                               confirmed=True, actor="owner-connector")
    if not res.get("ok"):
        return res.get("error", "could not record those details.")
    return json.dumps({"ok": True, "confirmed": True, "payee": payee, "status": res.get("status"),
                       "detail": f"Confirmed bank details for {payee}. Future invoices will be diffed against them."},
                      ensure_ascii=False)


def _rtm_status() -> str:
    from . import rtm_management as M
    rows = M.list_demands()
    live = [d for d in rows if d.get("status") not in ("Superseded", "Cancelled") and int(d.get("amount_pence") or 0) > 0]
    demanded = sum(int(d["amount_pence"]) for d in live)
    outstanding = sum(int(d.get("outstanding_pence") or 0) for d in live)
    paid = demanded - outstanding
    unpaid = [d for d in live if int(d.get("outstanding_pence") or 0) > 0]
    lines = [
        f"Demanded: {M._money(demanded)}",
        f"Paid: {M._money(paid)}",
        f"Outstanding: {M._money(outstanding)} across {len(unpaid)} flat(s)",
    ]
    if unpaid:
        lines.append("Unpaid:")
        for d in sorted(unpaid, key=lambda x: x.get("flat_id") or ""):
            lines.append(f"  {d.get('flat_id')}, {d.get('period','')}, {d.get('outstanding')} outstanding ({d.get('status')})")
    else:
        lines.append("All demands are settled.")
    # The RTM's own RECEIVING account, the one printed on every demand and demand-email cover, so the
    # connector can answer "what bank account is on the demands" without needing to open a PDF.
    try:
        from .rtm_example_court import company_profile
        bank = (company_profile() or {}).get("bank") or {}
        if bank.get("account_number") and bank.get("sort_code"):
            nm = bank.get("name") or bank.get("bank_name") or ""
            lines.append("")
            lines.append("Demands are paid INTO this account (printed on every demand):")
            lines.append(f"  {bank.get('account_name','')}, sort code {bank.get('sort_code')}, "
                         f"account {bank.get('account_number')}" + (f", {nm}" if nm else ""))
            lines.append("  Each flat pays with its own payment reference (see rtm_flat_statement).")
    except Exception:
        pass
    return "\n".join(lines)


def _rtm_knowledge(action: str, query: str, section: str) -> str:
    """Authoritative read-only RTM facts for the connector (bank account, budget, parties, matters,
    suppliers, deadlines, history), via the same knowledge base Mongoose's own assistant reads."""
    import json
    from .status_tools import tool_rtm_knowledge
    args: dict[str, Any] = {"action": (action or "index").strip().lower() or "index"}
    if query:
        args["query"] = query
    if section:
        args["section"] = section
    try:
        res = asyncio.run(tool_rtm_knowledge(args))
    except Exception as e:
        return f"rtm_knowledge failed: {type(e).__name__}: {e}"
    return json.dumps(res, default=str, indent=1)[:14000]


def _rtm_flat_statement(flat: str) -> str:
    from . import rtm_management as M
    fid, cands, err = _resolve_flat(flat)
    if not fid:
        return _ambig(cands) if cands else err
    st = M.flat_statement(fid)
    dem = M.list_demands(flat=fid)
    fl = M._get_flat(fid) or {}
    name = str(st.get("leaseholder_name") or fl.get("leaseholder_name") or "")
    out = [f"Flat {fid}, {name}".rstrip(),
           f"Balance: {st.get('balance')} ({st.get('position')})"]
    # LPE1 4.1.1 (annual service charge for the flat) and 4.6.1 (reserve held for the flat), the two
    # figures the seller-pack exercise struggled to get by hand. Read-only.
    live = [d for d in dem if d.get("status") not in ("Superseded", "Cancelled")]
    demanded = sum(int(d.get("amount_pence") or 0) for d in live)
    if demanded:
        out.append(f"Service charge demanded (live): {M._money(demanded)} across {len(live)} demand(s)")
    try:
        from .rtm_trust import flat_reserve_balance
        rb = flat_reserve_balance(fid)
        if rb.get("reserve_pence"):
            out.append(f"Reserve fund held for this flat: {rb.get('reserve')}")
    except Exception:
        pass
    # Contacts, from the reconciled schema: primary (on file) plus up to two alternate emails and one
    # alternate postal address. Coerced to str defensively (a raw tuple here once broke the whole tool).
    emails = [str(e) for e in (fl.get("leaseholder_email"), fl.get("leaseholder_email_alt1"),
                               fl.get("leaseholder_email_alt2")) if str(e or "").strip()]
    addrs = [str(a) for a in (fl.get("correspondence_address"), fl.get("correspondence_address_alt1"))
             if str(a or "").strip()]
    if emails:
        out.append("Email(s): " + " | ".join(emails) + (" (first is primary/on file)" if len(emails) > 1 else ""))
    if addrs:
        out.append("Address(es): " + " | ".join(addrs) + (" (first is primary/on file)" if len(addrs) > 1 else ""))
    if dem:
        out.append("Demands:")
        for d in dem:
            out.append(f"  {d.get('document_number')}, {d.get('period','')}, {d.get('amount')}, {d.get('status')} (outstanding {d.get('outstanding')})")
    return "\n".join(out)


def _rtm_unpaid_demands() -> str:
    from . import rtm_management as M
    rows = M.list_demands()
    unpaid = [d for d in rows if int(d.get("amount_pence") or 0) > 0
              and int(d.get("outstanding_pence") or 0) > 0
              and d.get("status") not in ("Superseded", "Cancelled")]
    if not unpaid:
        return "No unpaid demands, every flat is settled."
    lines = [f"{len(unpaid)} unpaid demand(s):"]
    for d in sorted(unpaid, key=lambda x: x.get("flat_id") or ""):
        lines.append(f"  {d.get('flat_id')}, {d.get('period','')}, {d.get('outstanding')} outstanding, due {d.get('due_date','')}")
    return "\n".join(lines)


def _reminders_list() -> str:
    from .time_alerts import list_alerts
    alerts = list_alerts(active_only=True)
    if not alerts:
        return "No upcoming reminders or alarms."
    lines = ["Upcoming:"]
    for a in alerts[:30]:
        lines.append(f"  [{a.get('kind')}] {a.get('label') or '(no label)'}, {a.get('fire_at')}")
    return "\n".join(lines)


# ── WRITE + richer READ tools ─────────────────────────────────────────────────
def _set_reminder(text: str, when: str) -> str:
    """Parse a natural-language time (Europe/London) and record a Mongoose-owned reminder that will
    fire to the owner's phone. Reuses the same deterministic parser the CLI/voice path uses."""
    text = (text or "").strip()
    when = (when or "").strip()
    if not text:
        return "I need something to remind you of (the 'text')."
    if not when:
        return "I need a time (the 'when'), e.g. 'tomorrow 9am' or 'in 2 hours'."
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from .time_parse import parse_when
    from .time_alerts import create_alert
    now_london = datetime.now(ZoneInfo("Europe/London"))
    parsed = parse_when(when, now=now_london)
    if not parsed:
        return (f"I couldn't work out a time from {when!r}. Try 'tomorrow 9am', 'in 2 hours', "
                "'Friday 18:30', or an ISO datetime.")
    alert = create_alert(kind="reminder", fire_at=parsed["fire_at"], label=text,
                         recurrence=parsed.get("recurrence"), delivery=["push"])
    fire_local = parsed["fire_at"].astimezone(ZoneInfo("Europe/London"))
    when_s = fire_local.strftime("%a %-d %b %Y at %-I:%M %p")
    rec = alert.get("recurrence")
    rec_s = {"daily": " (every day)", "weekdays": " (weekdays)", "weekends": " (weekends)"}.get(rec, "")
    return f"Reminder set: '{text}', {when_s}{rec_s}. I'll push it to your phone. (id {alert.get('id')})"


def _unified_search(query: str, limit: int) -> str:
    """Cross-source search over files, attachments, emails, voicemails, notifications and knowledge."""
    query = (query or "").strip()
    if not query:
        return "Give me something to search for (the 'query')."
    limit = max(1, min(int(limit or 10), 25))
    from .find import find
    res = find(query, limit=limit, surface="connector")
    hits = res.get("results") or []
    if not hits:
        if res.get("empty_index"):
            return f"No results for {query!r}, the searchable index is currently empty."
        return f"No results for {query!r}."
    lines = [f"{res.get('count', len(hits))} hit(s) for {query!r}:"]
    for h in hits:
        kind = h.get("kind") or "item"
        title = (h.get("title") or "(untitled)").strip()
        date = (h.get("date") or "").strip()
        snip = (h.get("snippet") or "").strip().replace("\n", " ")
        line = f"  [{kind}] {title}"
        if date:
            line += f", {date}"
        if snip:
            line += f", {snip[:160]}"
        # Emit a RETRIEVABLE HANDLE so the caller can pull the real bytes instead of trusting the snippet
        # (or, worse, guessing). Attachments (e.g. invoice PDFs) round-trip via retrieve_attachment; this
        # is the fix for "found the invoice but couldn't open it, so the numbers got made up".
        att = h.get("attachment_id")
        if kind == "attachment" and att is not None:
            line += f"  -> retrieve_attachment(id={att}) for the real content"
        elif kind == "file" and h.get("native_id") is not None:
            line += f"  -> retrieve_file(id={h.get('native_id')})"
        elif h.get("item_ref"):
            line += f"  (item_ref={h.get('item_ref')})"
        lines.append(line)
    return "\n".join(lines)


def _mongoose_chat(message: str) -> str:
    """Run a real Mongoose chat turn (local model + tool loop) and return its reply. Uses a dedicated
    connector conversation thread so it has continuity without stepping on the Cockpit chat."""
    message = (message or "").strip()
    if not message:
        return "Give me a message to send to Mongoose."

    async def _run() -> dict[str, Any]:
        from ..llm import chat_with_memory
        return await asyncio.wait_for(
            chat_with_memory(message, conversation_id="mcp-connector", role="chat"),
            timeout=120,
        )

    result = asyncio.run(_run())
    reply = (result.get("reply") or result.get("response") or result.get("text") or "").strip()
    # The financial anti-fabrication net lives in llm.chat_with_memory (grounded-aware, so it covers this
    # path too, mongoose_chat runs that exact loop). We deliberately do NOT re-append a caveat here, to
    # avoid a doubled warning.
    return reply or "(Mongoose returned an empty reply.)"


def _report_bug(summary: str, detail: str) -> str:
    """File a bug/feedback via the real bug workflow store, attach any detail, and surface it to the
    owner as a notification so it is not silently buried."""
    summary = (summary or "").strip()
    detail = (detail or "").strip()
    if not summary:
        return "I need a one-line summary of the bug or feedback."
    from .bug_workflow import log_bug, add_bug_detail
    res = log_bug(summary, actor="claude-connector")
    if not res.get("ok"):
        return f"Could not file the bug: {res.get('error', 'unknown error')}"
    bug_id = res.get("bug_id")
    if detail and bug_id:
        try:
            add_bug_detail(bug_id, detail, actor="claude-connector")
        except Exception:
            pass
    # Surface to the owner so it is visible on the dashboard/phone, best-effort.
    try:
        from .notifications import NotificationIn, store_notification
        store_notification(NotificationIn(
            source="mongoose", app="bug", title=f"New bug filed ({bug_id})",
            body=summary + (f"\n{detail}" if detail else ""),
            privacy="medium", urgency="normal", device="connector"))
    except Exception:
        pass
    return f"Filed bug {bug_id}: '{res.get('summary', summary)}' (status {res.get('status', 'logged')})."


_MCP_MAX_BYTES = 25 * 1024 * 1024  # match rtm_docs' cap so we fail fast, not after decoding


def _upload_file(filename: str, content_base64: str, doc_type: str,
                 flat_id: str, note: str) -> str:
    """Decode a base64 payload and ingest it through the RTM document store (store + extract + index +
    correlate). Defaults doc_type to other_evidence; enforces the same 25MB cap as rtm_docs."""
    filename = (filename or "").strip()
    if not filename:
        return "I need a filename."
    if not content_base64:
        return "I need the file contents (content_base64)."
    try:
        content = base64.b64decode(content_base64, validate=False)
    except Exception as exc:
        return f"Could not decode content_base64: {exc}"
    if not content:
        return "The decoded file is empty."
    if len(content) > _MCP_MAX_BYTES:
        return f"File too large ({len(content) // (1024 * 1024)}MB; max 25MB)."
    from . import rtm_docs
    doc_type = (doc_type or "").strip().lower() or "other_evidence"
    if doc_type not in rtm_docs.DOC_TYPES:
        return f"Unknown doc_type {doc_type!r}; choose one of {', '.join(rtm_docs.DOC_TYPES)}."
    res = rtm_docs.ingest(content=content, filename=filename, doc_type=doc_type,
                          flat_id=(flat_id or None), note=(note or ""),
                          uploaded_by="claude-connector", role="owner", source="connector")
    if not res.get("ok"):
        return f"Upload failed: {res.get('error', 'unknown error')}"
    parts = [f"Ingested '{res.get('filename', filename)}' as {res.get('doc_type', doc_type)}, document id {res.get('id')}."]
    if res.get("flat_id"):
        parts.append(f"Linked to flat {res.get('flat_id')}.")
    parts.append(f"Extracted {res.get('extracted_chars', 0)} chars of text; {res.get('correlations', 0)} correlation(s) found.")
    parts.append("It now appears in unified search and will be OCR-backfilled and analysed on the next pass.")
    return " ".join(parts)


def _asjson(obj: Any, limit: int = 120000) -> str:
    # Compact JSON so bounded RTM datasets fit whole; the cap is only a runaway guard. If a result would
    # exceed it, return a small valid JSON note rather than truncating into invalid JSON.
    import json
    s = json.dumps(obj, default=str, separators=(",", ":"))
    if len(s) <= limit:
        return s
    return json.dumps({"truncated": True, "reason": f"result exceeded {limit} chars; narrow the query",
                       "preview": s[:2000]})


# ── Comprehensive RTM read tools (all read-only; the async wrappers bridge with asyncio.run, exactly
#    as _rtm_knowledge does, because _call_tool runs in a worker thread with no live event loop). ──
def _rtm_dashboard() -> str:
    from . import rtm_management as M
    return _asjson(M.management_dashboard())


def _rtm_reconciliation(year: str) -> str:
    from . import rtm_management as M
    return _asjson(M.reconciliation_view(year or None))


def _rtm_arrears_view() -> str:
    from . import rtm_management as M
    return _asjson(M.arrears())


def _rtm_finances(view: str) -> str:
    from . import rtm_management as M
    v = (view or "funds").strip().lower()
    fns = {"funds": M.fund_position, "budget": lambda: M.budget_actuals("2027"), "cashflow": M.cashflow,
           "payables": M.list_payables, "fund_reconciliation": M.fund_reconciliation,
           "subaccount_audit": M.subaccount_audit, "collection": M.collection_position}
    fn = fns.get(v)
    if not fn:
        return f"Unknown view {view!r}. Use: {' | '.join(fns)}."
    return _asjson(fn())


def _rtm_trust_view(view: str) -> str:
    from . import rtm_trust as T
    v = (view or "transfer_status").strip().lower()
    fns = {"transfer_status": T.transfer_status, "exposure": T.exposure, "reserve_position": T.reserve_position}
    fn = fns.get(v)
    if not fn:
        return f"Unknown view {view!r}. Use: {' | '.join(fns)}."
    return _asjson(fn())


def _rtm_bookkeeper_view(view: str, fund: str) -> str:
    from . import rtm_bookkeeper as BK
    v = (view or "summary").strip().lower()
    if v == "summary":
        return _asjson(BK.summary())
    if v == "reconcile":
        return _asjson(BK.reconcile())
    if v == "ledger":
        return _asjson(BK.ledger(fund=(fund or None)))
    return f"Unknown view {view!r}. Use: summary | reconcile | ledger."


def _rtm_evidence(document_number: str, flat: str, description: str) -> str:
    from . import rtm_docs as D
    return _asjson(D.documents_for_bank_line(document_number or None, flat or None, None, description or None))


def _rtm_ontology(action: str, term: str) -> str:
    from .status_tools import tool_rtm_ontology
    args: dict[str, Any] = {"action": (action or "describe").strip().lower()}
    if term:
        args.update({"term": term, "name": term, "query": term})
    try:
        return _asjson(asyncio.run(tool_rtm_ontology(args)))
    except Exception as e:
        return f"rtm_ontology failed: {type(e).__name__}: {e}"


def _rtm_recent_activity(limit: Any) -> str:
    from .status_tools import tool_recent_activity
    try:
        return _asjson(asyncio.run(tool_recent_activity({"limit": int(limit or 20)})))
    except Exception as e:
        return f"rtm_recent_activity failed: {type(e).__name__}: {e}"


def _rtm_portal_status() -> str:
    from . import rtm_portal as P
    return _asjson({"summary": P.portal_brain_summary(), "missing": P.missing_documents()})


def _rtm_roster() -> str:
    from . import rtm_management as M
    return _asjson(M.list_flats())


def _rtm_referral_pack(flat: str, document_number: str) -> str:
    from . import rtm_management as M
    return _asjson(M.referral_pack(flat or None, document_number or None))


def _rtm_seller_pack(flat: str) -> str:
    from .rtm_seller_pack import review_sheet
    return review_sheet(flat)


def _rtm_registers_status() -> str:
    from .rtm_registers import registers_status
    return _asjson(registers_status())


def _rtm_first_stage_response(args: dict) -> str:
    """Stage-1 LPE1 reply: branded cover letter + statutory admin-charge summary. Artifacts only; sends
    nothing; never releases the completed form. Optional job details are applied to this render and, if
    save=true, remembered on the job."""
    from . import rtm_seller_pack_response as R
    flat = str(args.get("flat", "")).strip()
    if not flat:
        return "Give the flat (id or leaseholder)."
    job_fields = {k: str(args[k]).strip() for k in ("seller_name", "conveyancer_firm", "conveyancer_contact",
                  "conveyancer_email", "their_ref", "request_date") if args.get(k)}
    if bool(args.get("save")) and job_fields:
        R.set_job(flat, **job_fields)
    res = R.first_stage_response(flat, download_ttl_s=86400, **job_fields)
    return _asjson(res)


# ── Artifact-producing tools: each returns a downloadable /rtm/doc/{token} link; none send or move money. ──
def _rtm_export_tool(formats: str, include_evidence: bool, year: str) -> str:
    from . import rtm_export as X
    from .rtm_templates import mint_download
    fmts = None
    if formats:
        fmts = [f.strip() for f in str(formats).replace(",", " ").split() if f.strip()] or None
    res = X.build_export(year=(year or "2027"), formats=fmts, include_evidence=bool(include_evidence))
    if not res.get("ok") or not res.get("path"):
        return f"Export failed: {res.get('error', 'could not build the pack')}"
    tok = mint_download(res["path"], res["filename"], ttl_s=86400, media_type="application/zip")
    return _asjson({"ok": True, "filename": res["filename"], "download_url": f"/rtm/doc/{tok}",
                    "size": res.get("size"), "manifest": res.get("manifest")})


def _get_demand_pdf(flat: str, period: str, include_bytes: bool) -> str:
    from . import rtm_management as M
    from . import rtm_demand_mailing as DM
    from . import rtm_portal as P
    from .rtm_templates import mint_download
    fid, cands, err = _resolve_flat(flat)
    if not fid:
        return _ambig(cands) if cands else err
    live = [d for d in M.list_demands(flat=fid) if d.get("status") not in ("Superseded", "Cancelled")]
    if period:
        live = [d for d in live if str(d.get("period", "")) == period] or live
    if not live:
        return f"No demand found for {fid}" + (f" period {period}" if period else "") + "."
    d = sorted(live, key=lambda x: str(x.get("due_date") or ""))[-1]
    docnum = d.get("document_number")
    pdf = DM.demand_pdf_for(docnum)
    if not pdf:
        P.regenerate_demand_pdf(document_number=docnum)
        pdf = DM.demand_pdf_for(docnum)
    if not pdf:
        return f"Could not produce the demand PDF for {docnum}."
    path, sha = pdf
    fn = f"Demand_{fid}_{d.get('period', '')}.pdf"
    tok = mint_download(path, fn, ttl_s=86400, media_type="application/pdf")
    out: dict[str, Any] = {"ok": True, "flat": fid, "document_number": docnum, "period": d.get("period"),
                           "download_url": f"/rtm/doc/{tok}", "sha256": sha, "filename": fn}
    if include_bytes:
        import base64
        try:
            with open(path, "rb") as fh:
                out["content_base64"] = base64.b64encode(fh.read()).decode("ascii")
        except Exception:
            pass
    return _asjson(out)


def _rtm_generate_document(template: str, flat: str, recipient_email: str) -> str:
    from .rtm_templates import generate_document
    fid = ""
    if flat:
        rf, _c, _e = _resolve_flat(flat)
        fid = rf or flat
    res = generate_document(str(template or ""), flat_id=fid, recipient_email=(recipient_email or ""))
    if not res.get("ok"):
        reason = res.get("reason") or res.get("error") or "unknown"
        return f"Could not generate {template!r}: {reason}. {res.get('hint', '')}".strip()
    if res.get("kind") == "email":
        return _asjson({"ok": True, "kind": "email_draft", "ready_to_send": res.get("ready_to_send"),
                        "text": res.get("text")})
    return _asjson({"ok": True, "filename": res.get("filename"), "download_url": res.get("download_url", "")})


# Flat resolution is RTM logic and lives in mongoose-core; kept here under its old name.
from .rtm_management import resolve_flat as _resolve_flat  # noqa: E402,F401


def _ambig(cands: list[dict[str, Any]]) -> str:
    return "That matches more than one flat: " + ", ".join(
        f"{c.get('flat_id')} ({c.get('leaseholder_name')})" for c in cands) + ". Say which flat."


def _rtm_mark_paid(flat: str, amount: str = "", received_date: str = "") -> str:
    from .rtm_management import record_payment
    fid, cands, err = _resolve_flat(flat)
    if not fid:
        return _ambig(cands) if cands else err
    res = record_payment(flat=fid, amount=(amount or None), received_date=(received_date or None),
                         method="Bank transfer")
    if not res.get("ok"):
        return f"Could not mark {fid} paid: {res.get('error', 'unknown')}"
    if res.get("already_paid"):
        return f"{fid} is already fully paid (balance {res.get('balance', '£0.00')})."
    rc = res.get("receipt_ok")
    rmsg = (" Receipt generated." if rc is True
            else f" (receipt not generated: {res.get('receipt_error')})" if rc is False else "")
    return (f"Recorded {res.get('amount')} received for {fid} against {res.get('document_number')}, dated "
            f"{received_date or 'today'}. Balance now {res.get('balance', '£0.00')}.{rmsg} "
            f"Use rtm_send_receipt to email it.")


def _rtm_send_receipt(flat: str) -> str:
    from .rtm_portal import send_receipt_email
    fid, cands, err = _resolve_flat(flat)
    if not fid:
        return _ambig(cands) if cands else err
    res = send_receipt_email(fid)
    if not res.get("ok"):
        return f"Could not send {fid}'s receipt: {res.get('error', 'no receipt on file yet')}"
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    return f"Sent. WordPress emailed {fid}'s receipt to {data.get('email') or res.get('email') or 'the leaseholder'}."


def _rtm_subaccount_split(flat: str, year: str = "") -> str:
    from .rtm_trust import subaccount_split
    fid, cands, err = _resolve_flat(flat)
    if not fid:
        return _ambig(cands) if cands else err
    yr = (year or "").strip()
    if not yr:
        from ..db import connect
        with connect() as con:
            row = con.execute("SELECT MAX(year) FROM rtm_mgmt_budget_lines").fetchone()
        yr = str(row[0]) if row and row[0] else "2027"
    s = subaccount_split(fid, year=yr)
    sub = s.get("subaccount_split", {})
    return (f"{fid} split of {s.get('total')}: reserve fund trust {sub.get('reserve_fund_trust')}, "
            f"service charge trust {sub.get('service_charge_trust')} (reserve = {s.get('reserve_fraction_pct')}% "
            f"of the Schedule-1 half; the undercroft is all service).")


def _task_add(text: str, list_name: str = "") -> str:
    from .calendar_tasks import add_task, TaskIn
    t = (text or "").strip()
    if not t:
        return "Give me the task text."
    ln = (list_name or "").strip() or "Tasks"
    add_task(TaskIn(source="mongoose", list_name=ln, title=t[:200], status="open"))
    return f"Kept it as an open {'RTM ' if ln.upper() == 'RTM' else ''}task: {t[:200]!r}."


def _task_list(rtm_only: bool = False) -> str:
    from .chat_actions import list_tasks_reply
    return list_tasks_reply("rtm tasks" if rtm_only else "my tasks")


def _v3_bus_read(peer: str, limit: int = 20) -> str:
    """Messages addressed to a NAMED peer. The bus lives in FILES on the Pi under /config, not in SQLite,
    so there is no table to query: a peer that went looking for one found nothing and concluded, wrongly,
    that the bus was empty."""
    from .protocol_v3_runtime import read_bus
    who = (peer or "").strip().lower()
    if who not in {"chat", "code", "mongoose", "all"}:
        return "peer must be one of: chat, code, mongoose, all"
    res = read_bus(only_for_me=False, since_last=False)
    if not res.get("ok"):
        return f"Bus unreachable: {res.get('detail') or res.get('error') or 'unknown'}"
    msgs = res.get("messages", [])

    def _recipients(m: dict) -> list[str]:
        """`to` is a string OR a list: peers legitimately address several agents at once
        (to: [code, mongoose]). Comparing str(to) against a name matched neither, so a list-addressed
        message was dropped, and 'all' broadcasts were counted for a peer even when it sent them."""
        t = m.get("to")
        items = t if isinstance(t, (list, tuple, set)) else [t]
        return [str(x).strip().lower() for x in items if x is not None]

    if who != "all":
        msgs = [m for m in msgs
                # addressed to this peer directly, or broadcast to everyone,
                if (who in _recipients(m) or "all" in _recipients(m))
                # but never a peer's own message echoed back into its inbox
                and str(m.get("from", "")).strip().lower() != who]
    msgs = msgs[-max(1, min(int(limit or 20), 100)):]
    if not msgs:
        return f"No messages addressed to {who} on the bus."
    out = [f"{len(msgs)} message(s) addressed to {who}, oldest first:"]
    for m in msgs:
        out.append(
            f"\n[{m.get('timestamp', '')}] id={m.get('msg_id', '')}\n"
            f"from={m.get('from', '?')} to={m.get('to', '?')} topic={m.get('topic', '?')} "
            f"needs_ack={m.get('needs_ack')} ack_of={m.get('ack_of') or '-'}\n"
            f"{m.get('body', '')}")
    return "\n".join(out)


def _v3_bus_post(sender: str, to: str, body: str, topic: str = "note", ack_of: str = "") -> str:
    """Append to the bus, attributed to `sender`. A peer replying through Mongoose must be recorded as
    ITSELF: attributing chat's answer to mongoose would corrupt the very record the bus exists to keep."""
    from .protocol_v3 import build_envelope
    from .protocol_v3_runtime import _append_to_bus
    frm = (sender or "").strip().lower()
    if frm not in {"chat", "code", "mongoose"}:
        return "sender must be one of: chat, code, mongoose"
    try:
        env = build_envelope(topic=(topic or "note").strip(), body=body, to=(to or "all").strip().lower(),
                             needs_ack=False, ack_of=(ack_of or None))
    except ValueError as e:
        return f"Refused: {e}"
    env["from"] = frm
    env["msg_id"] = str(env.get("msg_id", "")).replace("-mongoose-", f"-{frm}-")
    res = _append_to_bus(env)
    if not res.get("ok"):
        return f"Post failed: {res.get('error') or 'unknown'}"
    return f"Posted to the bus as {frm}, addressed to {env.get('to')}. msg_id={env.get('msg_id')}"


def _call_tool(name: str, args: dict[str, Any]) -> str:
    if name == "v3_bus_read":
        return _v3_bus_read(str(args.get("peer", "")), args.get("limit", 20))
    if name == "v3_bus_post":
        return _v3_bus_post(str(args.get("sender", "")), str(args.get("to", "all")),
                            str(args.get("body", "")), str(args.get("topic", "note")),
                            str(args.get("ack_of", "")))
    if name == "describe_system":
        return _describe_system()
    if name == "list_files":
        return _list_files(str(args.get("doc_type", "")), str(args.get("flat_id", "")), args.get("limit", 50))
    if name == "retrieve_file":
        return _retrieve_file(args.get("id"), bool(args.get("include_bytes", True)))
    if name == "retrieve_attachment":
        return _retrieve_attachment(args.get("id"), bool(args.get("include_bytes", True)))
    if name == "confirm_payee_bank":
        return _confirm_payee_bank(str(args.get("payee", "")), str(args.get("sort_code", "")),
                                   str(args.get("account_number", "")), str(args.get("account_name", "")))
    if name == "rtm_status":
        return _rtm_status()
    if name == "rtm_knowledge":
        return _rtm_knowledge(str(args.get("action", "")), str(args.get("query", "")), str(args.get("section", "")))
    if name == "rtm_dashboard":
        return _rtm_dashboard()
    if name == "rtm_reconciliation":
        return _rtm_reconciliation(str(args.get("year", "")))
    if name == "rtm_arrears":
        return _rtm_arrears_view()
    if name == "rtm_finances":
        return _rtm_finances(str(args.get("view", "")))
    if name == "rtm_trust":
        return _rtm_trust_view(str(args.get("view", "")))
    if name == "rtm_bookkeeper":
        return _rtm_bookkeeper_view(str(args.get("view", "")), str(args.get("fund", "")))
    if name == "rtm_evidence":
        return _rtm_evidence(str(args.get("document_number", "")), str(args.get("flat", "")), str(args.get("description", "")))
    if name == "rtm_ontology":
        return _rtm_ontology(str(args.get("action", "")), str(args.get("term", "")))
    if name == "rtm_recent_activity":
        return _rtm_recent_activity(args.get("limit"))
    if name == "rtm_portal_status":
        return _rtm_portal_status()
    if name == "rtm_roster":
        return _rtm_roster()
    if name == "rtm_referral_pack":
        return _rtm_referral_pack(str(args.get("flat", "")), str(args.get("document_number", "")))
    if name == "rtm_seller_pack":
        return _rtm_seller_pack(str(args.get("flat", "")))
    if name == "rtm_registers_status":
        return _rtm_registers_status()
    if name == "rtm_first_stage_response":
        return _rtm_first_stage_response(args)
    if name == "rtm_export":
        return _rtm_export_tool(str(args.get("formats", "")), bool(args.get("include_evidence", True)), str(args.get("year", "")))
    if name == "get_demand_pdf":
        return _get_demand_pdf(str(args.get("flat", "")), str(args.get("period", "")), bool(args.get("include_bytes", False)))
    if name == "rtm_generate_document":
        return _rtm_generate_document(str(args.get("template", "")), str(args.get("flat", "")), str(args.get("recipient_email", "")))
    if name == "rtm_flat_statement":
        return _rtm_flat_statement(str(args.get("flat", "")))
    if name == "rtm_unpaid_demands":
        return _rtm_unpaid_demands()
    if name == "reminders_list":
        return _reminders_list()
    if name == "set_reminder":
        return _set_reminder(str(args.get("text", "")), str(args.get("when", "")))
    if name == "unified_search":
        return _unified_search(str(args.get("query", "")), args.get("limit", 10))
    if name == "mongoose_chat":
        return _mongoose_chat(str(args.get("message", "")))
    if name == "report_bug":
        return _report_bug(str(args.get("summary", "")), str(args.get("detail", "")))
    if name == "upload_file":
        return _upload_file(str(args.get("filename", "")), str(args.get("content_base64", "")),
                            str(args.get("doc_type", "")), str(args.get("flat_id", "")),
                            str(args.get("note", "")))
    if name == "rtm_mark_paid":
        return _rtm_mark_paid(str(args.get("flat", "")), str(args.get("amount", "")),
                              str(args.get("received_date", "")))
    if name == "rtm_send_receipt":
        return _rtm_send_receipt(str(args.get("flat", "")))
    if name == "rtm_subaccount_split":
        return _rtm_subaccount_split(str(args.get("flat", "")), str(args.get("year", "")))
    if name == "task_add":
        return _task_add(str(args.get("text", "")), str(args.get("list", "")))
    if name == "task_list":
        return _task_list(bool(args.get("rtm", False)))
    raise ValueError(f"unknown tool {name!r}")


# ── JSON-RPC dispatch ────────────────────────────────────────────────────────
def _one(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Handle a single JSON-RPC message. Returns a response dict, or None for notifications
    (no id) which get no reply per JSON-RPC."""
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": _server_info(),
            "instructions": ("Full read and write access to Mongoose (a live RTM / service-charge system). "
                             "Call describe_system first for the live tool + schema map. For RTM questions, "
                             "prefer the STRUCTURED tools below over mongoose_chat, which is a last resort and "
                             "can misroute; never take a figure (a bank account, amount, sort code) from a "
                             "chat summary or a search snippet, read it from a structured tool or the actual "
                             "document. "
                             "STATE: rtm_dashboard (one-call state + next actions, ask this first), "
                             "rtm_flat_statement (one flat's balance, demands, reserve held, annual charge, "
                             "contacts), rtm_status (totals + the receiving bank account printed on demands), "
                             "rtm_arrears / rtm_unpaid_demands. "
                             "DETAIL: rtm_finances (view=funds|budget|cashflow|payables|fund_reconciliation|"
                             "subaccount_audit|collection), rtm_trust (view=transfer_status|exposure|"
                             "reserve_position), rtm_bookkeeper (the trust cash book), rtm_reconciliation "
                             "(how the bank reconciles), rtm_knowledge (authoritative facts: bank, budget, "
                             "parties, legal matters), rtm_ontology (what a term means), rtm_roster "
                             "(leaseholders), rtm_evidence (the invoice/receipt behind a bank line), "
                             "rtm_referral_pack, rtm_recent_activity, rtm_portal_status. "
                             "ARTIFACTS (return a download link, never send): rtm_export (the accountant "
                             "pack), get_demand_pdf (a flat's demand PDF), rtm_generate_document (a branded "
                             "receipt/letter). "
                             "DOCUMENTS/SEARCH: list_files + retrieve_file (ingested store); unified_search "
                             "across files/emails/voicemails/knowledge, then retrieve_attachment to pull the "
                             "REAL bytes of an attachment (demand PDFs are now indexed and retrievable this "
                             "way). "
                             "WRITE (careful): rtm_mark_paid (record a payment + receipt), rtm_send_receipt "
                             "(email a branded receipt), rtm_subaccount_split, set_reminder, task_add/"
                             "task_list, upload_file, report_bug, v3_bus_post. IMPORTANT: never state an "
                             "invoice's amount, sort code or account number from a snippet or a mongoose_chat "
                             "summary alone; retrieve the document or use the structured tool first."),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if isinstance(method, str) and method.startswith("notifications/"):
        return None  # notifications get no response
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_specs()}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            text = _call_tool(name, args if isinstance(args, dict) else {})
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"Tool error: {e}"}], "isError": True}}
    # Unknown method: error only if it was a request (had an id).
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None


def handle(payload: Any) -> Any:
    """Handle a JSON-RPC request or batch. Returns a dict, a list, or None (nothing to send)."""
    if isinstance(payload, list):
        out = [r for r in (_one(m) for m in payload) if r is not None]
        return out or None
    return _one(payload)
