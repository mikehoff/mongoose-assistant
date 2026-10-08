from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

# Two-lane diagnostics + metrics system.
#
# The idea: Mongoose continuously captures what's happening to itself , bugs, benchmark results,
# inference timings, issues, insights, and metric deltas , as typed events with a WEIGHT (how much
# it matters) and a SCOPE (which subsystem). There are two LANES:
#   - fast: frequent, lightweight captures (a metric delta, a caught error, an inference timing).
#   - slow: heavier, less frequent (a benchmark run, a deep diagnostic sweep).
# Events accumulate. When the accumulated weight crosses a threshold OR the oldest un-reported event
# is older than a max age (whichever comes first), a REPORT becomes "ready" and the user is told
# they can export it and upload it to Claude. Exporting produces a readable .md + a JSON payload,
# marks those events reported, and resets the accumulator , so collection just carries on.
#
# Delta-saving is the discipline that keeps this from being noise: a metric is only recorded when it
# has changed meaningfully from the last value for that scope, not on every poll.

# Event kinds and their default weights (how much each pushes toward "report ready"). Tunable.
_KIND_WEIGHTS = {
    "bug": 5.0,            # something broke , high signal
    "issue": 3.0,         # a degradation / warning short of a break
    "benchmark": 2.0,     # a benchmark result (slow lane)
    "inference": 1.0,     # an inference timing / quality sample (fast lane)
    "metric_delta": 1.5,  # a metric changed meaningfully
    "insight": 2.0,       # a derived insight worth surfacing
}

# Which lane each kind defaults to.
_KIND_LANE = {
    "bug": "fast", "issue": "fast", "inference": "fast", "metric_delta": "fast",
    "benchmark": "slow", "insight": "slow",
}

# When is a report "ready"? Whichever fires first.
READY_WEIGHT_THRESHOLD = 25.0       # accumulated un-reported weight
READY_MAX_AGE_HOURS = 24.0          # oldest un-reported event age

# Default "meaningful change" for delta-saving (relative). A metric must move at least this
# fraction from its last recorded value to be saved. Per-scope overrides allowed.
_DEFAULT_DELTA_REL = 0.05


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _ensure_tables() -> None:
    from ..db import connect
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS diag_events (
                id TEXT PRIMARY KEY, kind TEXT, scope TEXT, lane TEXT,
                weight REAL, summary TEXT, payload TEXT,
                reported INTEGER DEFAULT 0, created_at TEXT
            )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_diag_unreported "
                    "ON diag_events(reported, created_at)")
        # last value per metric scope, for delta-saving
        con.execute("""
            CREATE TABLE IF NOT EXISTS diag_metric_last (
                scope TEXT PRIMARY KEY, value REAL, at TEXT
            )""")
        con.commit()


# --- capture -----------------------------------------------------------------

def record_event(kind: str, *, scope: str, summary: str,
                 weight: float | None = None, lane: str | None = None,
                 payload: dict[str, Any] | None = None,
                 at: datetime | None = None) -> dict[str, Any]:
    """Record a typed diagnostic event. kind in {bug, issue, benchmark, inference, metric_delta,
    insight}. weight/lane default from the kind if not given. This is the single capture point, the
    fast and slow lanes both land here, differing only in how often they're called and their lane
    tag."""
    _ensure_tables()
    if kind not in _KIND_WEIGHTS:
        kind = "issue"  # unknown kinds degrade to 'issue' rather than being lost
    w = weight if weight is not None else _KIND_WEIGHTS[kind]
    ln = lane or _KIND_LANE.get(kind, "fast")
    eid = "ev_" + uuid.uuid4().hex[:10]
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO diag_events(id,kind,scope,lane,weight,summary,payload,reported,"
                    "created_at) VALUES(?,?,?,?,?,?,?,0,?)",
                    (eid, kind, scope, ln, w, summary,
                     json.dumps(payload or {}), _iso(at)))
        con.commit()
    return {"id": eid, "kind": kind, "scope": scope, "lane": ln, "weight": w}


