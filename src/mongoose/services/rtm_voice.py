from __future__ import annotations

from typing import Any

# Voice-shaped presenters for RTM, so the chat brain can answer questions about the building's
# service-charge state and about the RTM product build in short, spoken sentences , reading the real
# management data, never the model guessing. Read-only: these summarise, they never send or pay.


def _speak(text: str) -> dict[str, Any]:
    return {"speech": text}


def operational_for_voice(focus: str = "dashboard") -> dict[str, Any]:
    """A short spoken answer about the RTM's operational state. `focus` is one of
    arrears | review | payables | dashboard."""
    from . import rtm_management as M
    try:
        if focus == "arrears":
            ar = M.arrears()
            n = ar.get("count", 0)
            if not n:
                return _speak("No flats are in arrears right now.")
            items = ar.get("items", [])[:3]
            lead = f"{n} flat{'s' if n != 1 else ''} in arrears."
            bits = []
            for it in items:
                bits.append(f"{it['flat_id']} owes {it['amount_outstanding']}, {it['days_overdue']} days over")
            tail = "" if n <= 3 else f" Plus {n - 3} more."
            return _speak(lead + " " + "; ".join(bits) + "." + tail)

        if focus == "review":
            rq = M.allocation_review_queue()
            n = rq.get("count", 0)
            if not n:
                return _speak("No bank credits are waiting to be allocated.")
            return _speak(f"{n} bank credit{'s' if n != 1 else ''} need allocation review. "
                          "Open the RTM tab to match them to demands.")

        if focus == "payables":
            pays = M.list_payables()
            due = [p for p in pays if p.get("status") in {"Scheduled", "Approved"}]
            if not due:
                return _speak("No supplier payables are scheduled or approved right now.")
            top = due[0]
            return _speak(f"{len(due)} payable{'s' if len(due) != 1 else ''} outstanding. "
                          f"Next: {top.get('supplier')} {top.get('amount')} due {top.get('due_date')}.")

        # dashboard / default: the money position + the single top action
        pos = M.fund_position()
        dash = M.management_dashboard()
        actions = [a for a in dash.get("next_actions", []) if a.get("type") != "ok"]
        money = (f"Demands raised {pos.get('demands_raised', '£0.00')}, "
                 f"arrears outstanding {pos.get('arrears_outstanding', '£0.00')}.")
        if not actions:
            return _speak(money + " Nothing needs you right now.")
        top = actions[0]
        more = "" if len(actions) == 1 else f" And {len(actions) - 1} other thing{'s' if len(actions) - 1 != 1 else ''}."
        return _speak(money + " Top action: " + top.get("title", "") + "." + more)
    except Exception:
        return _speak("I couldn't read the RTM figures just now. Try the RTM tab in the cockpit.")


def build_for_voice() -> dict[str, Any]:
    """A short spoken answer about the RTM product BUILD (the WordPress/admin surface), reading the
    builder plan state. Honest about the approval gate."""
    from . import builder
    from .build_plans import EXAMPLE_COURT_MANAGEMENT_STEPS
    try:
        plans = builder.list_plans()
        from .. import profile as _profile
        plan_name = f"{_profile.rtm_slug()}-management"
        mgmt = next((p for p in plans if p.get("name") == plan_name), None)
        total = len(EXAMPLE_COURT_MANAGEMENT_STEPS)
        if not mgmt:
            return _speak(f"The RTM management build plan has {total} steps but isn't created yet. "
                          "It covers the WordPress and admin surface. I can create it for you to approve.")
        steps = mgmt.get("steps", [])
        done = sum(1 for s in steps if s.get("status") == "done")
        if not steps:
            # A stored plan with no steps is a degenerate/stale artefact, not something approvable.
            # Saying "ready with 0 steps, waiting for approval" is nonsense; guide a clean recreate.
            return _speak(f"The RTM management build plan is empty (no steps recorded), so there's "
                          f"nothing to approve yet. It should have {total} steps for the WordPress and "
                          "admin surface: the stored plan looks stale. Ask me to recreate the RTM build "
                          "plan and I'll rebuild it cleanly for you to approve.")
        if not mgmt.get("approved"):
            return _speak(f"The RTM build plan is ready with {len(steps)} steps for the WordPress and "
                          "admin surface, but it's waiting for your approval. Nothing builds until you approve it.")
        if done >= len(steps) and steps:
            return _speak("The RTM build plan is fully built. Every step is done.")
        return _speak(f"The RTM build is approved and running: {done} of {len(steps)} steps done. "
                      "It advances a bit each week.")
    except Exception:
        return _speak("I couldn't read the RTM build state just now.")


def knowledge_for_voice(text: str = "") -> dict[str, Any]:
    """A short spoken answer about RTM capability or the Example Court real-data facts/caveats."""
    low = (text or "").lower()
    try:
        if "rounding" in low:
            from . import rtm_example_court as PC
            f = PC.operating_facts()
            return _speak(f"The demands total {f['demand_pack_total']} against a half-year budget of "
                          f"{f['half_year_budget']}, a {f['rounding_delta']} rounding delta. That's a "
                          "reconciliation item at year-end, not arrears.")
        if "deed" in low or "verif" in low:
            from . import rtm_example_court as PC
            deed = next((c for c in PC.caveats() if c["code"] == "DEED_VERIFICATION"), None)
            return _speak(deed["detail"][:240] if deed else "No deed-verification caveat on file.")
        if "budget" in low:
            from . import rtm_example_court as PC
            f = PC.operating_facts()
            return _speak(f"The annual budget is {f['annual_budget']}, {f['half_year_budget']} per "
                          f"half-year across {f['flats']} flats.")
        # default: capability state
        from . import rtm_capability as C
        cap = C.capability_state()
        in_use = [r["requirement"] for r in cap.get("requirements", []) if r["state"] in ("in_use", "verified")]
        return _speak(f"RTM capability: {cap.get('summary', '')}. In use: {', '.join(in_use) or 'none yet'}.")
    except Exception:
        return _speak("I couldn't read the RTM knowledge just now.")


def sync_for_voice() -> dict[str, Any]:
    """Run a sync checkpoint and report what it surfaced (the spoken version of the check-in)."""
    try:
        from .sync_checkpoint import assemble_checkpoint
        cp = assemble_checkpoint()
        bits = [cp["summary"]]
        if cp["asking"]:
            bits.append("I'd ask: " + cp["asking"][0])
        return _speak(" ".join(bits))
    except Exception:
        return _speak("I couldn't assemble a checkpoint just now.")
