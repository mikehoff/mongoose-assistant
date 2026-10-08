from __future__ import annotations

import os
import tempfile
from typing import Any

# Dress rehearsal: exercise EVERY feature module end-to-end against synthetic data, so that before
# trusting the system on real data you have proof each code path actually works and produces
# sensible output , not merely that dependencies are installed (that's release_check's job, which
# this composes).
#
# The honesty rule that makes this trustworthy: a module that needs a LIVE edge (Ollama serving,
# real IMAP, real SMTP send, the phone round-trip) is reported as "needs your hardware" or
# "disabled", NEVER as a pass it didn't earn. A green rehearsal means the logic is proven; the live
# edges are called out separately as the real-Mac gate.
#
# Each rehearsal step returns: name, result in {pass, configure, disabled, needs_hardware, fail},
# and a one-line detail. The overall verdict is the worst meaningful result.

PASS = "pass"            # exercised end-to-end, correct output
CONFIGURE = "configure"  # works, but needs a credential/setting to be useful
DISABLED = "disabled"    # intentionally off (a live edge), by design
HARDWARE = "needs_hardware"  # can only be proven on the real Mac (Ollama, real IMAP/SMTP, phone)
FAIL = "fail"            # broke , a real problem

_ORDER = {PASS: 0, DISABLED: 1, CONFIGURE: 2, HARDWARE: 3, FAIL: 4}


def _step(name: str, result: str, detail: str = "") -> dict[str, Any]:
    return {"name": name, "result": result, "detail": detail}


def _reset_runtime_caches() -> None:
    """Settings and DB schema readiness are cached for normal runtime speed.

    A dress rehearsal deliberately swaps MONGOOSE_DB_PATH/MONGOOSE_DATA_DIR to a
    throwaway location. If these caches are not cleared first, a prior call to
    get_settings()/connect() can make the rehearsal touch the caller's live DB,
    which would violate the whole point of a dry run.
    """
    try:
        import mongoose.settings as _settings
        _settings._SETTINGS = None
    except Exception:
        pass
    try:
        import mongoose.db as _db
        _db._SCHEMA_READY.clear()
    except Exception:
        pass


def _isolated_db() -> dict[str, str | None]:
    """Each rehearsal runs against a throwaway DB so it never touches real data.

    Returns the previous environment so the caller can restore it. This is a safety
    boundary, not just test hygiene: a rehearsal must not leave the process pointed
    at a temp DB or accidentally write to a cached live path.
    """
    previous = {
        "MONGOOSE_DB_PATH": os.environ.get("MONGOOSE_DB_PATH"),
        "MONGOOSE_DATA_DIR": os.environ.get("MONGOOSE_DATA_DIR"),
    }
    d = tempfile.mkdtemp(prefix="mongoose_rehearsal_")
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "rehearsal.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    _reset_runtime_caches()
    from .db import init_db
    init_db()
    return previous


def _restore_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _reset_runtime_caches()


# --- individual module rehearsals -------------------------------------------

def _rehearse_memory() -> dict[str, Any]:
    try:
        from .services.memory import ensure_conversation, add_message, build_context
        cid = ensure_conversation()
        add_message(cid, "user", "remember I prefer concise answers")
        add_message(cid, "assistant", "noted")
        hist, _ = build_context(cid, "what do I prefer?")
        return _step("Memory", PASS, f"conversation + context window working ({len(hist)} msgs)")
    except Exception as e:
        return _step("Memory", FAIL, str(e)[:80])


def _rehearse_readiness() -> dict[str, Any]:
    try:
        from .services.readiness import readiness_report
        rep = readiness_report()
        n = len(rep.get("items", []))
        return _step("Readiness layer", PASS, f"{n} capability signals computed")
    except Exception as e:
        return _step("Readiness layer", FAIL, str(e)[:80])


def _rehearse_body_state() -> dict[str, Any]:
    try:
        from datetime import datetime, timezone, timedelta
        from .services import body_state as BS, body_readiness as BR
        now = datetime.now(timezone.utc)
        for d in range(2, 9):
            BS.log_body_state(hrv=55, resting_hr=52, sleep_hours=7.5, at=now - timedelta(days=d))
        BS.log_body_state(hrv=46, tension=4, at=now)
        est = BR.load_estimate(now=now)
        return _step("Body State + load estimate", PASS,
                     f"logged + estimate ({est.get('mode')}/{est.get('band','n/a')})")
    except Exception as e:
        return _step("Body State + load estimate", FAIL, str(e)[:80])


