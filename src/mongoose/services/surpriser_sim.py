from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Any

# The surpriser simulator: a TRUST GATE for the "not noisy" promise.
#
# The surpriser's whole value rests on a claim that's invisible in unit tests: that over
# many days, on realistic data, it stays quiet unless something is genuinely worth an
# interruption. A single-shot test can't see "fires the same template every day". This
# replays N simulated days and reports a breakdown of what the surpriser did and , just as
# importantly , WHY it stayed silent (duplicate / shape / low-value / quiet-hours /
# min-gap). That makes the anti-noise behaviour measurable.
#
# Safety: the simulation ALWAYS runs against a temporary copy DB and simulated push
# delivery. It never mutates the live database and never sends a real notification.

# The seed deliberately includes REPEATED-BUT-UNIMPORTANT patterns: the same shape of
# correlation recurring with different people, which is exactly the case that should NOT
# produce a daily interruption. A good surpriser surfaces it once, then goes quiet.
_SEED_PEOPLE = ["Chris", "Jordan", "Mum", "Acme", "Pat", "Riley"]


def _seed_people() -> list[str]:
    """The seed's cast: the generic names above, or the site profile's own (lexicon surpriser_sim.seed_people)."""
    from .. import profile as _profile
    return list(_profile.lexicon("surpriser_sim.seed_people", _SEED_PEOPLE))


def _seed_day(con, day_base: datetime, person: str) -> None:
    """Seed one day's worth of a recurring (but not important) pattern: an email from
    `person`, who also has a meeting today. Same SHAPE every day, different person. The
    event is seeded around local midday and as a normal busy event; we also widen the
    window so it reliably lands in 'today' regardless of timezone boundary."""
    con.execute("DELETE FROM events")
    con.execute("DELETE FROM email_messages")
    # an event spanning most of today (start early, end late) so it's unambiguously 'today'
    start = day_base.replace(hour=9, minute=0, second=0, microsecond=0)
    end = day_base.replace(hour=18, minute=0, second=0, microsecond=0)
    con.execute(
        "INSERT INTO events(source,title,start_at,end_at,busy_status,sensitivity,last_seen) "
        "VALUES('sim',?,?,?,'busy','normal',?)",
        (f"Meeting with {person}", start.isoformat(), end.isoformat(), day_base.isoformat()))
    con.execute(
        "INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
        "VALUES('sim',?,?,?,?,'normal',?)",
        (f"{person} <{person.lower()}@example.com>", "agenda for our meeting", "see attached",
         day_base.isoformat(), day_base.isoformat()))


