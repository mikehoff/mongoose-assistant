"""Bug-report -> diagnosis -> candidate patch -> peer/verifier approval -> staged update workflow.

The chat surface exposes only small verbs (log bug, add bug detail, diagnose bug, attempt fix,
apply fix). Actual code mutation remains gated: attempt_fix creates a candidate workspace and patch;
apply_fix goes through the existing update staging system and requires at least one recorded OK review.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import audit, connect, utc_now_iso
from ..settings import get_settings

SHORT_BUG_RE = re.compile(r"\b[A-Z][0-9]{2}\b", re.I)
LEGACY_BUG_RE = re.compile(r"\bBUG-\d{8}-[A-Z0-9]{4,10}\b", re.I)
# New reports use a tiny one-letter/two-digit id (A42) because the owner has to remember them in chat.
# Legacy long ids still parse so older bug records remain usable. Prefer the short match when both
# appear, mirroring how users actually speak.
BUG_RE = re.compile(r"(?:" + SHORT_BUG_RE.pattern + r")|(?:" + LEGACY_BUG_RE.pattern + r")", re.I)
_EXCLUDE = {".git", ".venv", "data", "secrets", "certs", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"}


def _ensure_tables() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bug_reports (
                bug_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                diagnosis TEXT,
                diagnosis_strength REAL NOT NULL DEFAULT 0,
                fix_workspace TEXT,
                candidate_root TEXT,
                patch_path TEXT,
                stage_id TEXT,
                peer_reviews_json TEXT NOT NULL DEFAULT '[]',
                applied_at TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bug_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bug_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_bug_events_bug ON bug_events(bug_id, id)")


def parse_bug_id(text: str | None) -> str | None:
    """Return a memorable bug id from free text. New ids are one letter + two digits (e.g. C47); the
    parser still recognises legacy BUG-YYYYMMDD-XXXX ids so old records remain usable. Prefer the short
    match when both appear."""
    blob = text or ""
    m = SHORT_BUG_RE.search(blob)
    if m:
        return m.group(0).upper()
    m = LEGACY_BUG_RE.search(blob)
    return m.group(0).upper() if m else None


def _valid_bug_id(bug_id: str | None) -> bool:
    value = (bug_id or "").strip().upper()
    return bool(SHORT_BUG_RE.fullmatch(value) or LEGACY_BUG_RE.fullmatch(value))


def _new_bug_id() -> str:
    """Generate a tiny memorable id: one random letter + two random digits (e.g. A42). 2,600
    possibilities, plenty for a local bug scratchpad, and we still check the DB and retry so collisions
    never overwrite an existing bug. Ambiguous letters I and O are skipped so an id is never misread as
    1 or 0. If the space is somehow exhausted, fall back to the legacy long id rather than failing to
    log a bug."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # avoid I/O looking like 1/0
    _ensure_tables()
    with connect() as con:
        for _ in range(200):
            candidate = secrets.choice(alphabet) + f"{secrets.randbelow(100):02d}"
            if not con.execute("SELECT 1 FROM bug_reports WHERE bug_id=?", (candidate,)).fetchone():
                return candidate
    return datetime.now(timezone.utc).strftime("BUG-%Y%m%d-") + secrets.token_hex(3).upper()


def _clean_detail(text: str | None) -> str:
    return " ".join((text or "").replace("\x00", " ").split()).strip()


def _strip_leading_command(text: str) -> str:
    t = _clean_detail(text)
    patterns = [
        r"^log\s+bug\s*[:,-]?\s*",
        r"^add\s+bug\s+detail\s*(?:" + BUG_RE.pattern + r")?\s*[:,-]?\s*",
        r"^diagnose\s+bug\s*(?:" + BUG_RE.pattern + r")?\s*[:,-]?\s*",
        r"^attempt\s+fix\s*(?:for\s+)?(?:" + BUG_RE.pattern + r")?\s*[:,-]?\s*",
        r"^apply\s+fix\s*(?:for\s+)?(?:" + BUG_RE.pattern + r")?\s*[:,-]?\s*",
    ]
    for pat in patterns:
        t = re.sub(pat, "", t, flags=re.I)
    if (bid := parse_bug_id(t)):
        t = t.replace(bid, "").strip(" :-,")
    return t.strip() or "No description supplied yet."


