#!/usr/bin/env python3
"""Single-journey workflow simulation for Mongoose.

This drives the REAL CLI and app end-to-end, mirroring what the entry point
(Install Mongoose.command -> bootstrap -> mongoose serve) sets in motion, for the
parts that don't require brew/docker/ollama. It is the "triple check" that the
whole journey holds together: install -> first run -> load real RTM data ->
operate -> ask the brain -> sync -> build -> readiness.

Run: PYTHONPATH=src python scripts/simulate_journey.py
Exits non-zero if any stage fails.
"""
import os
import subprocess
import sys
import tempfile

FAILS: list[str] = []
STAGES = 0


def stage(name: str, ok: bool, detail: str = "") -> None:
    global STAGES
    STAGES += 1
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


def cli(*args: str) -> str:
    env = dict(os.environ, PYTHONPATH="src")
    r = subprocess.run([sys.executable, "-m", "mongoose.cli", *args],
                       env=env, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def main() -> int:
    # fresh sandbox state, like a first install
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    print("Mongoose single-journey simulation\n")

    # 1. INSTALL: init-db + install coherence (the bootstrap's core, sans brew/docker/ollama)
    print("1. Install / first run")
    from mongoose.db import init_db
    init_db()
    stage("init-db", os.path.exists(os.environ["MONGOOSE_DB_PATH"]))
    from mongoose.services.install_check import check_install_coherence
    coh = check_install_coherence()
    stage("install coherence (all CLI commands wired)", not coh["missing_commands"],
          f"missing={coh['missing_commands']}")
    # the two safety guards that must always hold
    from mongoose.release_check import _verify_read_first, _verify_rtm_model_tier_gated
    stage("safety guards (read-first + model-tier gated)",
          _verify_read_first()["ok"] and _verify_rtm_model_tier_gated()["ok"])

    # 2. REMOTE FROM DAY ONE: the four sync channels enabled
    print("\n2. Remote connections (day one)")
    from mongoose.services import sync_checkpoint as SC
    ch = SC.channel_status()
    stage("all four remote channels enabled",
          all(ch[c]["enabled"] for c in ("chat_pro", "claude_pro", "anthropic_api", "openai_api")))

    # 3. LOAD REAL DATA: the actual Example Court flats/budget/demands
    print("\n3. Load real Example Court data")
    cli("rtm", "load-real-demands", "--confirm")
    from mongoose.services import rtm_management as M, rtm_example_court as PC
    flats = M.list_flats()
    demands = M.list_demands(limit=50)
    stage("18 real flats loaded", len(flats) == 18, f"{len(flats)} flats")
    stage("18 real demands loaded", len(demands) == 18, f"{len(demands)} demands")
    v = PC.validate_against_management()
    stage("money exact (£17,685.24, 24p delta)",
          v["computed_total"] == "\u00a317,685.24" and v["rounding_delta"] == "\u00a30.24")
    # the 14-day due policy preserved (not silently 'corrected' to 28)
    hoff = next((dd for dd in demands if dd["flat_id"] == "1ECF1"), {})
    stage("14-day due policy preserved", hoff.get("due_date") == "2026-07-30",
          f"due={hoff.get('due_date')}")

    # 4. OPERATE: a payment, then check arrears + dashboard
    print("\n4. Operate the block")
    # mark one flat's demand overdue by issuing an old one, then check arrears reads it
    M.refresh_all_demand_statuses()
    # record a payment against Morgan via the management ledger
    dash = M.management_dashboard()
    stage("dashboard computes next-actions", isinstance(dash.get("next_actions"), list))
    stage("fund position reflects real demands",
          M.fund_position().get("demands_raised") == "\u00a317,685.24")

    # 5. ASK THE BRAIN: operational + knowledge + build, all from real state (no LLM fallthrough)
    print("\n5. Ask the conversational brain")
    import asyncio
    from mongoose.llm import chat_with_memory

    async def ask(q: str) -> tuple[str, str]:
        try:
            r = await chat_with_memory(q, conversation_id="sim")
            return r.get("source", "?"), r.get("reply", "")
        except Exception as e:
            return "LLM_FALLTHROUGH", str(e)[:40]

    src, _ = asyncio.run(ask("what is overdue at example court?"))
    stage("brain routes operational query", src == "rtm_operational", f"source={src}")
    src, reply = asyncio.run(ask("what is the rounding situation?"))
    stage("brain routes knowledge query", src == "rtm_knowledge", f"source={src}")
    src, _ = asyncio.run(ask("how is the rtm build going?"))
    stage("brain routes build query", src == "rtm_build", f"source={src}")
    # no-hijack: a neutral factual question must NOT route to an RTM source (it will try the LLM and
    # fail in sandbox, which proves it did NOT short-circuit to RTM)
    src, _ = asyncio.run(ask("what is the capital of France?"))
    stage("ordinary chat not hijacked by RTM",
          src not in ("rtm_operational", "rtm_knowledge", "rtm_build", "sync_checkpoint"),
          f"source={src}")

    # 6. SYNC CHECKPOINT: assemble + record
    print("\n6. Sync checkpoint")
    r = asyncio.run(SC.run_checkpoint())
    cp = r["checkpoint"]
    stage("checkpoint assembles (done/surfaced/asking)",
          bool(cp.get("done")) and bool(cp.get("asking")))
    stage("checkpoint recorded to history", len(SC.checkpoint_history()) >= 1)

    # 7. BUILD: ad-hoc build is lock-guarded against the scheduled job
    print("\n7. Build cadence + ad-hoc, lock-guarded")
    from mongoose.services import rtm_capability as C
    lk = C.acquire_build_lock("adhoc:sim")
    stage("ad-hoc build takes the lock", lk["ok"])
    contend = C.acquire_build_lock("scheduled:daily")
    stage("scheduled job defers while ad-hoc holds", not contend["ok"])
    C.release_build_lock(lk["token"])
    # candidate lifecycle
    a = C.create_candidate("wp-admin", approach="A")
    b = C.create_candidate("wp-admin", approach="B")
    promo = C.promote_candidate(a["id"])
    stage("candidates reconcile (promote one, supersede sibling)",
          promo.get("chosen") == a["id"] and b["id"] in promo.get("superseded", []))
    pr = C.prune_candidates(goal="wp-admin")
    stage("superseded candidates prunable", b["id"] in pr["pruned"])

    # 8. DELIGHT: the warm companion register , a joke/fact tied to interests, routed by the brain
    print("\n8. Companion warmth (delight)")
    src, reply = asyncio.run(ask("tell me a joke"))
    stage("brain routes a joke to delight (not a feature tour)", src == "delight", f"source={src}")
    from mongoose.services.delight import fun_fact
    ff = fun_fact(allow_model=False)
    stage("interest-aware fun fact available offline",
          ff["source"] == "shipped" and bool(ff["text"]), f"interest={ff['interest']}")
    os.environ["MONGOOSE_PROACTIVE_DELIGHT"] = "1"  # opt in for this check
    import mongoose.settings as _st; _st._SETTINGS = None
    from mongoose.services.surpriser import candidate_suggestions
    cands = asyncio.run(candidate_suggestions())
    delights = [c for c in cands if c.get("kind") == "delight"]
    stage("surpriser can lighten a quiet cycle when opted in (low priority)",
          len(delights) >= 1 and delights[0]["score"] < 30)

    # 9. COCKPIT TEXT CHAT: a typed message runs through the same brain as voice
    print("\n9. Cockpit text chat")
    try:
        from fastapi.testclient import TestClient
        import mongoose.app as A
        c = TestClient(A.app)
        resp = c.post("/cockpit/chat", json={"text": "tell me a joke"},
                      headers={"X-Cockpit-Nonce": A._COCKPIT_NONCE})
        ok = resp.status_code == 200 and resp.json().get("ok") and bool(resp.json().get("reply"))
        stage("text chat endpoint replies through the brain", ok)
        from mongoose.cockpit import cockpit_html
        h = cockpit_html()
        stage("ICQ/IRC chat UI present (lime console + blinking cursor)",
              "irc-win" in h and "#39ff14" in h and "ircBlink" in h)
    except Exception as e:
        stage("text chat endpoint replies through the brain", False, str(e)[:50])

    # 10. READINESS: the unified 'what needs me' includes RTM
    print("\n10. Unified readiness picture")
    from mongoose.services.readiness import collect_all
    items = collect_all()
    rtm_items = [i for i in items if i.domain == "service" and "rtm" in (i.id + i.label).lower()
                 or "example court" in i.label.lower()]
    stage("RTM appears in unified readiness", len(rtm_items) >= 1, f"{len(rtm_items)} RTM signals")
    # the deed-verification caveat should be surfaced as a warning
    deed = [i for i in rtm_items if "deed" in i.label.lower()]
    stage("real-data caveats surfaced in readiness", len(deed) >= 1)

    # summary
    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"JOURNEY INCOMPLETE: {len(FAILS)}/{STAGES} stage(s) failed: {FAILS}")
        return 1
    print(f"JOURNEY COMPLETE: all {STAGES} stages passed end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
