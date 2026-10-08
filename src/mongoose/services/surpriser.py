from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any

# The Surpriser: Mongoose as a resident companion that occasionally offers a genuinely
# useful, unprompted suggestion, and stays quiet the rest of the time.
#
# The failure mode this is built to AVOID is the notification spammer: an assistant that
# interrupts with low-value "insights". So every rule here is about EARNING the
# interruption:
#   - grounded: suggestions come from real, fresh state changes and cross-domain
#     correlations (reusing the Daily Operating Picture), never model riffing.
#   - novel: each suggestion has a stable signature; we never surface the same one twice
#     (within a cooldown), so it can't nag.
#   - well-timed: quiet hours are respected; a minimum gap between surprises is enforced.
#   - selective: even when several candidates exist, only the single highest-value novel
#     one is offered. Silence is the default and a valid outcome.
#
# A "surprise" is something you'd want to know and probably wouldn't have noticed: a
# fresh email from someone you're about to meet, a freshly-indexed file that bears on
# today, weather that just turned against an outdoor plan, an unanswered message to
# someone in today's calendar. The value is the CORRELATION on FRESH findings.

# Cooldowns / timing (conservative by design).
_MIN_GAP_MINUTES = 22          # minimum time between two BACKGROUND substantive surprises (was 90;
                               # reduced ~4x so Mongoose surfaces findings more often)
_DELIGHT_GAP_MINUTES = 5       # delights are light and welcome, so they may appear more often than
                               # substantive findings (was 20; reduced 4x). Still gated by quiet
                               # hours, novelty, and rejection-suppression.
_SIGNATURE_COOLDOWN_HOURS = 20  # don't repeat the same suggestion within this window

# Kinds that recite knowledge/facts rather than offer a novel, actionable nudge. The owner has
# repeatedly rejected these ("tell me something new"): reciting his own imported domain notes back
# (e.g. RTM first principles to the person acquiring an RTM company) is noise. Excluded from EVERY
# proactive suggestion surface. The passive phone card in app.py imports this so there is one source.
PROACTIVE_NOISE_KINDS = {"knowledge_insight"}
_FAMILY_COOLDOWN_HOURS = 68     # don't repeat the same SHAPE/family within ~3 days unless
#                                 elevated (imminent event, or family marked useful). The
#                                 anti-noise fix: a recurring pattern is the same shape
#                                 every day even when the person differs; firing it daily is
#                                 noise though each instance is technically novel.
_REJECTED_SUPPRESS_DAYS = 30    # a suggestion marked not_useful/dismissed is suppressed far longer
_QUIET_START_HOUR = 22          # 22:00..08:00 local: no proactive surprises
_QUIET_END_HOUR = 8