async def simulate(days: int = 14, *, seed: bool = True,
                   source_db: str | None = None,
                   hour: int | None = None,
                   scheduler_path: bool = False) -> dict[str, Any]:
    """Replay `days` days through the surpriser against a temp DB. Returns a per-day log
    plus a summary breakdown. Never touches the live DB or sends real pushes.

    seed=True injects the recurring-pattern seed (to test anti-noise on repetition).
    source_db copies an existing DB (e.g. your real one on the Mac) as the starting point,
    so you can simulate against real data without mutating it.

    hour pins the simulated time-of-day (0-23). This matters: the surpriser is silent
    22:00-08:00, so a sim run during quiet hours would show '0 surfaced' and falsely look
    'healthy' while actually proving nothing about SHAPE suppression. A seeded run
    therefore defaults to a daytime hour (14:00) so it tests the anti-noise mechanism, not
    quiet hours. Pass hour explicitly to probe a specific time. (QA-audit fix.)

    scheduler_path=True drives each day through the scheduler's run_due_jobs (with the
    surpriser job enabled) instead of calling pick_surprise directly, so the simulation
    exercises the REAL scheduling path, not just the picker. (QA-audit rec 7.)
    """
    from .. import db as dbmod

    # Choose the simulated hour. A SEEDED anti-noise run must be in daytime or it tests the
    # wrong thing; an explicit hour always wins.
    if hour is None:
        sim_hour = 14 if seed else None
    else:
        sim_hour = max(0, min(23, hour))

    # Build an isolated temp DB.
    tmpdir = tempfile.mkdtemp(prefix="mongoose-sim-")
    tmp_db = os.path.join(tmpdir, "sim.sqlite3")
    if source_db and os.path.exists(source_db):
        import shutil
        shutil.copy2(source_db, tmp_db)

    # The site profile is normally found beside the brain, so pin it to the REAL brain's profile before
    # swapping in the temp DB: the simulated surpriser must see the same identity the live one does.
    from .. import profile as _profile
    people = _seed_people()
    prev_profile = os.environ.get("MONGOOSE_PROFILE_FILE")
    os.environ["MONGOOSE_PROFILE_FILE"] = str(_profile.profile_path())
    prev = os.environ.get("MONGOOSE_DB_PATH")
    os.environ["MONGOOSE_DB_PATH"] = tmp_db
    # settings has no cache, but be explicit for safety.
    try:
        import mongoose.settings as st
        st._SETTINGS = None  # type: ignore[attr-defined]
    except Exception:
        pass

    from . import surpriser as S

    # Pin the surpriser's time-of-day for the run if a sim_hour was chosen, so the sim
    # tests SHAPE suppression rather than accidentally testing quiet-hours suppression.
    _orig_now_local = S._now_local
    if sim_hour is not None:
        def _pinned():
            n = _orig_now_local()
            return n.replace(hour=sim_hour, minute=0, second=0, microsecond=0)
        S._now_local = _pinned

    # In scheduler-path mode the real run_surpriser_cycle would try to PUSH to the phone;
    # simulate a successful (but non-real) delivery so the cycle completes and nothing
    # leaves the machine. Restored in finally.
    _orig_send = None
    if scheduler_path:
        try:
            from . import notifications as _notif

            async def _fake_send(*a, **k):
                return {"sent": True}
            _orig_send = _notif.send_to_phone
            _notif.send_to_phone = _fake_send
        except Exception:
            _orig_send = None

    # categories we count
    summary = {"surfaced": 0, "silent": 0,
               "suppressed_duplicate": 0, "suppressed_shape": 0,
               "suppressed_low_value": 0, "suppressed_quiet_hours": 0,
               "suppressed_min_gap": 0}
    day_log: list[dict[str, Any]] = []
    candidates_total = 0  # total candidates the seed produced across all days (QA-audit P0.2)

    try:
        dbmod.init_db()
        for d in range(days):
            if seed:
                with dbmod.connect() as con:
                    person = people[d % len(people)]
                    # Seed in LOCAL time at a daytime hour. The audit moved the Surpriser's
                    # candidate path to a local-only picture that reads events_today() (which
                    # queries the configured-timezone 'today'), so a UTC-placed event could fall
                    # outside the local day near a timezone boundary and produce zero candidates.
                    # Local-time seeding lands it in 'today'; the email's 24h freshness window
                    # (a duration, not a day boundary) is satisfied either way.
                    seed_at = S._now_local().replace(hour=12, minute=0, second=0, microsecond=0)
                    _seed_day(con, seed_at, person)
                    con.commit()

            if scheduler_path:
                # Drive the REAL scheduling path: enable the surpriser job and run due jobs
                # at the (pinned) now, then read what the surpriser recorded this cycle.
                os.environ["MONGOOSE_SURPRISER_ENABLED"] = "true"
                try:
                    import mongoose.settings as _st
                    _st._SETTINGS = None  # type: ignore[attr-defined]
                except Exception:
                    pass
                from .scheduler import run_due_jobs, _ensure_table as _sched_ensure
                # ensure the interval gap is satisfied: clear the job's last-run each day
                _sched_ensure()
                with dbmod.connect() as con:
                    con.execute("DELETE FROM scheduler_jobs WHERE name='surpriser'")
                    con.commit()
                sim_now = S._now_local()
                jobs = await run_due_jobs(now=sim_now)
                sj = jobs.get("surpriser", {})
                res = sj.get("result", {}) if isinstance(sj, dict) else {}
                if res.get("pushed"):
                    decision = {"surfaced": True, "suggestion": res.get("suggestion", {})}
                else:
                    decision = {"surfaced": False, "reason": res.get("reason", "")}
            else:
                decision = await S.pick_surprise(force=False, source="background")

            # Track whether the seed actually produced any candidate this day. A seeded run
            # that generates ZERO candidates across all days exercised nothing (e.g. a
            # seed-time/local-day mismatch put the seeded event on 'yesterday' locally, so
            # events_today() saw nothing) , it must NOT be reported as 'healthy'. (QA-audit
            # P0.2: seed-time coherence.)
            considered = decision.get("candidates_considered")
            if considered is None and decision.get("surfaced"):
                considered = 1
            if considered:
                candidates_total += int(considered)

            if decision.get("surfaced"):
                summary["surfaced"] += 1
                s = decision["suggestion"]
                day_log.append({"day": d + 1, "surfaced": True,
                                "kind": s.get("kind"), "text": s.get("text")})
            else:
                summary["silent"] += 1
                reason = decision.get("reason", "")
                supp = decision.get("suppression", {})
                if "quiet" in reason:
                    summary["suppressed_quiet_hours"] += 1
                elif "too soon" in reason:
                    summary["suppressed_min_gap"] += 1
                else:
                    summary["suppressed_duplicate"] += 1 if supp.get("duplicate", 0) else 0
                    summary["suppressed_shape"] += 1 if supp.get("shape", 0) else 0
                    summary["suppressed_low_value"] += 1 if supp.get("low_value", 0) else 0
                day_log.append({"day": d + 1, "surfaced": False, "reason": reason})

            # Advance the simulated clock by one day: age ALL surpriser_log rows back 24h
            # so the min-gap is satisfied next day, while the family cooldown (68h) still
            # suppresses a shape seen in the last ~3 days, exactly the behaviour we test.
            with dbmod.connect() as con:
                S._ensure_table(con)   # the table may not exist yet on a silent blank run
                rows = con.execute("SELECT id, created_at FROM surpriser_log").fetchall()
                for r in rows:
                    try:
                        ts = datetime.fromisoformat(r["created_at"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        con.execute("UPDATE surpriser_log SET created_at=? WHERE id=?",
                                    ((ts - timedelta(hours=24)).isoformat(), r["id"]))
                    except Exception:
                        pass
                con.commit()
    finally:
        # restore env, never leave the sim DB selected
        if prev is not None:
            os.environ["MONGOOSE_DB_PATH"] = prev
        else:
            os.environ.pop("MONGOOSE_DB_PATH", None)
        if prev_profile is not None:
            os.environ["MONGOOSE_PROFILE_FILE"] = prev_profile
        else:
            os.environ.pop("MONGOOSE_PROFILE_FILE", None)
        try:
            import mongoose.settings as st
            st._SETTINGS = None  # type: ignore[attr-defined]
        except Exception:
            pass
        # always restore the surpriser clock
        S._now_local = _orig_now_local
        # restore the real push function if we mocked it
        if scheduler_path and _orig_send is not None:
            try:
                from . import notifications as _notif
                _notif.send_to_phone = _orig_send
            except Exception:
                pass

    # an anti-noise verdict: on a recurring-but-unimportant seed, a healthy surpriser
    # surfaces a FEW times (the genuinely novel shapes) and is silent most days.
    total = max(1, days)
    surfaced_ratio = summary["surfaced"] / total

    # GUARD (QA-audit P0.2): if most silence came from QUIET HOURS, the run did NOT exercise
    # shape suppression, so it must NOT claim 'healthy'. Report inconclusive instead.
    quiet_dominated = summary["suppressed_quiet_hours"] >= max(1, days // 2) and summary["surfaced"] == 0

    # GUARD (QA-audit P0.2, seed-time coherence): a SEEDED run that produced zero candidates
    # across every day exercised nothing , the seed didn't land on the simulated 'today'
    # (e.g. a UTC-vs-local day rollover put the event on yesterday locally, so events_today()
    # saw nothing). Such a run must be INCONCLUSIVE, never 'healthy', because '0 surfaced'
    # here means 'the test never ran', not 'the surpriser stayed quiet'.
    seed_ineffective = seed and candidates_total == 0

    if seed_ineffective:
        verdict = ("inconclusive: the seed produced NO candidates (it did not land on the "
                   "simulated local day, a seed-time/timezone mismatch). This run did not "
                   "exercise shape suppression. Treat as INCONCLUSIVE, not healthy.")
        inconclusive = True
    elif quiet_dominated:
        verdict = ("inconclusive: silence was due to QUIET HOURS, not shape suppression. "
                   "Re-run with a daytime hour (this is the default for seeded runs, or pass "
                   "--hour 14) to actually test the anti-noise mechanism.")
        inconclusive = True
    elif surfaced_ratio <= 0.4:
        verdict = "healthy: stays quiet on repetition"
        inconclusive = False
    else:
        verdict = "NOISY: surfaces too often on a recurring pattern"
        inconclusive = False
    return {
        "days": days,
        "seeded": seed,
        "sim_hour": sim_hour,
        "source_db": bool(source_db),
        "summary": summary,
        "day_log": day_log,
        "surfaced_ratio": round(surfaced_ratio, 2),
        "candidates_generated": candidates_total,
        "seed_effective": (candidates_total > 0) if seed else None,
        "inconclusive": inconclusive,
        "verdict": verdict,
        "safety_note": "Simulation used a temporary copy DB and simulated push delivery; "
                       "it never mutated the live database or sent real notifications.",
    }


def format_simulation(res: dict[str, Any]) -> str:
    out = [f"Surpriser simulation: {res['days']} day(s), {res['summary']['surfaced']} surfaced",
           res["safety_note"], ""]
    for entry in res["day_log"]:
        if entry["surfaced"]:
            out.append(f"Day {entry['day']}: SURFACED · {entry.get('kind')} · {entry.get('text')}")
        else:
            out.append(f"Day {entry['day']}: silent ({entry.get('reason')})")
    s = res["summary"]
    out.append("")
    out.append("Breakdown:")
    out.append(f"  surfaced:               {s['surfaced']}")
    out.append(f"  silent:                 {s['silent']}")
    out.append(f"  - suppressed duplicate: {s['suppressed_duplicate']}")
    out.append(f"  - suppressed shape:     {s['suppressed_shape']}")
    out.append(f"  - suppressed low-value: {s['suppressed_low_value']}")
    out.append(f"  - suppressed quiet hrs: {s['suppressed_quiet_hours']}")
    out.append(f"  - suppressed min-gap:   {s['suppressed_min_gap']}")
    out.append("")
    if res.get("seeded"):
        out.append(f"Seed produced {res.get('candidates_generated', 0)} candidate(s) "
                   f"(seed_effective: {res.get('seed_effective')})")
    out.append(f"Surfaced ratio: {res['surfaced_ratio']}  ->  {res['verdict']}")
    return "\n".join(out)