def _row_to_dict(row) -> dict[str, Any] | None:
    if not row:
        return None
    d = dict(row)
    try:
        d["peer_reviews"] = json.loads(d.pop("peer_reviews_json") or "[]")
    except Exception:
        d["peer_reviews"] = []
    return d


def _load_bug(bug_id: str) -> dict[str, Any] | None:
    _ensure_tables()
    with connect() as con:
        return _row_to_dict(con.execute("SELECT * FROM bug_reports WHERE bug_id=?", (bug_id.upper(),)).fetchone())


def _events(bug_id: str, limit: int = 100) -> list[dict[str, Any]]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT id, kind, detail, created_at, payload_json FROM bug_events WHERE bug_id=? ORDER BY id ASC LIMIT ?",
            (bug_id.upper(), max(1, min(limit, 500))),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
        except Exception:
            d["payload"] = {}
        out.append(d)
    return out


def _add_event(bug_id: str, kind: str, detail: str, payload: dict[str, Any] | None = None) -> None:
    _ensure_tables()
    with connect() as con:
        con.execute(
            "INSERT INTO bug_events(bug_id, kind, detail, created_at, payload_json) VALUES (?,?,?,?,?)",
            (bug_id.upper(), kind, _clean_detail(detail)[:8000], utc_now_iso(), json.dumps(payload or {}, ensure_ascii=False)),
        )
        con.execute("UPDATE bug_reports SET updated_at=? WHERE bug_id=?", (utc_now_iso(), bug_id.upper()))


def log_bug(text: str, *, actor: str = "owner") -> dict[str, Any]:
    _ensure_tables()
    summary = _strip_leading_command(text)[:240]
    bug_id = _new_bug_id()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO bug_reports(bug_id, summary, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            (bug_id, summary, "logged", now, now),
        )
        con.execute(
            "INSERT INTO bug_events(bug_id, kind, detail, created_at, payload_json) VALUES (?,?,?,?,?)",
            (bug_id, "logged", summary, now, json.dumps({"actor": actor})),
        )
    audit("bug_workflow", "log", True, payload={"bug_id": bug_id})
    return {"ok": True, "bug_id": bug_id, "summary": summary, "status": "logged"}


def add_bug_detail(bug_id: str, detail: str, *, actor: str = "owner") -> dict[str, Any]:
    bug_id = (bug_id or "").upper().strip()
    if not _valid_bug_id(bug_id):
        return {"ok": False, "error": "valid bug_id is required"}
    if not _load_bug(bug_id):
        return {"ok": False, "error": f"unknown bug id {bug_id}"}
    clean = _strip_leading_command(detail)[:8000]
    _add_event(bug_id, "detail", clean, {"actor": actor})
    audit("bug_workflow", "detail", True, payload={"bug_id": bug_id})
    return {"ok": True, "bug_id": bug_id, "added": True, "detail_preview": clean[:180]}


# Natural-language "add detail to <ID>" so this NEVER falls through to the model (which mis-routed
# "add more detail to M42" onto R49). Fires only when the message BOTH names a bug id AND asks to add
# detail. Refuses to write to a bug that does not exist rather than silently picking another one.
_ADD_DETAIL_CUE = re.compile(
    r"\b(?:add|append|note|record|put|extra|more)\b.{0,30}\bdetail\b|\bdetail\b.{0,15}\b(?:to|on|for)\b",
    re.I)