def _ensure_table(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS surpriser_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signature TEXT NOT NULL,
            kind TEXT,
            text TEXT,
            score REAL,
            created_at TEXT NOT NULL,
            surfaced INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'background',
            family TEXT,
            feedback TEXT
        )""")
    # Older DBs may predate added columns; add them if missing (cheap, idempotent).
    cols = {r[1] for r in con.execute("PRAGMA table_info(surpriser_log)").fetchall()}
    if "source" not in cols:
        con.execute("ALTER TABLE surpriser_log ADD COLUMN source TEXT NOT NULL DEFAULT 'background'")
    if "family" not in cols:
        con.execute("ALTER TABLE surpriser_log ADD COLUMN family TEXT")
    if "feedback" not in cols:
        con.execute("ALTER TABLE surpriser_log ADD COLUMN feedback TEXT")


def _now_local():
    from ..settings import get_settings
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(get_settings().timezone))
    except Exception:
        return datetime.now(timezone.utc)


def _sig(kind: str, key: str) -> str:
    """Stable signature for novelty/dedup. The key is NORMALISED first (lowercased,
    whitespace collapsed, punctuation stripped) so the SAME underlying fact yields the
    SAME signature even if the rendered text varies slightly (a name punctuated
    differently across syncs, extra spaces, etc.). Without this, dedup is fragile to
    cosmetic text changes and a 'new' signature would re-surface the same suggestion."""
    norm = "".join(c.lower() if c.isalnum() else " " for c in (key or ""))
    norm = " ".join(norm.split())
    return hashlib.sha256(f"{kind}::{norm}".encode()).hexdigest()[:20]


def _in_quiet_hours(now_local: datetime) -> bool:
    h = now_local.hour
    if _QUIET_START_HOUR > _QUIET_END_HOUR:  # wraps midnight
        return h >= _QUIET_START_HOUR or h < _QUIET_END_HOUR
    return _QUIET_START_HOUR <= h < _QUIET_END_HOUR


def _local_surpriser_picture() -> dict[str, Any]:
    """Build the small, local-only picture the Surpriser needs.

    The Surpriser runs unattended in the background, so it must not block on live
    network/weather/doctor checks just to decide whether to stay quiet. It needs fresh
    local correlations, not the full Daily Operating Picture. This deliberately gathers
    from SQLite-backed/local sources only and then reuses the DOP cross-link logic.
    """
    picture: dict[str, Any] = {}
    try:
        from .calendar_tasks import events_today
        picture["events"] = events_today()
    except Exception:
        picture["events"] = []
    try:
        from .email_index import recent_email
        picture["email_recent"] = recent_email(hours=24, limit=20)
    except Exception:
        picture["email_recent"] = []
    try:
        from .whatsapp import unanswered_inbound
        picture["whatsapp_waiting"] = unanswered_inbound()
    except Exception:
        picture["whatsapp_waiting"] = []
    try:
        from .file_index import recent_files
        picture["files_recent"] = recent_files(hours=24, limit=20)
    except Exception:
        picture["files_recent"] = []
    try:
        from .calendar_tasks import tasks_today
        picture["tasks"] = tasks_today(include_open_without_due=True)
    except Exception:
        picture["tasks"] = []
    # No live weather fetch here. The DOP/brief can do richer weather synthesis on demand;
    # the background Surpriser should not hang on a remote call.
    picture["weather"] = None
    return picture


async def candidate_suggestions() -> list[dict[str, Any]]:
    """Compute candidate suggestions from fresh state + cross-domain correlations. Each
    has: kind, text (the suggestion), why (the grounding), score (value), and a signature
    (for dedup). Pure computation, no model invention. Never raises.

    Background-safe: this uses a local-only picture rather than full DOP so the
    Surpriser cannot hang on weather/doctor/live readiness probes while deciding whether
    to interrupt.
    """
    cands: list[dict[str, Any]] = []
    try:
        from .daily_picture import _cross_links
        dop = {"cross_domain_links": _cross_links(_local_surpriser_picture()), "what_needs_you": []}
    except Exception:
        return cands

    # 1) Cross-domain links are the richest surprises (e.g. email from someone you're
    # meeting today). They're already correlations on real data. Each carries a "family"
    # (its shape, independent of the specific person/event) so the surpriser can suppress a
    # REPEATED SHAPE, not just a repeated exact instance: "Email from X who's in today's
    # calendar" is the same shape whether X is Chris or Jordan, and surfacing it every single
    # day is noise even though each instance is technically novel.
    for link in dop.get("cross_domain_links", []):
        kind = link.get("kind", "link")
        text = link.get("text", "")
        # turn the observation into an actionable nudge
        if kind == "email_meets_calendar":
            suggestion = f"{text} Want me to pull it up before the meeting?"
            score = 0.9
        elif kind == "whatsapp_meets_calendar":
            suggestion = f"{text} Reply before you meet?"
            score = 0.95
        elif kind == "weather_threatens_event":
            suggestion = f"{text} Plan around it?"
            score = 0.8
        elif kind == "file_matches_today":
            suggestion = f"{text} Open it for the meeting?"
            score = 0.6
        else:
            suggestion = text
            score = 0.5
        cands.append({"kind": kind, "text": suggestion, "why": text,
                      "score": score, "signature": _sig(kind, text),
                      "family": kind,                       # the shape/template family
                      "imminent": bool(link.get("imminent")),  # event soon -> can override family cooldown
                      })

    # 2) A genuinely urgent "needs you" item. NOTE: repairs (ollama/db/scheduler down)
    # are deliberately weighted BELOW the surfacing threshold here: a broken capability
    # belongs in the cockpit's "what's broken" and the DOP's "needs you", not as an
    # unprompted ambient surprise (it's a static config state, not a fresh cross-domain
    # finding). The surpriser earns interruptions with novel correlations, not nags.
    now_items = [n for n in dop.get("what_needs_you", []) if n.get("urgency") == "now"]
    for n in now_items:
        if n.get("kind") == "repair":
            continue  # repairs are not proactive surprises
        cands.append({"kind": "needs_now", "text": f"Heads up: {n['text']}",
                      "why": n["text"], "score": 0.7,
                      "family": "needs_now", "imminent": True,
                      "signature": _sig("needs_now", n["text"])})

    # 2b) A gentle owner-only wellness check-in candidate. The scheduler is the main lane and can nudge
    # trusted guests through the mailbox; Surpriser only surfaces the owner nudge when it is due and has
    # not already happened today. Delivery still uses owner-targeted device profiles in run_surpriser_cycle.
    try:
        from .wellness_checkins import surpriser_candidate
        wc = surpriser_candidate()
        if wc:
            cands.append(wc)
    except Exception:
        pass

    # 2c) Knowledge-import care candidate: the adopted care planner ranks imported insights against the
    # current moment and already SUPPRESSES sensitive/ask-first/research-needed items, so nothing health
    # or AAS related is ever surfaced proactively here. Surface at most one, gently.
    try:
        from ..knowledge_import.care_planner import rank_care_candidates, refine_care_voice
        for cc in rank_care_candidates({"prompt": ""}, limit=3):
            text = cc.get("text")
            if not text:
                continue
            try:
                text = await refine_care_voice(text, tone=cc.get("tone", "warm_precise"),
                                               context=cc.get("matched_context"))
            except Exception:
                pass
            key = cc.get("insight_id") or cc.get("title") or text[:40]
            cands.append({"kind": "knowledge_insight", "text": text,
                          "why": cc.get("why") or "from your imported notes",
                          "score": float(cc.get("score") or 0.6),
                          "signature": _sig("knowledge_insight", str(key))})
            break
    except Exception:
        pass

    # 3) Device setup/staleness is deliberately NOT surfaced as a "suggestion". A suggestion should
    # be a novel cross-domain surprise, not a setup nag keyed off a device's name , which read as
    # random and wrong ("<device name>: enable Notification capture") especially right after a
    # re-pair. Unfinished device capabilities live in the Status tab, where they belong.

    # 4) "What changed" with a cross-domain flavour: if something changed AND it touches
    # today's calendar, that's a surprise; plain changes are NOT surfaced (too noisy).
    # (Cross-domain change detection is already captured by the links above; we keep this
    # branch deliberately narrow to avoid spam.)

    # A light moment, sparingly and OPT-IN: if the user has turned on proactive delight AND nothing
    # else of substance surfaced this cycle, Mongoose may offer a small interest-aware delight. It's
    # off by default so the surpriser's silence-when-nothing contract holds for everyone who hasn't
    # asked for it; on-demand delight (the CLI and "tell me a joke") is always available regardless.
    # Even when on, it scores LOW so any real finding wins, and all the surpriser's gap/quiet-hours/
    # family rules still gate whether it's shown.
    try:
        from ..settings import get_settings as _gs
        _delight_on = getattr(_gs(), "proactive_delight_enabled", False)
    except Exception:
        _delight_on = False
    substantive = [c for c in cands if c.get("score", 0) >= 0.55 or c.get("imminent")]
    if _delight_on and not substantive:
        try:
            from .delight import delight as _delight
            d = _delight(allow_model=False)  # background path stays offline (no model latency)
            if d.get("text") and d.get("source") != "none":
                label = "A fun fact" if d.get("kind") == "fact" else "A little something"
                cands.append({"kind": "delight", "text": d["text"],
                              "why": f"{label}, {d.get('interest', 'general')}",
                              "score": 0.08, "signature": _sig("delight", d["text"]),
                              "family": "delight", "imminent": False})
        except Exception:
            pass

    # Mongoose's music-taste streak. Same opt-in delight gate (proactive_delight_enabled) and the same
    # "only when nothing substantive" rule, so it never crowds out real findings and respects the
    # silence-when-nothing contract. Two flavours:
    #   - ONBOARDING: a brand-new user gets the eager interview question (on its own escalating schedule,
    #     enforced inside music_taste). This scores a touch higher than a plain delight so the get-to-
    #     know-you flurry actually lands while it's due.
    #   - AFTER onboarding: an occasional quirky music nudge/rec ("is X by Y up your alley? lol").
    if _delight_on and not substantive:
        try:
            from . import music_taste as _mt
            scope = _mt.OWNER_SCOPE
            # Music curiosity, proactive as well as in chat, is part of who Mongoose is: it is NOT gated
            # behind a tab opt-in. begin_if_new starts the onboarding clock by default; the only thing
            # that silences proactive music is an explicit opt-out. The _delight_on / not-substantive /
            # quiet-hours / rate-limit controls above and around this decide WHEN a nudge actually lands,
            # so ungating it does not make Mongoose a needy minute-by-minute interrogator.
            if not _mt.is_opted_out(scope):
                _mt.begin_if_new(scope)
                try:
                    from . import mailbox as _mb
                    _owner_name = _mb.name_for(_mb.OWNER)
                except Exception:
                    _owner_name = ""
                nudge = _mt.onboarding_nudge(scope, name=_owner_name)
                if nudge:
                    cands.append({"kind": "music_interview", "text": nudge["text"],
                                  "why": "Mongoose is learning your music taste",
                                  "score": 0.12, "signature": nudge["signature"],
                                  "family": "delight", "imminent": False})
                else:
                    rec = _mt.recommendation_candidate("", scope=scope)
                    cands.append({"kind": "music_rec", "text": rec["text"], "why": rec["why"],
                                  "score": 0.07, "signature": rec["signature"],
                                  "family": "delight", "imminent": False})
        except Exception:
            pass

    # sort by value, highest first
    cands.sort(key=lambda c: c.get("score", 0), reverse=True)
    return cands


def _suppressed_families(con) -> set[str]:
    """Families (suggestion SHAPES) that surfaced within the family cooldown AND were not
    marked useful. A family the user found useful is NOT shape-suppressed (they want that
    kind of nudge); an imminent-event instance can also override this at selection time.
    This is what stops the surpriser firing the same template every day."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_FAMILY_COOLDOWN_HOURS)).isoformat()
    rows = con.execute(
        "SELECT family, feedback FROM surpriser_log "
        "WHERE created_at > ? AND surfaced=1 AND family IS NOT NULL", (cutoff,)).fetchall()
    recent: set[str] = set()
    useful: set[str] = set()
    for r in rows:
        fam = r["family"]
        if not fam:
            continue
        recent.add(fam)
        if (r["feedback"] or "") == "useful":
            useful.add(fam)
    # a family marked useful is exempt from shape suppression
    return recent - useful