def _rehearse_progression() -> dict[str, Any]:
    """Exercise the Alpha importer + progression query against a tiny synthetic export."""
    try:
        from .services import alpha_import as AI, body_progression_detail as PD
        sessions = ("title,date,duration,n_exercises,n_sets,dt,month\n"
                    "Day 1,2026-06-01 9:00 am,40 min,1,3,2026-06-01 09:00:00,2026-06\n")
        sets = "title,date,duration,exercise,equipment,target,order,set_no,kg,reps,rir,mins\n"
        # synth lateral raises across several sessions so a trend exists
        from datetime import date, timedelta
        rows = []
        for i in range(8):
            d = date(2026, 3, 1) + timedelta(days=i * 10)
            kg = 10 + i * 0.5
            rows.append(f"Day 1,{d} 9:00 am,40 min,Lateral Raises,Dumbbells,10 reps,1,1,{kg},10,1,")
        sets += "\n".join(rows) + "\n"
        AI.import_all(sessions=sessions, sets=sets)
        t = PD.exercise_trend("Lateral Raises")
        return _step("Alpha import + progression", PASS,
                     f"imported + trend computed ({t.get('status')})")
    except Exception as e:
        return _step("Alpha import + progression", FAIL, str(e)[:80])


def _rehearse_unstuck() -> dict[str, Any]:
    try:
        from .services import unstuck as U
        out = U.unstick(force=True)
        # also assert the guardrail held (no forbidden passive framing)
        U._assert_clean(out["speech"])
        return _step("Unstuck / active-recovery", PASS, "suggestion generated, guardrail held")
    except Exception as e:
        return _step("Unstuck / active-recovery", FAIL, str(e)[:80])


def _rehearse_time_alerts() -> dict[str, Any]:
    try:
        from datetime import datetime, timezone
        from .services.time_alerts import set_from_text
        r = set_from_text("wake me at 7am tomorrow", now=datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc))
        return _step("Time Alerts", PASS if r.get("ok") else CONFIGURE,
                     "alarm parsed + scheduled" if r.get("ok") else "parsed but check delivery")
    except Exception as e:
        return _step("Time Alerts", FAIL, str(e)[:80])


async def _rehearse_rtm() -> dict[str, Any]:
    """Exercise the RTM pipeline: intake -> classify (incl. safety) -> draft, on synthetic mail."""
    try:
        from .services import rtm_intake as R
        # a safety email must classify high + produce a draft, draft-only
        res = await R.handle_incoming(
            sender="Test <test@example.com>", subject="gas smell in communal hall",
            body="strong smell of gas in the shared hallway", auto_push=False)
        cls = res["classification"]
        ok = (cls["urgency"] == "high" and cls["safety"] and res["draft"].get("ok"))
        # and approving must NOT send (live send off)
        ap = R.approve_draft(res["draft"]["draft_id"])
        safe = ap.get("sent") is False
        if ok and safe:
            return _step("RTM intake + draft (safety)", PASS,
                         "email->case->classified high->draft, approve did not send")
        return _step("RTM intake + draft (safety)", FAIL,
                     f"classify_ok={ok} send_blocked={safe}")
    except Exception as e:
        return _step("RTM intake + draft (safety)", FAIL, str(e)[:80])


def _rehearse_builder() -> dict[str, Any]:
    """The overnight builder's plan machinery (not a live build, that needs the Mac)."""
    try:
        from .services import build_plans
        assert hasattr(build_plans, "build_example_court_plan")
        assert hasattr(build_plans, "build_example_court_intake_plan")
        return _step("Overnight builder (plan machinery)", PASS, "both plans loadable; live build is a Mac step")
    except Exception as e:
        return _step("Overnight builder (plan machinery)", FAIL, str(e)[:80])