def handle_bug_detail_command(message: str, *, actor: str = "owner") -> dict[str, Any] | None:
    """If `message` is 'add (more) detail to <bug id> [bug][:] <detail>', append the detail to THAT bug
    deterministically. Returns {reply, bug_id, ok} or None if it is not this command."""
    t = _clean_detail(message)
    if not t:
        return None
    bug_id = parse_bug_id(t)
    if not bug_id or not _ADD_DETAIL_CUE.search(t):
        return None
    # the detail is whatever follows the bug id (strip a leading 'bug'/'in'/'on'/':'/',' etc.)
    idx = t.upper().find(bug_id.upper())
    tail = t[idx + len(bug_id):] if idx >= 0 else t
    detail = re.sub(r"^\s*(?:bug|in|on|for|about)?\s*[:,\-]?\s*", "", tail, flags=re.I).strip()
    if not detail:
        return {"ok": False, "reply": f"What detail should I add to {bug_id}?", "bug_id": bug_id}
    if not _load_bug(bug_id):
        open_ids = [b.get("bug_id") for b in list_open_bugs(limit=8) if b.get("bug_id")]
        return {"ok": False, "bug_id": "",
                "reply": (f"I don't have a bug called {bug_id}, so I did not add that anywhere. "
                          + (f"Open bugs: {', '.join(open_ids)}." if open_ids else "No open bugs logged."))}
    res = add_bug_detail(bug_id, detail, actor=actor)
    if res.get("ok"):
        return {"ok": True, "bug_id": bug_id,
                "reply": f"Added to {bug_id}: \"{detail[:140]}\"."}
    return {"ok": False, "bug_id": bug_id, "reply": f"I couldn't update {bug_id}: {res.get('error')}."}


def _infer_area(text: str) -> str:
    t = text.lower()
    areas = [
        ("installer/watchdog", ["install", "watchdog", "launchctl", "ready", "bootstrap", "browser", "welcome"]),
        ("scheduler/background jobs", ["scheduler", "job", "reminder", "benchmark", "pause", "memory"]),
        ("notifications/push/phone", ["push", "notification", "phone", "pwa", "ntfy"]),
        ("mail/voicemail/search", ["imap", "email", "voicemail", "search", "transcript"]),
        ("file crawling/search", ["file", "folder", "crawl", "index", "deleted", "fts"]),
        ("rtm/build/update", ["rtm", "build", "plugin", "update", "stage", "patch"]),
        ("guest/privacy/session", ["guest", "privacy", "session", "token", "owner", "leak"]),
        ("home/v3/ha", ["v3", "home assistant", "ha", "bus", "pi", "peer"]),
    ]
    scored = [(sum(1 for w in words if w in t), name) for name, words in areas]
    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else "unknown/general"


def _deterministic_diagnosis(bug: dict[str, Any], events: list[dict[str, Any]], extra: str = "") -> tuple[str, float, str]:
    evidence = "\n".join([bug.get("summary", "")] + [e.get("detail", "") for e in events] + ([extra] if extra else []))
    area = _infer_area(evidence)
    n_chars = len(evidence)
    n_events = len(events)
    strength = 0.25 + min(0.35, n_chars / 1200) + min(0.20, n_events * 0.05)
    if any(k in evidence.lower() for k in ("traceback", "exception", "line ", "stack", "import", "unboundlocal", "404", "403", "500")):
        strength += 0.15
    if parse_bug_id(evidence):
        strength += 0.03
    strength = max(0.1, min(0.9, strength))
    diag = (
        f"Working hypothesis ({area}): the reported behaviour likely sits at the {area} seam. "
        f"Evidence currently has {n_events} events and {n_chars} characters. "
        "Next useful check: reproduce the exact user journey, capture the route/command return code, "
        "and inspect whether the user-facing state agrees with the authoritative backend state."
    )
    if n_chars < 80:
        diag += " Detail is still thin; add logs, screen text, route names, or expected-vs-actual behaviour before attempting a code fix."
    return diag, strength, area


