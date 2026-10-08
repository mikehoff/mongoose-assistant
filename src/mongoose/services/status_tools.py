"""Read-only status tools for chat, plain-language access to every subsystem.

These are the safe 80% of the chat-integration design (docs/CHAT_INTEGRATION_DESIGN.md): each tool is a
thin wrapper over a function that ALREADY exists (doctor, self_harden, claim_consistency, scheduler,
rtm_management, updater, ddns, benchmark, receipts). Nothing here mutates state, sends, or acts, so
these belong in READ_TOOLS and can be chained freely by the planner.

The centrepiece is `system_status(area)`: one tool that answers most status questions, keeping the
model's tool list short (short lists plan better). Bespoke read tools are added only where a query is
richer than a status roll-up (e.g. a specific flat's RTM statement).
"""
from __future__ import annotations

from typing import Any

# Each executor is defensive: a subsystem that errors or isn't configured yields a {ok, note} dict
# rather than raising, so one unavailable area never breaks the whole status read.


def _safe(label: str, fn) -> dict[str, Any]:
    try:
        return {"ok": True, label: fn()}
    except Exception as exc:
        return {"ok": False, "note": f"{label} unavailable: {str(exc)[:160]}"}


def _scheduler_brief() -> dict[str, Any]:
    from .scheduler import job_status
    jobs = job_status()
    bad = [j for j in jobs if str(j.get("last_status", "")).lower() in {"error", "dead", "stale"}]
    disabled = [j for j in jobs if j.get("enabled") is False]
    return {"jobs": len(jobs), "unhealthy": len(bad), "disabled": len(disabled),
            "unhealthy_jobs": [j.get("job") for j in bad][:8],
            "disabled_jobs": [j.get("job") for j in disabled][:12]}


def _consistency_brief() -> dict[str, Any]:
    from . import claim_consistency as CC
    r = CC.run_builtin()
    return {"claims_checked": r["total"], "diverging": r["diverging_count"],
            "diverging_keys": [d["key"] for d in r.get("diverging", [])]}


def _hardening_brief() -> dict[str, Any]:
    from . import self_harden as H
    s = H.status()
    return {"open_observations": s.get("open", s.get("open_observations")), "last_scan": s.get("last_scan")}


def _updates_brief() -> dict[str, Any]:
    from ..updater import list_update_stages
    stages = list_update_stages()
    pending = [s for s in stages if not s.get("approved")]
    return {"staged": len(stages), "unapproved": len(pending),
            "stages": [{"id": s.get("stage_id"), "target": s.get("target_version"),
                        "approved": bool(s.get("approved"))} for s in stages[:5]]}


def _connectivity_brief() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        from ..ddns import status as ddns_status
        out["ddns"] = ddns_status()
    except Exception as exc:
        out["ddns"] = {"note": f"ddns status unavailable: {str(exc)[:120]}"}
    try:
        from ..app import _lan_reachable_now
        out["lan_reachable"] = bool(_lan_reachable_now())
    except Exception:
        out["lan_reachable"] = None
    return out


def _connectors_brief() -> dict[str, Any]:
    """LIVE connector health for the brain: which connectors actually work right now (not just whether
    they're configured). Surfaces the 'broken' set (configured-but-not-usable) loudly so chat/voice can
    say 'Gmail is set up but not working' instead of a false 'connected'. Each probe is a single cheap
    read-only call. This is what makes 'are my connectors working?' answerable in the single brain."""
    from ..connector_probes import probe_all
    agg = probe_all()
    sm = agg["summary"]
    # compact per-connector view (name + working/not/unproven + one-line detail) , the brain doesn't
    # need latency/evidence noise, just the actionable picture.
    connectors = []
    for c in agg["connectors"]:
        if c["usable"] is True:
            state = "working"
        elif c["usable"] is False:
            state = "not working" if c["configured"] else "not set up"
        else:
            state = "set up (not live-tested)" if c["configured"] else "not set up"
        connectors.append({"name": c["name"], "state": state, "detail": c["detail"]})
    return {
        "summary": sm,
        "working": [c["name"] for c in connectors if c["state"] == "working"],
        "broken": sm.get("broken_names", []),
        "connectors": connectors,
        "boundary": "Live read-only probe: each connector is tested with one cheap call; a token that "
                    "exists but is revoked, or a host that's down, shows as not working, not connected.",
    }