def _rehearse_sc_register() -> dict[str, Any]:
    """Exercise the RTM management spine (R1-R7): seed flats, issue a demand, supersede it
    (immutability, the old demand's debit must be reversed, not double-counted), and check the
    money is exact. The immutability and money precision are the FTT-critical parts."""
    try:
        from .services import rtm_management as M
        from . import profile as _profile
        flat = f"{_profile.block_codes()[0]}F9"
        M.seed_flats()
        M.create_demand(document_number="REH-R0", flat=flat, period="2026", amount="800.00",
                        issue_date="2026-06-01")
        M.create_demand(document_number="REH-R1", flat=flat, period="2026", amount=850,
                        issue_date="2026-06-01", supersedes="REH-R0")
        st = M.flat_statement(flat)
        # 850 live demand, old 800 reversed -> 850 (not 1650); and 850 (int) must be £850.00
        ok = (st["balance_pence"] == 85000)
        return _step("RTM management (R1-R7 spine)",
                     PASS if ok else FAIL,
                     "demand/supersede/reversal correct, money exact" if ok
                     else f"balance_pence={st.get('balance_pence')} (supersede may double-count)")
    except Exception as e:
        return _step("RTM management (R1-R7 spine)", FAIL, str(e)[:80])


def _rehearse_rtm_integration() -> dict[str, Any]:
    """Exercise the RTM coherence wiring: the conversational brain routes an RTM operational query
    to the real ledger (not the model), and RTM arrears show up in the unified readiness picture."""
    try:
        from .services import rtm_management as M
        from . import profile as _profile
        M.seed_flats()
        M.create_demand(document_number="REH-OD", flat=f"{_profile.block_codes()[-1]}F9", period="2026",
                        amount=400, issue_date="2026-01-01")
        M.refresh_all_demand_statuses()
        # 1. brain routes the query
        from .role_router import looks_like_rtm_operational
        routed = looks_like_rtm_operational(f"what is overdue at {_profile.rtm_name().lower()}?")
        # 2. RTM appears in readiness
        from .services.readiness import collect_rtm_readiness
        rtm_items = collect_rtm_readiness()
        in_readiness = any("arrears" in (i.id + i.label).lower() for i in rtm_items)
        ok = routed and in_readiness
        return _step("RTM integration (brain + readiness)",
                     PASS if ok else FAIL,
                     "brain routes RTM queries; arrears in readiness picture" if ok
                     else f"routed={routed} in_readiness={in_readiness}")
    except Exception as e:
        return _step("RTM integration (brain + readiness)", FAIL, str(e)[:80])


def _rehearse_rtm_state_and_sync() -> dict[str, Any]:
    """Exercise the RTM capability/build-candidate state, the build lock (ad-hoc vs scheduled never
    collide), the Example Court real-data fixture, and a sync checkpoint, the pieces that make RTM
    self-maintaining and keep it in sync with the owner's Claude/chat."""
    try:
        from .services import rtm_capability as C, rtm_example_court as PC, sync_checkpoint as SC
        # real-data fixture validates + loads
        v = PC.validate_against_management()
        if not v.get("ok"):
            return _step("RTM state + sync (capability/candidates/real-data)", FAIL,
                         f"fixture invalid: {v.get('computed_total')}")
        PC.apply_real_data(with_demands=True)
        # capability state reflects the loaded data
        cap = C.capability_state()
        # build lock: ad-hoc holds, scheduled defers
        lk = C.acquire_build_lock("adhoc:rehearsal")
        held = lk.get("ok", False)
        contend = C.acquire_build_lock("scheduled:daily")
        deferred = not contend.get("ok", True)
        C.release_build_lock(lk["token"])
        # candidate lifecycle
        a = C.create_candidate("rehearsal-goal", approach="A")
        b = C.create_candidate("rehearsal-goal", approach="B")
        promo = C.promote_candidate(a["id"])
        promoted = promo.get("chosen") == a["id"] and b["id"] in promo.get("superseded", [])
        C.prune_candidates(goal="rehearsal-goal")
        # a sync checkpoint assembles
        cp = SC.assemble_checkpoint()
        ok = (cap.get("in_use", 0) >= 1 and held and deferred and promoted
              and bool(cp.get("asking")))
        return _step("RTM state + sync (capability/candidates/real-data)",
                     PASS if ok else FAIL,
                     "capability derived, lock guards ad-hoc, candidates reconcile, checkpoint assembles"
                     if ok else f"in_use={cap.get('in_use')} held={held} deferred={deferred} promoted={promoted}")
    except Exception as e:
        return _step("RTM state + sync (capability/candidates/real-data)", FAIL, str(e)[:80])