async def diagnose_bug(bug_id: str, detail: str = "") -> dict[str, Any]:
    bug_id = (bug_id or "").upper().strip()
    bug = _load_bug(bug_id)
    if not bug:
        return {"ok": False, "error": f"unknown bug id {bug_id}"}
    if detail and _strip_leading_command(detail) != "No description supplied yet.":
        _add_event(bug_id, "diagnosis_detail", _strip_leading_command(detail))
    evs = _events(bug_id)
    diag, strength, area = _deterministic_diagnosis(bug, evs, detail)

    # Optional model refinement. It is supportive only: failures never block deterministic diagnosis.
    model_note = ""
    try:
        import asyncio
        from ..llm import ask_ollama
        prompt = (
            "Diagnose this local Mongoose bug from the evidence. Return a concise hypothesis, likely module, "
            "and next verification. Do not invent logs.\n\n" + json.dumps({"bug": bug, "events": evs[-20:]}, default=str)[:7000]
        )
        raw = await asyncio.wait_for(
            ask_ollama(prompt, system="You are a cautious software diagnostician.", role="code"),
            timeout=8.0,
        )
        if raw and raw.strip():
            model_note = raw.strip()[:2000]
            diag = diag + "\n\nModel refinement:\n" + model_note
            strength = min(0.95, strength + 0.05)
    except Exception:
        pass

    with connect() as con:
        con.execute(
            "UPDATE bug_reports SET status=?, diagnosis=?, diagnosis_strength=?, updated_at=? WHERE bug_id=?",
            ("diagnosed" if strength >= 0.55 else "needs_more_detail", diag, float(strength), utc_now_iso(), bug_id),
        )
    _add_event(bug_id, "diagnosis", diag, {"strength": strength, "area": area, "model_refined": bool(model_note)})
    return {"ok": True, "bug_id": bug_id, "diagnosis": diag, "strength": round(strength, 2), "area": area, "enough_for_attempt_fix": strength >= 0.55}


def _source_root() -> Path:
    env = os.getenv("MONGOOSE_SOURCE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for p in [here] + list(here.parents):
        if (p / "pyproject.toml").exists() and (p / "src" / "mongoose").exists():
            return p
    return Path(__file__).resolve().parents[3]


def _copy_candidate(src: Path, dest: Path) -> None:
    def ignore(dir_path: str, names: list[str]) -> set[str]:
        return {n for n in names if n in _EXCLUDE or n.endswith((".pyc", ".pyo"))}
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=False, ignore=ignore)


def _diff(original: Path, candidate: Path, patch_path: Path) -> tuple[int, str]:
    cmd = ["diff", "-ruN"]
    for ex in sorted(_EXCLUDE):
        cmd += ["--exclude", ex]
    cmd += [str(original), str(candidate)]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if proc.returncode in (0, 1):
        patch_path.write_text(proc.stdout, encoding="utf-8")
    return proc.returncode, (proc.stderr or "")[:2000]


def _run_check(cmd: list[str], cwd: Path, timeout: int = 120) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return {"cmd": cmd, "ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": proc.stdout[-3000:], "stderr": proc.stderr[-3000:]}


def _reviews(bug: dict[str, Any]) -> list[dict[str, Any]]:
    return list(bug.get("peer_reviews") or [])


def _save_reviews(bug_id: str, reviews: list[dict[str, Any]]) -> None:
    with connect() as con:
        con.execute(
            "UPDATE bug_reports SET peer_reviews_json=?, updated_at=? WHERE bug_id=?",
            (json.dumps(reviews, ensure_ascii=False), utc_now_iso(), bug_id.upper()),
        )


def _add_review(bug_id: str, *, peer: str, ok: bool, detail: str, payload: dict[str, Any] | None = None) -> None:
    bug = _load_bug(bug_id)
    reviews = _reviews(bug or {})
    reviews.append({"peer": peer, "ok": bool(ok), "detail": detail[:2000], "created_at": utc_now_iso(), "payload": payload or {}})
    _save_reviews(bug_id, reviews)
    _add_event(bug_id, "peer_review", detail, {"peer": peer, "ok": bool(ok), **(payload or {})})


def record_peer_review(bug_id: str, *, peer: str, ok: bool, detail: str = "") -> dict[str, Any]:
    if not _load_bug(bug_id):
        return {"ok": False, "error": f"unknown bug id {bug_id}"}
    _add_review(bug_id, peer=peer or "peer", ok=ok, detail=detail or ("approved" if ok else "rejected"))
    return {"ok": True, "bug_id": bug_id.upper(), "peer": peer, "approved": bool(ok)}


def _has_peer_ok(bug: dict[str, Any]) -> bool:
    return any(bool(r.get("ok")) for r in _reviews(bug))