def _benchmark_brief() -> dict[str, Any]:
    from ..benchmark import load_results_for_recommend
    results, scores = load_results_for_recommend()
    return {"models_benchmarked": len(results), "scores": scores}


def _tasks_brief() -> dict[str, Any]:
    """All synced tasks, grouped by list, with status and due date. This is the data the
    Microsoft To Do sync puts into the brain, not a live API call."""
    from ..db import connect
    with connect() as c:
        rows = c.execute(
            "SELECT list_name, title, status, due_at FROM tasks ORDER BY list_name, due_at NULLS LAST"
        ).fetchall()
    if not rows:
        return {"count": 0, "lists": [], "note": "no tasks synced yet: run a Microsoft To Do sync first"}
    by_list: dict[str, list[dict]] = {}
    for r in rows:
        lst = r[0] or "Tasks"
        if lst not in by_list:
            by_list[lst] = []
        by_list[lst].append({"title": r[1], "status": r[2] or "not_started", "due": r[3]})
    return {"count": len(rows), "lists": [
        {"name": name, "task_count": len(tasks), "tasks": tasks[:50]}
        for name, tasks in by_list.items()
    ]}


def _calendar_brief() -> dict[str, Any]:
    """Upcoming calendar events from synced calendar data."""
    from .calendar_tasks import events_today
    events = events_today()
    if not events:
        return {"count": 0, "events": [], "note": "no events today (or calendar not synced yet)"}
    return {"count": len(events), "events": [
        {"title": e.get("title", e.get("summary", "")), "start": e.get("start"),
         "end": e.get("end"), "location": e.get("location")}
        for e in events[:20]
    ]}





async def _home_brief() -> dict[str, Any]:
    # Reachability of Home Assistant , a cheap read that answers "is the home system up?"
    try:
        from .entity_resolver import _fetch_entities
        ents = await _fetch_entities()
        return {"reachable": True, "entities": len(ents)}
    except Exception as exc:
        return {"reachable": False, "note": str(exc)[:120]}


def _rtm_brief() -> str:
    from .rtm_management import format_status
    return format_status()


def _rtm_bank_feed_brief() -> dict[str, Any]:
    from .rtm_bank_feed_playbook import playbook_status
    s = playbook_status()
    return {
        "accounts": s.get("accounts", []),
        "tests": s.get("tests", []),
        "next_actions": s.get("next_actions", [])[:6],
        "providers": s.get("providers", []),
        "boundary": s.get("boundary"),
    }

def _resources_brief() -> dict[str, Any]:
    from .resource_guard import assess_runtime_pressure
    from .scheduler import active_job_status
    r = assess_runtime_pressure(include_process_table=True)
    r["active_scheduler_jobs"] = active_job_status()
    return r


_AREAS = {
    "scheduler": lambda: _safe("scheduler", _scheduler_brief),
    "resources": lambda: _safe("resources", _resources_brief),
    "system_resources": lambda: _safe("resources", _resources_brief),
    "hardening": lambda: _safe("hardening", _hardening_brief),
    "consistency": lambda: _safe("consistency", _consistency_brief),
    "updates": lambda: _safe("updates", _updates_brief),
    "connectivity": lambda: _safe("connectivity", _connectivity_brief),
    "connectors": lambda: _safe("connectors", _connectors_brief),
    "rtm": lambda: _safe("rtm", _rtm_brief),
    "rtm_bank_feeds": lambda: _safe("rtm_bank_feeds", _rtm_bank_feed_brief),
    "bank_feeds": lambda: _safe("bank_feeds", _rtm_bank_feed_brief),
    "benchmark": lambda: _safe("benchmark", _benchmark_brief),
    "tasks": lambda: _safe("tasks", _tasks_brief),
    "calendar": lambda: _safe("calendar", _calendar_brief),
}


async def _home_area() -> dict[str, Any]:
    try:
        return {"ok": True, "home": await _home_brief()}
    except Exception as exc:
        return {"ok": False, "note": f"home unavailable: {str(exc)[:140]}"}


