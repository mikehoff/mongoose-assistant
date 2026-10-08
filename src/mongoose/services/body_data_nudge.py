"""Occasionally ask the owner for their training/nutrition data, so the body features stay real.

Mongoose's per-muscle progression is only real once the Alpha Progression export is imported, and
nutrition is only real once MyFitnessPal data is present. Rather than silently degrading to "not built
yet", Mongoose occasionally surfaces a concrete request for that data through chat (and the cockpit /
phone provide the upload facilities to satisfy it).

This is a NUDGE, not a nag. It borrows the surpriser's discipline:
  - It only fires when the data is actually missing or stale (no point asking for what we already have).
  - It respects a minimum gap between asks (so it can't pester turn after turn).
  - It backs off hard once asked, and further if the owner declines.
  - Importing data immediately silences it.

The decision is pure and testable: `due_nudge()` returns a request dict or None. The chat loop calls
it and, when it returns something, weaves the `ask` into the reply. The cockpit and phone call
`nudge_status()` to show the same prompt as a card.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..db import get_app_state, set_app_state

# How the nudge paces itself. These are deliberately conservative: a data request is a real
# interruption, so it earns its place by being rare and only when useful.
_STATE_KEY = "body_data_nudge_v1"
_MIN_GAP_SECONDS = 60 * 60 * 36          # at most roughly once every day and a half, even if missing
_DECLINE_BACKOFF_SECONDS = 60 * 60 * 24 * 7  # a decline pushes the next ask out by a week
_STALE_AFTER_SECONDS = 60 * 60 * 24 * 21      # Alpha data older than ~3 weeks counts as stale-worth-refreshing


def _now() -> float:
    return time.time()


def _load() -> dict[str, Any]:
    raw = get_app_state(_STATE_KEY, "{}") or "{}"
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(state: dict[str, Any]) -> None:
    set_app_state(_STATE_KEY, json.dumps(state, sort_keys=True))


# --- what data do we have, and how fresh? ------------------------------------

def _alpha_state() -> dict[str, Any]:
    """Whether Alpha training data is present, and how old the newest import is."""
    present = False
    newest_import = None
    try:
        from ..db import connect
        with connect() as con:
            r = con.execute("SELECT COUNT(*) c, MAX(imported_at) m FROM bs_alpha_sets").fetchone()
            if r:
                present = (r["c"] or 0) > 0
                newest_import = r["m"]
    except Exception:
        present = False
    age = None
    if newest_import:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(newest_import).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = _now() - dt.timestamp()
        except Exception:
            age = None
    return {"present": present, "age_seconds": age, "newest_import": newest_import}


def _mfp_present() -> bool:
    """Whether any MyFitnessPal nutrition data has been imported."""
    try:
        from ..db import connect
        with connect() as con:
            # table created by the MFP importer; absent table means not present
            r = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bs_mfp_days'"
            ).fetchone()
            if not r:
                return False
            c = con.execute("SELECT COUNT(*) c FROM bs_mfp_days").fetchone()
            return (c["c"] if c else 0) > 0
    except Exception:
        return False


def data_status() -> dict[str, Any]:
    """A plain snapshot of what body data Mongoose has, for the cockpit/phone cards and the chat tool."""
    a = _alpha_state()
    stale = bool(a["present"] and a["age_seconds"] is not None and a["age_seconds"] > _STALE_AFTER_SECONDS)
    return {
        "alpha_present": a["present"],
        "alpha_stale": stale,
        "alpha_newest_import": a["newest_import"],
        "mfp_present": _mfp_present(),
    }


# --- the nudge decision ------------------------------------------------------

def _reason_to_ask(status: dict[str, Any]) -> str | None:
    """Pick the single most useful thing to ask for right now, or None if nothing is needed."""
    if not status["alpha_present"]:
        return "alpha_missing"
    if status["alpha_stale"]:
        return "alpha_stale"
    if not status["mfp_present"]:
        return "mfp_optional"
    return None


_ASKS = {
    "alpha_missing": (
        "I can only guess at your training so far. If you export your Alpha Progression data (the "
        "sessions and sets CSVs) and drop them in, I'll track per-muscle volume, your top weights and "
        "real muscle trends instead of guessing. You can upload from the cockpit Body data card or "
        "straight from your phone."
    ),
    "alpha_stale": (
        "Your Alpha Progression import is a few weeks old now. If you export a fresh copy and drop it "
        "in, your muscle trends and volume will reflect what you've actually been lifting lately. "
        "Cockpit Body data card, or from your phone."
    ),
    "mfp_optional": (
        "Optional: if you ever export your MyFitnessPal data and add it, I can start relating how you "
        "feel to protein, salt and carbs instead of saying I don't have your food data. No pressure: "
        "only if it's useful to you."
    ),
}


def due_nudge(*, force: bool = False) -> dict[str, Any] | None:
    """Return a data request to weave into chat, or None.

    Fires only when (a) some data is genuinely missing or stale, AND (b) enough time has passed since
    the last ask (a decline pushes that out further). `force=True` bypasses the timing gate (used by an
    explicit "what data do you need?" request), but still returns None when nothing is actually needed.
    """
    status = data_status()
    reason = _reason_to_ask(status)
    if reason is None:
        return None  # we have everything; never ask

    state = _load()
    if not force:
        last = float(state.get("last_asked_at", 0) or 0)
        gap = _MIN_GAP_SECONDS
        # if the owner declined this exact thing, back off harder
        if state.get("last_decline_reason") == reason:
            declined_at = float(state.get("last_declined_at", 0) or 0)
            if _now() - declined_at < _DECLINE_BACKOFF_SECONDS:
                return None
        if _now() - last < gap:
            return None

    return {
        "reason": reason,
        "optional": reason == "mfp_optional",
        "ask": _ASKS[reason],
        "upload": {
            "cockpit": "Body data card on the Setup tab (or the Activity tab)",
            "phone": "Share a CSV to Mongoose, or use the phone upload button",
            "accepts": (
                "Alpha Progression sessions CSV, Alpha Progression sets CSV"
                + ("" if status["alpha_present"] else ", body-composition CSV")
                + ", and optionally a MyFitnessPal export CSV"
            ),
        },
    }


def mark_asked(reason: str) -> None:
    """Record that we surfaced a request, so the gap timer starts. The chat loop calls this when it
    actually shows the ask to the owner."""
    state = _load()
    state["last_asked_at"] = _now()
    state["last_asked_reason"] = reason
    _save(state)


def mark_declined(reason: str) -> None:
    """The owner said not now: back this specific request off by a week."""
    state = _load()
    state["last_declined_at"] = _now()
    state["last_decline_reason"] = reason
    _save(state)


def mark_satisfied() -> None:
    """Data just arrived: clear decline suppression so the *next* legitimately-needed ask (e.g. a
    later staleness) isn't wrongly held back, and reset the asked timer."""
    state = _load()
    state["last_satisfied_at"] = _now()
    state.pop("last_decline_reason", None)
    state.pop("last_declined_at", None)
    state["last_asked_at"] = _now()  # don't immediately re-ask for the optional next thing
    _save(state)