def _useful_families(con) -> set[str]:
    """Families the user has ever marked useful (these get latitude)."""
    rows = con.execute(
        "SELECT DISTINCT family FROM surpriser_log WHERE feedback='useful' AND family IS NOT NULL"
    ).fetchall()
    return {r["family"] for r in rows if r["family"]}


def _suppressed_signatures(con) -> set[str]:
    """Signatures we must NOT surface right now: anything shown within the normal cooldown
    (so it can't repeat), PLUS anything the user marked not_useful/dismissed within a much
    longer window (so a rejected suggestion durably backs off, not just for 20h). This is
    the fix for 'not_useful feedback did nothing'."""
    now = datetime.now(timezone.utc)
    recent_cut = (now - timedelta(hours=_SIGNATURE_COOLDOWN_HOURS)).isoformat()
    reject_cut = (now - timedelta(days=_REJECTED_SUPPRESS_DAYS)).isoformat()
    rows = con.execute(
        "SELECT signature, feedback, created_at FROM surpriser_log "
        "WHERE created_at > ?", (reject_cut,)).fetchall()
    out: set[str] = set()
    for r in rows:
        fb = (r["feedback"] or "")
        if fb in ("not_useful", "dismissed"):
            out.add(r["signature"])          # rejected: suppress for the long window
        elif r["created_at"] > recent_cut:
            out.add(r["signature"])          # recently shown: suppress for the short window
    return out