async def tool_system_status(args: dict[str, Any]) -> dict[str, Any]:
    """Health roll-up across subsystems, or one area in depth. area in: all/scheduler/hardening/
    consistency/updates/connectivity/connectors/rtm/rtm_bank_feeds/bank_feeds/benchmark/home/tasks/
    calendar. Note: 'connectors' runs LIVE probes (network calls) so it's only included when asked for
    by name, not in the 'all' sweep. area=resources reports OS/process memory, loop pressure and active scheduler jobs."""
    area = (args.get("area") or "all").strip().lower()
    if area == "home":
        return {"area": "home", **(await _home_area())}
    if area != "all" and area in _AREAS:
        return {"area": area, **_AREAS[area]()}
    # all: the master doctor roll-up plus a one-line brief per subsystem. We skip 'connectors' here
    # because its live probes make network calls; it's reachable on demand via area="connectors".
    out: dict[str, Any] = {"area": "all"}
    try:
        from ..doctor import system_status as doctor_status
        out["overview"] = doctor_status()
    except Exception as exc:
        out["overview"] = {"note": f"doctor unavailable: {str(exc)[:140]}"}
    out["subsystems"] = {name: builder() for name, builder in _AREAS.items() if name != "connectors"}
    out["subsystems"]["home"] = await _home_area()
    return out


async def tool_consistency_check(args: dict[str, Any]) -> dict[str, Any]:
    """Run the claim-consistency check: are any two subsystems making the same claim with different
    logic? Read-only. Returns each claim and, on divergence, which authorities disagree."""
    from . import claim_consistency as CC
    return CC.run_builtin()


async def tool_rtm_statement(args: dict[str, Any]) -> dict[str, Any]:
    """A specific flat's RTM statement (balance, demands, payments) AND how each demand splits across the
    service-charge schedules (the "subaccounts": Building service costs + Undercroft parking). Resolved by
    flat id OR by leaseholder name/surname (e.g. "Morgan" -> 1ECF1). Arrears across all flats if nothing
    is given. Read-only."""
    from .rtm_management import flat_statement, arrears, list_flats
    flat = (args.get("flat") or "").strip()
    name = (args.get("leaseholder") or args.get("name") or "").strip()
    resolved_from = None
    # Resolve a leaseholder name/surname to their flat when no flat id was given, so questions phrased by
    # person ("how does Morgan's demand split") work, not just questions phrased by flat id.
    if not flat and name:
        qtokens = [t for t in name.lower().split() if t]
        def _hay(f):
            return (str(f.get("leaseholder_name") or "") + " " + str(f.get("payment_reference") or "")).lower()
        # Match when EVERY word of the query appears somewhere in the name, so "Jane Smith" matches
        # "Dr Jane Mary Smith" (not just an exact contiguous substring), while "Smith" alone still
        # matches every Morgan (and is reported as ambiguous).
        matches = [f for f in (list_flats(include_excluded=True) or [])
                   if qtokens and all(t in _hay(f) for t in qtokens)]
        if len(matches) == 1:
            flat = str(matches[0].get("flat_id") or "")
            resolved_from = name
        elif len(matches) > 1:
            return {"ok": False, "ambiguous": True,
                    "error": f"{len(matches)} flats match {name!r}: ask which flat",
                    "candidates": [{"flat_id": m.get("flat_id"), "leaseholder_name": m.get("leaseholder_name")} for m in matches]}
        else:
            return {"ok": False, "error": f"no leaseholder matching {name!r}"}
    if flat:
        try:
            out: dict[str, Any] = {"flat": flat, "statement": flat_statement(flat)}
            if resolved_from:
                out["resolved_from"] = resolved_from
            # The canonical split, computed by the engine (rtm_trust.subaccount_split), NEVER hand-derived.
            # It returns BOTH the schedule split (Sch1 building / Sch2 undercroft) AND the sub-account /
            # trust split (reserve_fund_trust / service_charge_trust), clearly labelled so the two can
            # never be confused. The model must read these numbers, not compute its own.
            try:
                from .rtm_trust import subaccount_split
                out["splits"] = subaccount_split(flat)
            except Exception:
                pass
            return out
        except Exception as exc:
            return {"ok": False, "error": f"no statement for flat {flat!r}: {str(exc)[:140]}"}
    return {"arrears": arrears()}