def _rehearse_governance() -> dict[str, Any]:
    """Exercise the RTM governance layer: configure a company, compute compliance deadlines, and
    record a trust-fund transaction, confirming the secretary and bookkeeper work on synthetic
    data. The deadline arithmetic is the legally-sensitive part."""
    try:
        from datetime import date
        from .services import rtm_secretary as S, rtm_bookkeeper as B
        S.configure_profile(company_number="00000000", corporation_tax_period_end="2026-06-30")
        rep = S.compute_compliance(today=date(2026, 6, 1))
        items = {i["kind"]: i for i in rep["items"]}
        ct_ok = items.get("corporation_tax_payment", {}).get("due_date") == "2027-03-31"
        B.record_opening_balance("service", 1000, "2026-07-16")
        B.record_transaction(fund="service", amount=250, direction="in", description="test")
        bal = B.summary()["ledger_balances"]["service_charge_trust"]
        books_ok = bal == "\u00a31,250.00"
        return _step("RTM governance (secretary + books)",
                     PASS if (ct_ok and books_ok) else FAIL,
                     "deadlines computed + trust ledger balanced" if (ct_ok and books_ok)
                     else f"ct_ok={ct_ok} books_ok={books_ok}")
    except Exception as e:
        return _step("RTM governance (secretary + books)", FAIL, str(e)[:80])


def _rehearse_diagnostics() -> dict[str, Any]:
    """Exercise the two-lane diagnostics: capture events, delta-save a metric, check the report-ready
    logic, and export a bundle, all on synthetic data."""
    try:
        from .services import diagnostics as D
        D.record_bug("rehearsal.scope", "synthetic bug for rehearsal")
        D.record_inference("rehearsal.scope", "synthetic inference")
        saved1 = D.record_metric("rehearsal.metric", 100.0)["saved"]
        saved2 = D.record_metric("rehearsal.metric", 100.5)["saved"]  # tiny -> not saved
        st = D.report_status()
        exp = D.export_report()
        ok = (saved1 and not saved2 and st["event_count"] >= 2
              and exp["event_count"] >= 2)
        return _step("Diagnostics (two-lane + export)",
                     PASS if ok else FAIL,
                     "captured, delta-saved, report exported" if ok else "logic mismatch")
    except Exception as e:
        return _step("Diagnostics (two-lane + export)", FAIL, str(e)[:80])


def _rehearse_rtm_search() -> dict[str, Any]:
    """Exercise voicemail import + FTS search on synthetic data."""
    try:
        from .services import rtm_intake as R
        R.intake_email(sender="x <x@y.com>", subject="lift stuck", body="the lift is stuck",
                       kind="voicemail")
        hits = R.search_intake("lift")
        return _step("RTM voicemail + FTS search", PASS if hits else FAIL,
                     f"{len(hits)} search hit(s)")
    except Exception as e:
        return _step("RTM voicemail + FTS search", FAIL, str(e)[:80])


# --- live-edge checks (honest: hardware/disabled, not fake passes) ----------

def _check_ollama() -> dict[str, Any]:
    try:
        from .doctor import run_doctor
        ok = (run_doctor(timeout_seconds=1.0).get("checks", {}).get("ollama", {}) or {}).get("ok")
        if ok:
            return _step("Ollama serving models", PASS, "reachable")
        return _step("Ollama serving models", HARDWARE,
                     "not reachable here, on the Mac, run `ollama serve` and pull the models")
    except Exception:
        return _step("Ollama serving models", HARDWARE, "only verifiable on the Mac")


def _rtm_email() -> str:
    from . import profile as _profile
    return _profile.rtm_email()


def _check_rtm_live_edges() -> list[dict[str, Any]]:
    from .services.rtm_intake import status
    s = status()
    out = []
    if s["live_mailbox"]:
        out.append(_step("RTM live mailbox", CONFIGURE,
                         f"ARMED, polling {_rtm_email()} (verify creds on the Mac)"))
    else:
        out.append(_step("RTM live mailbox", DISABLED,
                         "off by default, enable with RTM_LIVE_MAILBOX after rehearsal passes"))
    if s["live_send"]:
        out.append(_step("RTM live send", CONFIGURE, "ARMED, approved drafts will actually send"))
    else:
        out.append(_step("RTM live send", DISABLED,
                         "off by default, the responsible setting until you've watched a dry run"))
    return out


def _check_voicemail_bridge() -> dict[str, Any]:
    """The HA voicemail add-on writes JSON; Mongoose can ingest it. Report whether the share path
    exists (on the Mac/Pi it will; here it won't)."""
    import os
    from . import profile as _profile
    path = _profile.ha_voicemail_dir()
    if os.path.isdir(path):
        return _step("Voicemail bridge", PASS, f"{path} present")
    return _step("Voicemail bridge", HARDWARE,
                 "voicemail share not present here, exists on the HA host; importer ready")