def nudge_status() -> dict[str, Any]:
    """For the cockpit / phone cards: the current data status plus the request to show (if any),
    bypassing the chat timing gate so the card always reflects reality."""
    return {
        "data": data_status(),
        "request": due_nudge(force=True),
    }


# --- ingest (browser + phone both call this) ---------------------------------
# One place that takes an uploaded CSV's text, works out which kind it is (Alpha sessions, Alpha sets,
# Alpha body-comp, or MyFitnessPal), imports it, and marks the nudge satisfied. Used by the cockpit
# Body data card and the phone upload path, so both surfaces behave identically.

def _sniff_kind(filename: str, text: str) -> str | None:
    name = (filename or "").lower()
    head = "\n".join(text.splitlines()[:3]).lower()
    # explicit filename hints first
    if "mfp" in name or "myfitnesspal" in name or "nutrition" in name:
        return "mfp"
    if "sets" in name:
        return "alpha_sets"
    if "session" in name:
        return "alpha_sessions"
    if "body" in name and ("comp" in name or "composition" in name):
        return "alpha_body_comp"
    # then header sniffing
    if "calories" in head and ("carbohydrates" in head or "protein" in head):
        return "mfp"
    if "weight" in head and "reps" in head:
        return "alpha_sets"
    if ("n_sets" in head or "n_exercises" in head) or ("duration" in head and "title" in head):
        return "alpha_sessions"
    if ("weight" in head and ("body" in head or "fat" in head or "muscle" in head)):
        return "alpha_body_comp"
    return None


def ingest_csv(filename: str, text: str) -> dict[str, Any]:
    """Import an uploaded body-data CSV, auto-detecting Alpha sessions/sets/body-comp or MyFitnessPal.

    Returns the importer result plus the detected kind. Marks the nudge satisfied on success so chat
    stops asking. Both the cockpit and the phone call this.
    """
    if not text or not text.strip():
        return {"ok": False, "error": "empty file"}
    kind = _sniff_kind(filename, text)
    if kind is None:
        return {"ok": False,
                "error": ("couldn't tell what this CSV is. Name it so it's clear (…sessions.csv, "
                          "…sets.csv, …body-comp.csv, or a MyFitnessPal nutrition export), or check "
                          "it's the right export."),
                "detected": None}
    from . import alpha_import as A
    if kind == "alpha_sessions":
        res = A.import_sessions_csv(text)
    elif kind == "alpha_sets":
        res = A.import_sets_csv(text)
    elif kind == "alpha_body_comp":
        res = A.import_body_comp_csv(text)
    elif kind == "mfp":
        res = A.import_mfp_csv(text)
    else:
        return {"ok": False, "error": f"unhandled kind {kind!r}"}
    res = dict(res or {})
    res["detected"] = kind
    if res.get("ok"):
        mark_satisfied()
    return res