def record_metric(scope: str, value: float, *, unit: str = "",
                  min_rel_change: float | None = None,
                  at: datetime | None = None) -> dict[str, Any]:
    """Delta-saving: record a metric ONLY if it changed meaningfully from the last value for this
    scope. Returns {saved: bool, ...}. This is what keeps the fast lane signal not noise, a metric
    that hasn't moved isn't re-logged."""
    _ensure_tables()
    thresh = _DEFAULT_DELTA_REL if min_rel_change is None else min_rel_change
    from ..db import connect
    with connect() as con:
        row = con.execute("SELECT value FROM diag_metric_last WHERE scope = ?", (scope,)).fetchone()
        last = row["value"] if row else None
        if last is not None:
            denom = abs(last) if abs(last) > 1e-9 else 1.0
            rel = abs(value - last) / denom
            if rel < thresh:
                return {"saved": False, "scope": scope, "value": value, "reason": "below_delta",
                        "rel_change": round(rel, 4)}
        con.execute("INSERT INTO diag_metric_last(scope,value,at) VALUES(?,?,?) "
                    "ON CONFLICT(scope) DO UPDATE SET value=excluded.value, at=excluded.at",
                    (scope, value, _iso(at)))
        con.commit()
    # a meaningful change becomes a metric_delta event
    delta_payload = {"value": value, "previous": last, "unit": unit}
    ev = record_event("metric_delta", scope=scope,
                      summary=f"{scope} {('->' if last is None else f'{last} -> ')}{value}{unit}".strip(),
                      payload=delta_payload, at=at)
    return {"saved": True, "scope": scope, "value": value, "previous": last, "event_id": ev["id"]}


# convenience wrappers for the common kinds (clear call sites)

def record_bug(scope: str, summary: str, **kw) -> dict[str, Any]:
    return record_event("bug", scope=scope, summary=summary, **kw)


def record_benchmark(scope: str, summary: str, payload: dict[str, Any] | None = None,
                     **kw) -> dict[str, Any]:
    return record_event("benchmark", scope=scope, summary=summary, payload=payload, **kw)


def record_inference(scope: str, summary: str, payload: dict[str, Any] | None = None,
                    **kw) -> dict[str, Any]:
    return record_event("inference", scope=scope, summary=summary, payload=payload, **kw)


def record_insight(scope: str, summary: str, **kw) -> dict[str, Any]:
    return record_event("insight", scope=scope, summary=summary, **kw)


# --- the "report ready" trigger ---------------------------------------------

def report_status(*, now: datetime | None = None) -> dict[str, Any]:
    """Is a report ready to upload to Claude? Ready when accumulated un-reported weight crosses the
    threshold OR the oldest un-reported event is older than the max age. Always honest about which
    (if either) fired, and how close the other is."""
    _ensure_tables()
    now = now or _now()
    from ..db import connect
    with connect() as con:
        rows = con.execute("SELECT weight, created_at, lane, kind FROM diag_events "
                           "WHERE reported = 0").fetchall()
    if not rows:
        return {"ready": False, "reason": "no_events", "accumulated_weight": 0.0,
                "event_count": 0}
    total_w = sum(r["weight"] for r in rows)
    oldest = min(datetime.fromisoformat(r["created_at"]) for r in rows)
    age_h = (now - oldest).total_seconds() / 3600.0
    by_weight = total_w >= READY_WEIGHT_THRESHOLD
    by_age = age_h >= READY_MAX_AGE_HOURS
    ready = by_weight or by_age
    reason = ("weight_threshold" if by_weight else
              ("max_age" if by_age else "accumulating"))
    return {"ready": ready, "reason": reason,
            "accumulated_weight": round(total_w, 1),
            "weight_threshold": READY_WEIGHT_THRESHOLD,
            "oldest_age_hours": round(age_h, 1),
            "max_age_hours": READY_MAX_AGE_HOURS,
            "event_count": len(rows),
            "fast_lane": sum(1 for r in rows if r["lane"] == "fast"),
            "slow_lane": sum(1 for r in rows if r["lane"] == "slow")}


async def maybe_notify_report_ready(*, now: datetime | None = None) -> dict[str, Any]:
    """If a report is ready, push a notification telling the user they can export it and upload it
    to Claude. Does NOT export automatically, collection carries on; the user exports when they
    choose. Safe to call frequently (the scheduler can poll this); only notifies on the transition
    to ready, not repeatedly."""
    st = report_status(now=now)
    if not st["ready"]:
        return {"notified": False, "status": st}
    # de-dupe: only notify once per ready-window using a marker
    from ..db import connect
    with connect() as con:
        marker = con.execute("SELECT value FROM diag_metric_last WHERE scope = '__report_notified'"
                             ).fetchone()
    already = marker and marker["value"] == 1
    if already:
        return {"notified": False, "status": st, "reason": "already_notified"}
    try:
        from .notifications import send_to_phone
        res = await send_to_phone(
            "Diagnostics report ready",
            f"{st['event_count']} events ({st['accumulated_weight']} weight), export and upload to "
            f"Claude when you like. Collection continues.",
            priority="default", tags=["bar_chart"])
    except Exception:
        res = None
    # Only set the once-per-window marker if the notice actually reached a phone; a failed push must not
    # mark it notified and hide the ready report forever.
    if not (res or {}).get("sent"):
        return {"notified": False, "status": st, "reason": "push_failed"}
    with connect() as con:
        con.execute("INSERT INTO diag_metric_last(scope,value,at) VALUES('__report_notified',1,?) "
                    "ON CONFLICT(scope) DO UPDATE SET value=1, at=excluded.at", (_iso(now),))
        con.commit()
    return {"notified": True, "status": st}