async def tool_rtm_mark_paid(args: dict[str, Any]) -> dict[str, Any]:
    """Mark a flat's service-charge demand as PAID: records a received payment (bank credit + allocation),
    clearing the balance and flipping the demand to Paid. Records a receipt; never moves money. Idempotent.
    Resolve by flat id, leaseholder name/surname, or demand document number."""
    from .rtm_management import record_payment, list_flats
    flat = (args.get("flat") or "").strip()
    doc = (args.get("document_number") or "").strip()
    name = (args.get("leaseholder") or args.get("name") or "").strip()
    if not flat and not doc and name:
        qtokens = [t for t in name.lower().split() if t]
        def _hay(f):
            return (str(f.get("leaseholder_name") or "") + " " + str(f.get("payment_reference") or "")).lower()
        matches = [f for f in (list_flats(include_excluded=True) or []) if qtokens and all(t in _hay(f) for t in qtokens)]
        if len(matches) == 1:
            flat = str(matches[0].get("flat_id") or "")
        elif len(matches) > 1:
            return {"ok": False, "ambiguous": True, "error": f"{len(matches)} flats match {name!r}: say which flat",
                    "candidates": [{"flat_id": m.get("flat_id"), "leaseholder_name": m.get("leaseholder_name")} for m in matches]}
        else:
            return {"ok": False, "error": f"no leaseholder matching {name!r}"}
    return record_payment(flat=flat, document_number=doc, amount=args.get("amount"),
                          received_date=(args.get("received_date") or None),
                          method=str(args.get("method") or "Bank transfer"))


async def tool_rtm_send_receipt(args: dict[str, Any]) -> dict[str, Any]:
    """Email a leaseholder their payment receipt (already on file) with a branded cover note, sent from the
    RTM address. Resolve by flat id or leaseholder name."""
    from .rtm_portal import send_receipt_email
    from .rtm_management import list_flats
    flat = (args.get("flat") or "").strip()
    name = (args.get("leaseholder") or args.get("name") or "").strip()
    if not flat and name:
        qtokens = [t for t in name.lower().split() if t]
        def _hay(f):
            return (str(f.get("leaseholder_name") or "") + " " + str(f.get("payment_reference") or "")).lower()
        matches = [f for f in (list_flats(include_excluded=True) or []) if qtokens and all(t in _hay(f) for t in qtokens)]
        if len(matches) == 1:
            flat = str(matches[0].get("flat_id") or "")
        elif len(matches) > 1:
            return {"ok": False, "ambiguous": True, "error": f"{len(matches)} flats match {name!r}: say which flat",
                    "candidates": [{"flat_id": m.get("flat_id"), "leaseholder_name": m.get("leaseholder_name")} for m in matches]}
        else:
            return {"ok": False, "error": f"no leaseholder matching {name!r}"}
    if not flat:
        return {"ok": False, "error": "a flat or leaseholder is required"}
    return send_receipt_email(flat, str(args.get("receipt_no") or ""))


async def tool_rtm_plan_letter(args: dict[str, Any]) -> dict[str, Any]:
    """Read, or send, a leaseholder's payment-plan letter.

    action 'preview' (default) composes the letter and returns it WITHOUT sending, so the owner reads the
    exact words before a real leaseholder does. action 'send' emails it, and only ever after that read:
    this letter once fired automatically off a WordPress button and a wrong schedule reached a
    leaseholder with nobody having seen it, which is why the send is confirm-gated rather than one click.

    The letter states the position as at TODAY, not the schedule as agreed: instalments already paid are
    acknowledged, and any that have fallen due unpaid are summed into a single catch-up figure payable
    within 7 days, so it stays truthful however long after agreement it is sent.
    """
    from .rtm_portal import build_payment_plan_letter, send_payment_plan_letter
    from .rtm_management import list_flats
    flat = (args.get("flat") or "").strip()
    name = (args.get("leaseholder") or args.get("name") or "").strip()
    if not flat and name:
        qtokens = [t for t in name.lower().split() if t]
        def _hay(f):
            return (str(f.get("leaseholder_name") or "") + " " + str(f.get("payment_reference") or "")).lower()
        matches = [f for f in (list_flats(include_excluded=True) or []) if qtokens and all(t in _hay(f) for t in qtokens)]
        if len(matches) == 1:
            flat = str(matches[0].get("flat_id") or "")
        elif len(matches) > 1:
            return {"ok": False, "ambiguous": True, "error": f"{len(matches)} flats match {name!r}: say which flat",
                    "candidates": [{"flat_id": m.get("flat_id"), "leaseholder_name": m.get("leaseholder_name")} for m in matches]}
        else:
            return {"ok": False, "error": f"no leaseholder matching {name!r}"}
    if not flat:
        return {"ok": False, "error": "a flat or leaseholder is required"}
    action = (args.get("action") or "preview").strip().lower()
    erratum = str(args.get("erratum") or "")
    if action in ("send", "email"):
        return send_payment_plan_letter(flat, erratum=erratum, confirm=True)
    draft = build_payment_plan_letter(flat, erratum=erratum)
    if not draft.get("ok"):
        return draft
    # Return the letter as READABLE TEXT as well as html: the point of a preview is that the owner can
    # actually read it in chat, and a wall of markup is not a thing anyone proofreads.
    import re as _re
    from html import unescape as _unescape
    t = _re.sub(r"<br\s*/?>", "\n", draft.get("body_html") or "")
    t = _re.sub(r"</(p|div|li|h\d)>", "\n", t)
    t = _unescape(_re.sub(r"<[^>]+>", "", t))
    draft["body_text"] = _re.sub(r"\n{3,}", "\n\n", t).strip()
    draft["sent"] = False
    draft["note"] = "Draft only - nothing has been sent. Say 'send it' to email this to the leaseholder."
    return draft