def _last_background_surface_at(con):
    """The min-gap should only count BACKGROUND surprises. On-demand 'Anything for me?'
    use must not push back the scheduled surpriser (the fix for on-demand starving it)."""
    row = con.execute(
        "SELECT created_at FROM surpriser_log WHERE surfaced=1 AND source='background' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        dt = datetime.fromisoformat(row["created_at"])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


_CROSS_DOMAIN_KINDS = {"email_meets_calendar", "whatsapp_meets_calendar", "weather_threatens_event", "file_matches_today"}


async def _ground_nudge(chosen: dict[str, Any]) -> str:
    """Let the MODEL phrase a cross-domain nudge from the real observation (the owner's always-think principle),
    so the actionable tail is Mongoose's voice, not a fixed 'Want me to pull it up?'. Falls back to the
    deterministic text if the model is unreachable. Never raises."""
    fallback = chosen.get("text") or ""
    why = (chosen.get("why") or fallback).strip()
    if not why:
        return fallback
    try:
        from ..llm import ask_persona
        out = (await ask_persona(
            "You noticed this connection across the owner's day (real, from their own data): "
            f"{why}\n\nSay it back in ONE short line as Mongoose, then offer the obvious next help as a "
            "natural question. One or two sentences, your own voice. Do not invent anything beyond the "
            "observation.", role="chat")).strip()
        return out or fallback
    except Exception:
        return fallback


async def pick_surprise(*, force: bool = False, source: str = "background") -> dict[str, Any]:
    """Choose at most ONE novel, well-timed, high-value suggestion to surface now, or
    explain why nothing was surfaced. Does NOT send; returns the decision.

    `force` bypasses quiet-hours and the min-gap (used by the on-demand 'suggest' command)
    but NOT novelty or rejection-suppression (so even forced, it won't repeat itself or
    resurface something you rejected). `source` records whether this was a background
    cycle or an on-demand request; only background surprises count toward the min-gap.
    """
    from ..db import connect
    now_local = _now_local()

    # quiet hours apply only to unsolicited (background) surprises.
    if not force and _in_quiet_hours(now_local):
        return {"surfaced": False, "reason": "quiet hours"}

    # Compute candidates BEFORE taking the write lock (it's the expensive async part).
    cands = await candidate_suggestions()
    # Drop pure knowledge recitations: reciting an imported domain note back to the owner (e.g. "Right
    # to Manage is a no-fault route" to the person acquiring an RTM company) is not a suggestion, it is
    # noise. The passive phone card already excluded these; do it here too so on-demand "surprise me",
    # chat, and the scheduler push agree. A suggestion must tell the owner something NEW and actionable.
    cands = [c for c in cands if c.get("kind") not in PROACTIVE_NOISE_KINDS]

    # Single transaction for check-and-insert, so two concurrent cycles cannot both
    # surface the same suggestion (closes the TOCTOU race).
    with connect() as con:
        _ensure_table(con)
        suppressed = _suppressed_signatures(con)
        shape_suppressed = _suppressed_families(con)
        useful_fams = _useful_families(con)

        # background min-gap (on-demand use does not count, and does not block). Delights use a SHORTER
        # gap than substantive findings: a light fun fact is welcome more often than a serious nudge.
        # So we only hard-block here if even the delight gap hasn't elapsed; otherwise we let selection
        # proceed and, below, drop substantive (non-delight) candidates if the full gap hasn't passed.
        gap_state = "open"
        if not force:
            last_bg = _last_background_surface_at(con)
            if last_bg:
                since = datetime.now(timezone.utc) - last_bg
                if since < timedelta(minutes=_DELIGHT_GAP_MINUTES):
                    return {"surfaced": False, "reason": "too soon since last surprise"}
                if since < timedelta(minutes=_MIN_GAP_MINUTES):
                    gap_state = "delight_only"  # only delights may surface in this window

        # Categorise WHY each candidate is or isn't eligible, so callers (the simulator,
        # the trust gate) can report a breakdown: duplicate vs shape vs low-value.
        reasons = {"duplicate": 0, "shape": 0, "low_value": 0}

        def _eligible(c):
            # In the delight-only window (full gap not yet elapsed, but the short delight gap has),
            # only delight-family candidates may surface; substantive findings wait for the full gap.
            if gap_state == "delight_only" and c.get("family") != "delight":
                reasons["low_value"] += 1
                return False
            if c["signature"] in suppressed:
                reasons["duplicate"] += 1
                return False
            # Delights are intentionally scored LOW (so any real finding outranks them), but a delight
            # should still be allowed to surface when it's the best/only thing. Exempt the delight
            # family from the value floor; all other gates (novelty, quiet hours, gap) still apply.
            if c.get("family") != "delight" and c.get("score", 0) < 0.55:
                reasons["low_value"] += 1
                return False
            fam = c.get("family")
            if fam and fam in shape_suppressed:
                elevated = c.get("imminent") or fam in useful_fams or c.get("score", 0) >= 0.93
                if not elevated:
                    reasons["shape"] += 1
                    return False
            return True

        fresh = [c for c in cands if _eligible(c)]
        if not fresh:
            # name the dominant suppression reason for legibility
            dominant = max(reasons, key=reasons.get) if any(reasons.values()) else None
            reason_text = {
                "duplicate": "already shown recently (same suggestion)",
                "shape": "same kind of suggestion shown recently (shape cooldown)",
                "low_value": "nothing above the value threshold",
            }.get(dominant, "nothing fresh and worth interrupting for")
            return {"surfaced": False,
                    "reason": reason_text,
                    "suppression": reasons,
                    "candidates_considered": len(cands)}

        chosen = fresh[0]
        # Final guard inside the same transaction: a concurrent writer may have just
        # surfaced this exact signature. Re-check immediately before inserting.
        already = con.execute(
            "SELECT 1 FROM surpriser_log WHERE signature=? AND created_at > ?",
            (chosen["signature"],
             (datetime.now(timezone.utc) - timedelta(hours=_SIGNATURE_COOLDOWN_HOURS)).isoformat())
        ).fetchone()
        if already:
            return {"surfaced": False, "reason": "just surfaced by another cycle"}

        con.execute(
            "INSERT INTO surpriser_log(signature, kind, text, score, created_at, surfaced, source, family) "
            "VALUES(?,?,?,?,?,1,?,?)",
            (chosen["signature"], chosen["kind"], chosen["text"], chosen.get("score", 0),
             datetime.now(timezone.utc).isoformat(), source, chosen.get("family")))
        con.commit()
    # Ground the ONE chosen cross-domain nudge in Mongoose's voice (the observation is real; only the
    # "want me to pull it up?" tail was canned). Done here, after the transaction and for the single winner,
    # so it's one model call, not one per candidate, and no DB connection is held during it.
    if chosen and chosen.get("kind") in _CROSS_DOMAIN_KINDS:
        chosen = {**chosen, "text": await _ground_nudge(chosen)}
    return {"surfaced": True, "suggestion": chosen, "candidates_considered": len(cands)}


async def run_surpriser_cycle() -> dict[str, Any]:
    """Scheduler entrypoint: pick a surprise and, if there is one, push it to the phone.
    Quiet, novelty-respecting, rate-limited. Returns what happened.

    Delivery integrity: pick_surprise atomically logs the chosen suggestion (which is what
    prevents two cycles surfacing duplicates). But a logged-but-undelivered suggestion
    would be silently lost (novelty would suppress it for hours though the user never saw
    it). So if the push FAILS, we roll the log row back, so the suggestion is retried next
    cycle instead of vanishing."""
    decision = await pick_surprise(force=False, source="background")
    if not decision.get("surfaced"):
        return {"pushed": False, "reason": decision.get("reason")}
    s = decision["suggestion"]
    try:
        from .notifications import send_to_phone
        pushed = await send_to_phone("Mongoose noticed", s["text"][:300],
                                     priority="default", tags="bulb",
                                     target_device_profiles=("primary_phone", "home_tablet"))
        if not pushed.get("sent", False):
            _rollback_surface(s["signature"])
            return {"pushed": False, "reason": "push not delivered", "suggestion": s}
        if s.get("kind") == "wellness_checkin":
            try:
                from .wellness_checkins import mark_surpriser_prompted
                mark_surpriser_prompted()
            except Exception:
                pass
        return {"pushed": True, "suggestion": s}
    except Exception as exc:
        _rollback_surface(s["signature"])
        return {"pushed": False, "error": str(exc), "suggestion": s}


def _rollback_surface(signature: str) -> None:
    """Remove the most recent background surface row for a signature whose push failed,
    so the suggestion can be retried rather than being silently consumed by novelty."""
    from ..db import connect
    try:
        with connect() as con:
            _ensure_table(con)
            con.execute(
                "DELETE FROM surpriser_log WHERE id=("
                "SELECT id FROM surpriser_log WHERE signature=? AND source='background' "
                "ORDER BY id DESC LIMIT 1)", (signature,))
            con.commit()
    except Exception:
        pass


def record_feedback(signature: str, feedback: str) -> dict[str, Any]:
    """Record 'useful' / 'not_useful' / 'dismissed' on a surfaced suggestion. A
    not_useful or dismissed mark durably suppresses that suggestion for
    _REJECTED_SUPPRESS_DAYS (far longer than the normal novelty cooldown), so a rejected
    nudge actually backs off instead of returning after 20h. (See _suppressed_signatures,
    which reads this feedback.)"""
    from ..db import connect
    if feedback not in ("useful", "not_useful", "dismissed"):
        return {"ok": False, "error": "feedback must be useful / not_useful / dismissed"}
    with connect() as con:
        _ensure_table(con)
        cur = con.execute(
            "UPDATE surpriser_log SET feedback=? WHERE signature=? AND id=("
            "SELECT id FROM surpriser_log WHERE signature=? ORDER BY id DESC LIMIT 1)",
            (feedback, signature, signature))
        con.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "no such suggestion"}
    return {"ok": True, "signature": signature, "feedback": feedback}


def recent_suggestions(limit: int = 20) -> list[dict[str, Any]]:
    """Recent surfaced suggestions and their feedback (for the cockpit)."""
    from ..db import connect
    with connect() as con:
        _ensure_table(con)
        rows = con.execute(
            "SELECT signature, kind, text, score, created_at, feedback FROM surpriser_log "
            "WHERE surfaced=1 ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
    return [dict(r) for r in rows]