# --- export the bundle for Claude -------------------------------------------

def export_report(*, mark_reported: bool = True, now: datetime | None = None,
                  out_dir: str | None = None) -> dict[str, Any]:
    """Build the report bundle for Claude: a readable .md summary + a JSON payload, covering all
    un-reported events. If mark_reported, those events are flagged reported and the accumulator
    resets (so collection carries on from a clean slate). Returns the paths + the content."""
    _ensure_tables()
    now = now or _now()
    from ..db import connect
    with connect() as con:
        rows = con.execute("SELECT id,kind,scope,lane,weight,summary,payload,created_at "
                           "FROM diag_events WHERE reported = 0 ORDER BY created_at").fetchall()
    events = [dict(r) for r in rows]
    for e in events:
        try:
            e["payload"] = json.loads(e["payload"])
        except Exception:
            e["payload"] = {}

    md = _render_markdown(events, now)
    payload = {"generated_at": _iso(now), "event_count": len(events),
               "accumulated_weight": round(sum(e["weight"] for e in events), 1),
               "by_kind": _counts(events, "kind"),
               "by_scope": _counts(events, "scope"),
               "by_lane": _counts(events, "lane"),
               "events": events}

    import os
    base = out_dir or os.path.join(os.getenv("MONGOOSE_DATA_DIR", "."), "diagnostics_reports")
    os.makedirs(base, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    md_path = os.path.join(base, f"mongoose_diagnostics_{stamp}.md")
    json_path = os.path.join(base, f"mongoose_diagnostics_{stamp}.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    if mark_reported and events:
        ids = [e["id"] for e in events]
        with connect() as con:
            con.executemany("UPDATE diag_events SET reported = 1 WHERE id = ?",
                            [(i,) for i in ids])
            # clear the notified marker so the next window can notify again
            con.execute("INSERT INTO diag_metric_last(scope,value,at) VALUES('__report_notified',0,?) "
                        "ON CONFLICT(scope) DO UPDATE SET value=0, at=excluded.at", (_iso(now),))
            con.commit()

    return {"md_path": md_path, "json_path": json_path, "event_count": len(events),
            "markdown": md, "payload": payload, "reset": mark_reported}


def _counts(events: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        out[e[key]] = out.get(e[key], 0) + 1
    return out


def _render_markdown(events: list[dict[str, Any]], now: datetime) -> str:
    lines = ["# Mongoose diagnostics report", "",
             f"Generated: {_iso(now)}  ",
             f"Events: {len(events)}  |  Accumulated weight: {round(sum(e['weight'] for e in events), 1)}",
             ""]
    if not events:
        lines.append("_No un-reported events._")
        return "\n".join(lines)
    # summary tables
    lines.append("## Summary")
    lines.append("")
    for label, key in [("By kind", "kind"), ("By scope", "scope"), ("By lane", "lane")]:
        c = _counts(events, key)
        lines.append(f"{label}: " + ", ".join(f"{k} ({v})" for k, v in sorted(c.items())))
    lines.append("")
    # highest-weight first , the things most worth Claude's attention
    lines.append("## Events (highest weight first)")
    lines.append("")
    for e in sorted(events, key=lambda x: x["weight"], reverse=True):
        lines.append(f"- **[{e['kind']}/{e['scope']}]** (w{e['weight']}, {e['lane']}) "
                     f"{e['summary']}  ")
        if e["payload"]:
            compact = ", ".join(f"{k}={v}" for k, v in list(e["payload"].items())[:6])
            lines.append(f"  - {compact}")
    lines.append("")
    lines.append("_Upload this (and the JSON payload) to Claude. Mongoose has reset its accumulator "
                 "and is still collecting._")
    return "\n".join(lines)


# --- read helpers ------------------------------------------------------------

def recent_events(limit: int = 50, *, lane: str | None = None,
                  include_reported: bool = False) -> list[dict[str, Any]]:
    _ensure_tables()
    q = "SELECT id,kind,scope,lane,weight,summary,created_at,reported FROM diag_events"
    conds, params = [], []
    if not include_reported:
        conds.append("reported = 0")
    if lane:
        conds.append("lane = ?")
        params.append(lane)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    from ..db import connect as _c
    with _c() as con:
        rows = con.execute(q, params).fetchall()
    return [dict(r) for r in rows]