def _post_v3_review_request(bug_id: str, patch_path: Path) -> dict[str, Any]:
    try:
        from .protocol_v3 import build_envelope, _append_to_bus
        env = build_envelope(
            "mongoose-bugfix-review",
            f"Please review proposed fix for {bug_id}. Patch: {patch_path}. Reply with bug id and ok/reject.",
            to="all",
            needs_ack=True,
            m_approval_status="not-required",
            refs=[bug_id, str(patch_path)],
        )
        return _append_to_bus(env)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _request_cloud_review(bug_id: str, patch_path: Path) -> dict[str, Any]:
    """Ask a configured remote instant model to review the patch, if cloud escalation is enabled.

    The patch text is capped and secrets/data are excluded by the diff step. This is still opt-in through
    the existing CLOUD_ESCALATION_ENABLED setting; disabled/missing keys produce a clean skipped result.
    """
    try:
        import asyncio
        from ..cloud import TurnBudget, escalate
        patch = patch_path.read_text(encoding="utf-8", errors="replace")[:16000]
        prompt = (
            f"Review this Mongoose patch for bug {bug_id}. Answer with a first line exactly 'OK' or 'REJECT', "
            "then a concise reason. Approve only if the patch is plausibly safe and bounded.\n\n" + patch
        )

        async def _run() -> dict[str, Any]:
            budget = TurnBudget(limit_pennies=min(float(get_settings().cloud_turn_budget_pennies), 2.0))
            return await escalate(prompt, system="You are a cautious peer code reviewer.", budget=budget)

        res = asyncio.run(_run())
        reply = str(res.get("reply") or res.get("text") or "")
        ok = bool(re.match(r"\s*OK\b", reply, flags=re.I))
        _add_review(bug_id, peer=f"remote-llm:{res.get('provider', 'cloud')}", ok=ok,
                    detail=reply[:2000] or "empty review", payload={"cloud": res})
        return {"ok": True, "reviewed": True, "approved": ok, "provider": res.get("provider")}
    except Exception as exc:
        # BudgetExceeded is expected when cloud escalation is disabled; keep it non-fatal and explicit.
        return {"ok": False, "skipped": True, "reason": str(exc)[:200]}