async def tool_rtm_demand_schedule(args: dict[str, Any]) -> dict[str, Any]:
    """View, preview, or confirm the next SCHEDULED half-year demand run. Each half-year is an already
    approved on-account instalment (half the annual budget, lease clause 12.11), payable 1 January / 1 July
    (clause 12.10), so it needs issuing at the right time, not re-approving. Mongoose previews it on the
    scheduled prepare-date and alerts the owner; issuing to the ledger is confirm-gated (a demand to 18
    leaseholders is legally significant), and sending the documents is the separate demand mailer.

    action 'status' (default) shows the scheduled run; 'preview' dry-runs the exact per-flat figures
    (nothing written); 'issue' writes the demands to the ledger (requires confirm); 'cancel' cancels it.
    """
    from . import rtm_scheduled_demands as SD
    action = (args.get("action") or "status").strip().lower()
    if action == "preview":
        return SD.preview()
    if action in ("issue", "confirm", "raise"):
        return SD.issue(confirm=bool(args.get("confirm", False)))
    if action == "cancel":
        return SD.cancel_schedule()
    return SD.status()


async def tool_rtm_on_account(args: dict[str, Any]) -> dict[str, Any]:
    """Payment on account: record an advance / overpayment as standing credit, split it to a fund, or read
    what a flat holds. There are no refunds; credit carries forward and the next demand raised consumes it.

    action 'record' books money received on account (advance for a budget not yet demanded, or a payment
    with no open demand). action 'assign' pins standing credit to a fund/schedule ('reserve' or 'service')
    before a demand consumes it. action 'status' (default) reports a flat's credit and balance.
    """
    from . import rtm_management as M
    from .rtm_management import list_flats
    flat = (args.get("flat") or "").strip()
    name = (args.get("leaseholder") or args.get("name") or "").strip()
    if not flat and name:
        qtokens = [t for t in name.lower().split() if t]
        def _hay(f):
            return (str(f.get("leaseholder_name") or "") + " " + str(f.get("payment_reference") or "")).lower()
        matches = [f for f in (list_flats(include_excluded=True) or []) if qtokens and all(t in _hay(f) for t in qtokens)]
        if len(matches) == 1:
            flat = str(matches[0].get("flat_id") or "")
        elif len(matches) > 1:
            return {"ok": False, "ambiguous": True, "error": f"{len(matches)} flats match {name!r}: say which flat",
                    "candidates": [{"flat_id": m.get("flat_id"), "leaseholder_name": m.get("leaseholder_name")} for m in matches]}
        else:
            return {"ok": False, "error": f"no leaseholder matching {name!r}"}
    if not flat:
        return {"ok": False, "error": "a flat or leaseholder is required"}
    action = (args.get("action") or "status").strip().lower()
    if action in ("record", "pay", "advance"):
        return M.record_on_account_credit(flat=flat, amount=args.get("amount"),
                                          received_date=args.get("received_date") or None,
                                          reference=str(args.get("reference", "")),
                                          note=str(args.get("note", "")))
    if action in ("assign", "split", "designate"):
        return M.assign_on_account_to_fund(flat=flat,
                                           fund_schedule=str(args.get("fund") or args.get("fund_schedule") or ""),
                                           amount=args.get("amount"))
    st = M.flat_statement(flat)
    return {"ok": True, "flat_id": st.get("flat_id"), "position": st.get("position"),
            "balance": st.get("balance"), "in_credit": st.get("in_credit"),
            "credit_display": st.get("credit_display"),
            "available_on_account": M._money(M.available_on_account_credit(flat))}