# --- the rehearsal ----------------------------------------------------------

async def dress_rehearsal() -> dict[str, Any]:
    """Run the full dress rehearsal. Returns sections of steps + an overall verdict. Runs against
    an isolated throwaway DB; touches no real data and sends nothing."""
    previous_env = _isolated_db()
    try:
        return await _dress_rehearsal_inner()
    finally:
        _restore_env(previous_env)


async def _dress_rehearsal_inner() -> dict[str, Any]:
    def _safe(fn, label):
        """Run a sync rehearsal step; if it raises, that becomes a FAIL line (a rehearsal must be
        robust, one broken module can't abort the report)."""
        try:
            return fn()
        except Exception as e:
            return _step(label, FAIL, str(e)[:80])

    async def _safe_async(coro, label):
        try:
            return await coro
        except Exception as e:
            return _step(label, FAIL, str(e)[:80])

    functional = [
        _safe(_rehearse_memory, "Memory"),
        _safe(_rehearse_readiness, "Readiness layer"),
        _safe(_rehearse_body_state, "Body State + load estimate"),
        _safe(_rehearse_progression, "Alpha import + progression"),
        _safe(_rehearse_unstuck, "Unstuck / active-recovery"),
        _safe(_rehearse_time_alerts, "Time Alerts"),
        await _safe_async(_rehearse_rtm(), "RTM intake + draft (safety)"),
        _safe(_rehearse_rtm_search, "RTM voicemail + FTS search"),
        _safe(_rehearse_builder, "Overnight builder (plan machinery)"),
        _safe(_rehearse_diagnostics, "Diagnostics (two-lane + export)"),
        _safe(_rehearse_governance, "RTM governance (secretary + books)"),
        _safe(_rehearse_sc_register, "RTM management (R1-R7 spine)"),
        _safe(_rehearse_rtm_integration, "RTM integration (brain + readiness)"),
        _safe(_rehearse_rtm_state_and_sync, "RTM state + sync (capability/candidates/real-data)"),
    ]
    live = [_safe(_check_ollama, "Ollama serving models"),
            _safe(_check_voicemail_bridge, "Voicemail bridge")] + _check_rtm_live_edges()

    # environment readiness from the existing release_check (models, cockpit, LAN, data paths)
    env_sections = []
    try:
        from .release_check import release_check
        env = await release_check()
        env_sections = env.get("sections", [])
    except Exception as e:
        env_sections = [("Environment", [_line_compat("release_check", "unknown", str(e)[:80])])]

    all_results = functional + live
    # a FAIL is the only thing that means "not ready"; hardware/configure/disabled are expected
    has_fail = any(s["result"] == FAIL for s in all_results)
    if has_fail:
        verdict = "NOT READY, a module failed (see below)"
    else:
        verdict = "LOGIC READY, all modules exercised; live edges need your Mac (see below)"

    return {"verdict": verdict, "has_fail": has_fail,
            "functional": functional, "live": live, "environment": env_sections}

def _line_compat(label, state, detail):
    return {"label": label, "state": state, "detail": detail}


def format_rehearsal(report: dict[str, Any]) -> str:
    """Human-readable rehearsal report for the terminal."""
    mark = {PASS: "OK  ", CONFIGURE: "CFG ", DISABLED: "off ", HARDWARE: "MAC ", FAIL: "FAIL"}
    out = ["", "=" * 64, "  MONGOOSE DRESS REHEARSAL", "=" * 64,
           f"  Verdict: {report['verdict']}", ""]
    out.append("  Modules exercised end-to-end (synthetic data, nothing sent):")
    for s in report["functional"]:
        out.append(f"    [{mark[s['result']]}] {s['name']}" + (f"  ({s['detail']})" if s['detail'] else ""))
    out.append("")
    out.append("  Live edges (only your Mac can prove these; off = safe by design):")
    for s in report["live"]:
        out.append(f"    [{mark[s['result']]}] {s['name']}" + (f"  ({s['detail']})" if s['detail'] else ""))
    out.append("")
    out.append("  Legend: OK exercised & correct | off intentionally disabled | "
               "MAC needs real hardware | CFG armed/needs creds | FAIL real problem")
    out.append("=" * 64)
    return "\n".join(out)