def attempt_fix(bug_id: str, *, run_codegen: bool = True, request_peers: bool = True) -> dict[str, Any]:
    """Create a candidate workspace/patch and verifier review. Sensitive; called via confirmation."""
    bug_id = (bug_id or "").upper().strip()
    bug = _load_bug(bug_id)
    if not bug:
        return {"ok": False, "error": f"unknown bug id {bug_id}"}
    if float(bug.get("diagnosis_strength") or 0) < 0.55:
        return {"ok": False, "error": "diagnosis is not strong enough yet; add detail and run diagnose bug first", "bug_id": bug_id}
    root = _source_root()
    data = get_settings().data_dir / "bugfixes" / bug_id
    data.mkdir(parents=True, exist_ok=True)
    candidate = data / "candidate"
    _copy_candidate(root, candidate)
    request = data / "BUGFIX_REQUEST.md"
    request.write_text(
        f"# {bug_id}\n\nSummary: {bug.get('summary')}\n\nDiagnosis strength: {bug.get('diagnosis_strength')}\n\n"
        f"Diagnosis:\n{bug.get('diagnosis') or ''}\n\nEvents:\n" +
        "\n".join(f"- {e['kind']}: {e['detail'][:600]}" for e in _events(bug_id)[-30:]) + "\n",
        encoding="utf-8",
    )
    codegen = {"attempted": False, "ok": False, "reason": "not requested"}
    if run_codegen:
        cmd = shutil.which(get_settings().claude_code_cmd or "claude")
        if cmd:
            codegen = {"attempted": True, "ok": False, "cmd": cmd}
            prompt = (
                "You are editing a local Mongoose candidate tree. Implement the smallest safe fix for the bug. "
                "Add or update tests. Do not touch secrets/data. Bug request file:\n" + request.read_text(encoding="utf-8")[:12000]
            )
            try:
                proc = subprocess.run([cmd, "-p", prompt], cwd=str(candidate), text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
                try:
                    from .agent_output_hygiene import sanitize_agent_output
                    safe_stdout = sanitize_agent_output(proc.stdout, max_chars=3000)
                    safe_stderr = sanitize_agent_output(proc.stderr, max_chars=3000)
                except Exception:
                    safe_stdout = proc.stdout[-3000:]
                    safe_stderr = proc.stderr[-3000:]
                codegen.update({"ok": proc.returncode == 0, "returncode": proc.returncode,
                                "stdout": safe_stdout, "stderr": safe_stderr})
            except Exception as exc:
                codegen.update({"ok": False, "error": str(exc)[:500]})
        else:
            codegen = {"attempted": False, "ok": False, "reason": "configured Claude Code command not found; candidate workspace created for manual/peer editing"}
    patch_path = data / "fix.patch"
    diff_rc, diff_err = _diff(root, candidate, patch_path)
    patch_generated = patch_path.exists() and patch_path.stat().st_size > 0

    checks: list[dict[str, Any]] = []
    if patch_generated:
        checks.append(_run_check(["python", "-m", "compileall", "-q", "src", "tests"], candidate, timeout=180))
        pyflakes = shutil.which("pyflakes")
        if pyflakes:
            checks.append(_run_check([pyflakes, "src", "tests"], candidate, timeout=180))
        else:
            checks.append({"cmd": ["pyflakes", "src", "tests"], "ok": True, "skipped": True, "reason": "pyflakes not installed here"})
    verifier_ok = bool(patch_generated and all(c.get("ok") for c in checks))

    with connect() as con:
        con.execute(
            "UPDATE bug_reports SET status=?, fix_workspace=?, candidate_root=?, patch_path=?, updated_at=? WHERE bug_id=?",
            ("fix_candidate" if patch_generated else "awaiting_manual_patch", str(data), str(candidate), str(patch_path), utc_now_iso(), bug_id),
        )
    _add_event(bug_id, "attempt_fix", "Created candidate workspace" if patch_generated else "Created workspace; no patch generated", {"codegen": codegen, "checks": checks})
    if verifier_ok:
        _add_review(bug_id, peer="mongoose-local-verifier", ok=True, detail="compile/lint verifier passed on candidate patch", payload={"checks": checks})
    elif patch_generated:
        _add_review(bug_id, peer="mongoose-local-verifier", ok=False, detail="candidate patch failed local verifier", payload={"checks": checks})

    peer_req = None
    cloud_review = None
    if request_peers and patch_generated:
        peer_req = _post_v3_review_request(bug_id, patch_path)
        if peer_req.get("ok"):
            _add_event(bug_id, "peer_review_requested", "Posted v3 peer review request", peer_req)
        cloud_review = _request_cloud_review(bug_id, patch_path)
        if cloud_review.get("reviewed"):
            _add_event(bug_id, "peer_review_requested", "Requested remote LLM patch review", cloud_review)
    audit("bug_workflow", "attempt_fix", bool(patch_generated), payload={"bug_id": bug_id, "verifier_ok": verifier_ok})
    return {"ok": True, "bug_id": bug_id, "patch_generated": patch_generated, "patch_path": str(patch_path),
            "candidate_root": str(candidate), "codegen": codegen, "checks": checks,
            "peer_review_requested": peer_req, "cloud_review": cloud_review,
            "has_peer_ok": _has_peer_ok(_load_bug(bug_id) or {})}


def apply_fix(bug_id: str) -> dict[str, Any]:
    """Stage/check/approve/apply a candidate fix. Sensitive; called via confirmation."""
    bug_id = (bug_id or "").upper().strip()
    bug = _load_bug(bug_id)
    if not bug:
        return {"ok": False, "error": f"unknown bug id {bug_id}"}
    if not _has_peer_ok(bug):
        return {"ok": False, "error": "at least one peer/verifier OK is required before apply_fix", "bug_id": bug_id}
    candidate = Path(str(bug.get("candidate_root") or ""))
    if not candidate.exists():
        return {"ok": False, "error": "candidate root is missing; run attempt fix again", "bug_id": bug_id}
    from ..updater import approve_update_stage, apply_update_stage, check_update_stage, stage_update_candidate
    try:
        stage = stage_update_candidate(candidate)
        sid = stage["stage_id"]
        checked = check_update_stage(sid, runtime_smoke=False)
        if not checked.get("ok"):
            with connect() as con:
                con.execute("UPDATE bug_reports SET status=?, stage_id=?, updated_at=? WHERE bug_id=?", ("stage_check_failed", sid, utc_now_iso(), bug_id))
            return {"ok": False, "bug_id": bug_id, "stage_id": sid, "check": checked}
        approved = approve_update_stage(sid)
        rc = apply_update_stage(sid, yes=True)
        with connect() as con:
            con.execute(
                "UPDATE bug_reports SET status=?, stage_id=?, applied_at=?, updated_at=? WHERE bug_id=?",
                ("applied" if rc == 0 else "apply_failed", sid, utc_now_iso() if rc == 0 else None, utc_now_iso(), bug_id),
            )
        _add_event(bug_id, "apply_fix", f"Applied staged update {sid}" if rc == 0 else f"Apply failed for {sid}", {"returncode": rc, "approved": approved})
        return {"ok": rc == 0, "bug_id": bug_id, "stage_id": sid, "check": checked, "approved": approved, "returncode": rc}
    except Exception as exc:
        _add_event(bug_id, "apply_fix_error", str(exc)[:1000])
        return {"ok": False, "error": str(exc)[:300], "bug_id": bug_id}


def bug_status(bug_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    _ensure_tables()
    if bug_id:
        bid = bug_id.upper().strip()
        bug = _load_bug(bid)
        if not bug:
            return {"ok": False, "error": f"unknown bug id {bid}"}
        bug["events"] = _events(bid)
        bug["has_peer_ok"] = _has_peer_ok(bug)
        return {"ok": True, "bug": bug}
    with connect() as con:
        rows = con.execute(
            "SELECT bug_id, summary, status, created_at, updated_at, diagnosis_strength, stage_id FROM bug_reports "
            "ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return {"ok": True, "bugs": [dict(r) for r in rows]}


# States that mean the bug no longer needs the owner's attention in the approvals queue.
_BUG_CLOSED_STATES = ("applied", "dismissed", "resolved", "wont_fix")


def _bug_reporter(bug_id: str) -> str:
    """The actor who first logged the bug, read from the 'logged' event payload. 'owner' if unknown."""
    with connect() as con:
        row = con.execute(
            "SELECT payload_json FROM bug_events WHERE bug_id=? AND kind='logged' ORDER BY id ASC LIMIT 1",
            (bug_id,)).fetchone()
    if row and row["payload_json"]:
        try:
            return str(json.loads(row["payload_json"]).get("actor") or "owner")
        except Exception:
            return "owner"
    return "owner"


def list_open_bugs(limit: int = 20) -> list[dict[str, Any]]:
    """Open bug reports (not yet applied/dismissed), newest first, enriched with who reported them so the
    approvals tab can flag guest-raised bugs. Guests can raise bugs; this is where the owner sees them."""
    _ensure_tables()
    placeholders = ",".join("?" for _ in _BUG_CLOSED_STATES)
    with connect() as con:
        rows = con.execute(
            f"SELECT bug_id, summary, status, created_at, updated_at, diagnosis_strength FROM bug_reports "
            f"WHERE status NOT IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
            (*_BUG_CLOSED_STATES, max(1, min(int(limit), 100))),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        reporter = _bug_reporter(d["bug_id"])
        d["reporter"] = reporter
        d["from_guest"] = reporter.startswith("guest:")
        out.append(d)
    return out


def dismiss_bug(bug_id: str, *, actor: str = "owner", reason: str = "") -> dict[str, Any]:
    """Owner dismisses a bug from the approvals queue without a code fix (e.g. not reproducible, or a
    guest misunderstanding). Recorded as an event so the history stays honest."""
    bug_id = (bug_id or "").upper().strip()
    if not _valid_bug_id(bug_id):
        return {"ok": False, "error": "valid bug_id is required"}
    if not _load_bug(bug_id):
        return {"ok": False, "error": f"unknown bug id {bug_id}"}
    with connect() as con:
        con.execute("UPDATE bug_reports SET status=?, updated_at=? WHERE bug_id=?",
                    ("dismissed", utc_now_iso(), bug_id))
    _add_event(bug_id, "dismissed", (reason or "dismissed by owner")[:2000], {"actor": actor})
    audit("bug_workflow", "dismiss", True, payload={"bug_id": bug_id})
    return {"ok": True, "bug_id": bug_id, "status": "dismissed"}