async def tool_rtm_demand_mailing(args: dict[str, Any]) -> dict[str, Any]:
    """Prepare or send the batch "payment now due" demand mailing: emails every flat that has an
    outstanding service-charge demand and an email on file, individually, each with its own demand PDF
    attached and a dash-free cover that repeats the amount, bank details and payment reference.

    action 'preview' (default) is a dry-run: it returns who would be emailed, the amounts, and who is
    excluded and why, sending nothing. action 'send' performs the individual sends (this tool is
    sensitive, so a send only runs after the owner explicitly confirms). Idempotent: a demand already
    emailed this period is skipped, so re-running never double-sends."""
    from . import rtm_demand_mailing as DM
    action = (args.get("action") or "preview").strip().lower()
    period = (args.get("period") or "").strip() or None
    if action in ("send", "confirm", "go"):
        return DM.send_batch(period, confirm=True, force=bool(args.get("force")),
                             priority=str(args.get("priority") or "normal"))
    prep = DM.prepare(period)
    return {"ok": True, "action": "preview", "period": prep["period"], "period_end": prep["period_end"],
            "would_email": prep["recipient_count"], "excluded": prep["excluded_count"],
            "recipients": [{"unit": b["unit"], "to": b["to"], "amount": b["amount"],
                            "reference": b["payment_reference"], "ready": b["ready"]} for b in prep["bundles"]],
            "excluded_detail": prep["excluded"], "problems": prep["problems"],
            "note": "Dry-run. Nothing sent. Say send the payment-due batch to send."}


async def tool_rtm_ontology(args: dict[str, Any]) -> dict[str, Any]:
    """The RTM domain ontology: canonical meaning of terms and rules, layered and overridable, plus a
    self-audit. Grounds RTM answers in one authoritative definition (e.g. 'sub-account' vs 'schedule',
    the reserve split rule) rather than the model guessing. Read-only."""
    from .rtm_ontology import ontology
    o = ontology()
    action = (args.get("action") or "describe").strip().lower()
    key = (args.get("key") or "").strip()
    if action == "explain" and key:
        return o.explain(key)
    if action == "audit":
        return o.audit()
    if action == "value" and key:
        try:
            return {"key": key, "value": o.value(key)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:160]}
    return o.describe()


async def tool_rtm_knowledge(args: dict[str, Any]) -> dict[str, Any]:
    """The Example Court RTM knowledge base: budget, flats/parties registry, legal matters, suppliers,
    calendar, digital infra, history. Read-only, with provenance. Authority order is enforced by the
    base's README: canonical functions > semantics + active budget > registry > legal/operations >
    history; BRIEFING.md is orientation only, never canonical; the model never does arithmetic."""
    from .rtm_knowledge import is_installed, index, section, search, semantics, active_budget
    if not is_installed():
        return {"ok": False, "error": "RTM knowledge base not installed"}
    action = (args.get("action") or "index").strip().lower()
    q = (args.get("query") or "").strip()
    name = (args.get("section") or "").strip()
    if action == "search" and q:
        return {"query": q, "results": search(q)}
    if action == "section" and name:
        s = section(name)
        return {"section": name, "data": s} if s is not None else {"ok": False, "error": f"no section {name!r}"}
    if action == "semantics":
        return {"semantics": semantics()}
    if action == "budget":
        return {"budget": active_budget()}
    return {"index": index(),
            "authority": "canonical functions > semantics + active budget > registry > legal/operations > history; BRIEFING.md is non-canonical."}


async def tool_recent_activity(args: dict[str, Any]) -> dict[str, Any]:
    """What has Mongoose actually done lately, from receipts/audit (including failures and skips).
    Read-only, this is the receipt_truth claim surfaced to chat."""
    from .receipts import receipts_ledger
    try:
        limit = int(args.get("limit") or 20)
    except Exception:
        limit = 20
    return receipts_ledger(limit=max(1, min(limit, 100)))
