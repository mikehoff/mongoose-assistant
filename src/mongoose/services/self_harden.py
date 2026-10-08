from __future__ import annotations

import ast
import json
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from ..db import connect

# Self-harden is deliberately observation/proposal only. It does not patch live
# code and it does not install an update. Its job is to turn Mongoose's own
# failures into claim/invariant records, packets, and build-plan inputs.

_DEFAULT_CLAIMS: list[dict[str, str]] = [
    {
        "key": "install_runtime_identity",
        "category": "install",
        "promise": "Installed/running means the active server is this version and this source tree.",
        "authority": "/health + release.json + doctor --install-root",
        "invariant": "A healthy port is not enough: active server version and source identity must match the candidate.",
        "severity": "P0",
    },
    {
        "key": "configured_truth",
        "category": "setup",
        "promise": "Configured means genuinely usable or authorised, not a default/path placeholder.",
        "authority": "effective env + verifier + readiness + real connector call",
        "invariant": "Setup, doctor, readiness and connector tests must agree on usable state.",
        "severity": "P0",
    },
    {
        "key": "local_first_egress",
        "category": "privacy",
        "promise": "Nothing leaves the machine unless the user explicitly enables that route/action.",
        "authority": "system map + notification/search/sync delivery configuration",
        "invariant": "Channel availability is not consent to send private summaries through it.",
        "severity": "P0",
    },
    {
        "key": "action_approval_gate",
        "category": "safety",
        "promise": "Risky actions happen only after explicit approval.",
        "authority": "acting routes + MCP tools + approval receipts",
        "invariant": "Read paths cannot escalate to acting paths; failed sends never claim sent.",
        "severity": "P0",
    },
    {
        "key": "receipt_truth",
        "category": "observability",
        "promise": "What did you do? can be answered from receipts/audit, including failures and skips.",
        "authority": "receipts + audit log + scheduler job status",
        "invariant": "Meaningful actions, failures and skipped risky actions leave visible evidence.",
        "severity": "P1",
    },
    {
        "key": "backup_durability",
        "category": "durability",
        "promise": "A backup that reports success is actually restorable, locally and (if connected) off-site in Drive.",
        "authority": "backup archive manifest + brain integrity check + Drive size/md5 verification",
        "invariant": "An upload is 'ok' only if its bytes are verified against the local archive; the rota never deletes a good backup for an unverified one; failures are recorded, not silent.",
        "severity": "P0",
    },
    {
        "key": "search_source_truth",
        "category": "search",
        "promise": "Search reports what it actually searched across files, email and attachments.",
        "authority": "find source counts + crawler state + mail status",
        "invariant": "No-file-index must not imply no-email-search when mail is indexed.",
        "severity": "P1",
    },
    {
        "key": "memory_claim_integrity",
        "category": "memory",
        "promise": "Remembered means stored; forgotten means deleted/superseded; ambiguity asks.",
        "authority": "memory_facts + memory events + chat response",
        "invariant": "Mongoose must not merely say it remembered/forgot if the DB did not change accordingly.",
        "severity": "P1",
    },
    {
        "key": "rtm_ledger_integrity",
        "category": "rtm",
        "promise": "RTM balances, arrears and corrections are reconstructable and not double-counted.",
        "authority": "demand register + immutable ledger + export pack",
        "invariant": "Corrections are reversing entries; paid demands are not arrears; integers mean pounds.",
        "severity": "P0",
    },
    {
        "key": "scheduler_liveness",
        "category": "runtime",
        "promise": "Background jobs do not silently die, hang, or keep claiming they are alive.",
        "authority": "scheduler_jobs + runtime watchdog + liveness",
        "invariant": "Dead/stale/error jobs create observations; shutdown is bounded.",
        "severity": "P1",
    },
]

_SEVERITY_WEIGHT = {"P0": 5.0, "P1": 3.0, "P2": 1.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


def _read_json(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _ensure_tables() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS self_harden_claims (
                key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                promise TEXT NOT NULL,
                authority TEXT NOT NULL,
                invariant TEXT NOT NULL,
                severity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS self_harden_observations (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL,
                severity TEXT NOT NULL,
                claim_key TEXT,
                summary TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_self_harden_observations_status
              ON self_harden_observations(status, created_at);
            CREATE TABLE IF NOT EXISTS self_harden_module_index (
                name TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                purpose TEXT,
                commands_json TEXT NOT NULL,
                routes_json TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                db_tables_json TEXT NOT NULL,
                tests_json TEXT NOT NULL,
                external_calls_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS self_harden_runs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS self_harden_review_packets (
                id TEXT PRIMARY KEY,
                reviewer TEXT NOT NULL,
                observation_id TEXT,
                packet_dir TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # v0.54: enrich the module index without forcing a destructive rebuild.
        existing_cols = {r[1] for r in con.execute("PRAGMA table_info(self_harden_module_index)").fetchall()}
        for col in ("claims_json", "scheduler_jobs_json", "imports_json", "owner_json"):
            if col not in existing_cols:
                con.execute(f"ALTER TABLE self_harden_module_index ADD COLUMN {col} TEXT NOT NULL DEFAULT '[]'")
        now = _now()
        for c in _DEFAULT_CLAIMS:
            con.execute(
                """
                INSERT INTO self_harden_claims(key, category, promise, authority, invariant, severity, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  category=excluded.category,
                  promise=excluded.promise,
                  authority=excluded.authority,
                  invariant=excluded.invariant,
                  severity=excluded.severity,
                  updated_at=excluded.updated_at
                """,
                (c["key"], c["category"], c["promise"], c["authority"], c["invariant"], c["severity"], now, now),
            )
        con.commit()


def _observation_key(source: str, kind: str, scope: str, claim_key: str, summary: str) -> str:
    import hashlib
    raw = "|".join([source, kind, scope, claim_key or "", re.sub(r"\s+", " ", summary.strip().lower())])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def record_observation(
    source: str,
    kind: str,
    scope: str,
    summary: str,
    *,
    severity: str = "P1",
    claim_key: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record or refresh a hardening observation.

    Idempotent by source/kind/scope/claim/summary so a scheduler scan does not spam rows.
    """
    _ensure_tables()
    sev = severity if severity in _SEVERITY_WEIGHT else "P1"
    oid = _observation_key(source, kind, scope, claim_key, summary)
    now = _now()
    with connect() as con:
        con.execute(
            """
            INSERT INTO self_harden_observations(id, source, kind, scope, severity, claim_key, summary, evidence_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              severity=excluded.severity,
              evidence_json=excluded.evidence_json,
              status=CASE WHEN self_harden_observations.status='resolved' THEN 'reopened' ELSE self_harden_observations.status END,
              updated_at=excluded.updated_at
            """,
            (oid, source, kind, scope, sev, claim_key, summary.strip(), _json(evidence), now, now),
        )
        con.commit()
    return {"id": oid, "severity": sev, "claim_key": claim_key, "summary": summary.strip()}


def claims() -> list[dict[str, Any]]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute("SELECT * FROM self_harden_claims ORDER BY severity, category, key").fetchall()
    return [dict(r) for r in rows]


def recent(limit: int = 30, *, status: str = "") -> list[dict[str, Any]]:
    _ensure_tables()
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE status = ?"
        params.append(status)
    params.append(int(limit))
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM self_harden_observations {where} ORDER BY updated_at DESC LIMIT ?", params
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = _read_json(d.pop("evidence_json", "{}"), {})
        out.append(d)
    return out


def _record_run(kind: str, status: str, summary: str, payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_tables()
    rid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    with connect() as con:
        con.execute(
            "INSERT INTO self_harden_runs(id, kind, status, summary, payload_json, created_at) VALUES(?,?,?,?,?,?)",
            (rid, kind, status, summary, _json(payload), _now()),
        )
        con.commit()
    return {"id": rid, "kind": kind, "status": status, "summary": summary}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def scan_runtime() -> dict[str, Any]:
    """Collect cheap local symptoms and convert them into hardening observations.

    This is safe for the scheduler: no network, no live connector calls, no builds.
    """
    _ensure_tables()
    created: list[dict[str, Any]] = []

    # Scheduler errors / stale jobs are a recurrent false-success source.
    try:
        from .scheduler import job_status
        now = datetime.now(timezone.utc)
        for job in job_status():
            st = str(job.get("last_status") or "")
            if st == "error":
                created.append(record_observation(
                    "scheduler", "job_error", str(job.get("job")),
                    f"Scheduler job {job.get('job')} last ran with error",
                    severity="P1", claim_key="scheduler_liveness", evidence=job))
            last = _parse_iso(job.get("last_run"))
            sched = str(job.get("schedule") or "")
            if last and "every " in sched and sched.endswith("m"):
                try:
                    mins = int(sched.split("every ", 1)[1].split("m", 1)[0])
                    if (now - last) > timedelta(minutes=max(30, mins * 3)):
                        created.append(record_observation(
                            "scheduler", "job_stale", str(job.get("job")),
                            f"Scheduler job {job.get('job')} has not run within its expected freshness window",
                            severity="P1", claim_key="scheduler_liveness", evidence=job))
                except Exception:
                    pass
    except Exception as exc:
        created.append(record_observation(
            "self_harden", "scan_error", "scheduler", "Self-harden could not inspect scheduler state",
            severity="P2", claim_key="scheduler_liveness", evidence={"error": str(exc)[:300]}))

    # Runtime watchdog/liveness gap: if watchdog exists and says unhealthy, record it.
    try:
        from ..db import get_app_state
        raw = get_app_state("runtime_watchdog", "")
        wd = _read_json(raw, {}) if raw else {}
        if isinstance(wd, dict):
            if wd.get("scheduler_restart_count", 0):
                created.append(record_observation(
                    "watchdog", "scheduler_restarted", "runtime", "Runtime watchdog restarted the scheduler task",
                    severity="P1", claim_key="scheduler_liveness", evidence=wd))
            if float(wd.get("event_loop_lag_seconds") or 0) > 2.0:
                created.append(record_observation(
                    "watchdog", "event_loop_lag", "runtime", "Runtime watchdog saw material event-loop lag",
                    severity="P1", claim_key="scheduler_liveness", evidence=wd))
    except Exception:
        pass

    # Recent audit failures are direct hardening observations.
    try:
        with connect() as con:
            rows = con.execute(
                "SELECT module, action, reason, payload_json, created_at FROM audit_log WHERE success=0 ORDER BY id DESC LIMIT 25"
            ).fetchall()
        for r in rows:
            module = str(r["module"] or "unknown")
            action = str(r["action"] or "unknown")
            claim = "receipt_truth"
            if module in {"scheduler", "watchdog"}:
                claim = "scheduler_liveness"
            elif module.startswith("rtm") or action.startswith("rtm"):
                claim = "rtm_ledger_integrity"
            created.append(record_observation(
                "audit_log", "failed_action", f"{module}.{action}",
                f"Audit log recorded failed action {module}.{action}",
                severity="P1", claim_key=claim,
                evidence={"reason": r["reason"], "payload": _read_json(r["payload_json"], {}), "created_at": r["created_at"]},
            ))
    except Exception:
        pass

    # Claim-consistency: the highest-value class. For each claim whose authorities can be made
    # executable, run them over the same input and flag any DISAGREEMENT (two subsystems making the
    # same claim with different logic, e.g. the installer's process-identity rule drifting from
    # `mongoose stop`). This is evidence-only and never acts. The claim->self_harden mapping lives on
    # the claim itself (its `maps_to`), so there is no second copy of that mapping here.
    try:
        from . import claim_consistency as _cc
        _summary = _cc.run_builtin()
        for _rep in _summary.get("diverging", []):
            created.append(record_observation(
                "self_harden", "claim_divergence", _rep["key"],
                (f"Two subsystems disagree on '{_rep['key']}': {_rep['description']}"
                 f" (authorities returned different answers for the same input)."),
                severity="P1", claim_key=_rep.get("maps_to", ""),
                evidence={"results": _rep["results"], "divergence": _rep.get("divergence", {})},
            ))
    except Exception:
        pass

    payload = {"observations": len(created), "created": created[:20]}
    _record_run("scan", "ok", f"Recorded/refreshed {len(created)} hardening observation(s)", payload)
    return payload


def status() -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT severity, status, COUNT(*) AS n FROM self_harden_observations GROUP BY severity, status"
        ).fetchall()
        total = con.execute("SELECT COUNT(*) AS n FROM self_harden_observations WHERE status IN ('open','reopened')").fetchone()["n"]
        p0 = con.execute("SELECT COUNT(*) AS n FROM self_harden_observations WHERE severity='P0' AND status IN ('open','reopened')").fetchone()["n"]
        last = con.execute("SELECT * FROM self_harden_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    return {
        "open": total,
        "open_p0": p0,
        "by_status": [dict(r) for r in rows],
        "last_run": dict(last) if last else None,
        "top": recent(10, status="open"),
    }




_CLAIM_PATH_HINTS: list[tuple[str, str]] = [
    ("setup", "configured_truth"),
    ("wizard", "configured_truth"),
    ("install", "install_runtime_identity"),
    ("updater", "install_runtime_identity"),
    ("enable_phone", "install_runtime_identity"),
    ("notifications", "local_first_egress"),
    ("sync_checkpoint", "local_first_egress"),
    ("cloud", "local_first_egress"),
    ("whatsapp", "action_approval_gate"),
    ("home_router", "action_approval_gate"),
    ("mcp", "action_approval_gate"),
    ("receipts", "receipt_truth"),
    ("find", "search_source_truth"),
    ("search", "search_source_truth"),
    ("email_index", "search_source_truth"),
    ("file_index", "search_source_truth"),
    ("memory", "memory_claim_integrity"),
    ("rtm", "rtm_ledger_integrity"),
    ("scheduler", "scheduler_liveness"),
    ("liveness", "scheduler_liveness"),
    ("surpriser", "scheduler_liveness"),
]

_OWNER_HINTS: list[tuple[str, str]] = [
    ("setup", "setup/onboarding"),
    ("wizard", "setup/onboarding"),
    ("install", "install/update"),
    ("updater", "install/update"),
    ("cockpit", "cockpit/web-ui"),
    ("devices", "devices/phone-surfaces"),
    ("rtm", "rtm"),
    ("body", "body-state"),
    ("memory", "memory"),
    ("scheduler", "runtime/scheduler"),
    ("liveness", "runtime/scheduler"),
    ("surpriser", "companion/surpriser"),
    ("whatsapp", "social/whatsapp"),
    ("home", "home-control"),
    ("find", "search"),
    ("search", "search"),
]


def _infer_claims(rel: str, text: str) -> list[str]:
    hay = (rel + "\n" + text[:12000]).lower()
    claims: set[str] = set()
    for needle, claim in _CLAIM_PATH_HINTS:
        if needle in hay:
            claims.add(claim)
    if "httpx" in hay or "requests." in hay or "https://" in hay:
        claims.add("local_first_egress")
    if "approve" in hay or "send" in hay:
        claims.add("action_approval_gate")
    return sorted(claims)


def _infer_owner(rel: str) -> dict[str, str]:
    low = rel.lower()
    for needle, owner in _OWNER_HINTS:
        if needle in low:
            return {"area": owner}
    if low.startswith("src/mongoose/services/"):
        return {"area": "service"}
    return {"area": "core"}


def _extract_strings_from_ast(path: Path) -> dict[str, list[str]]:
    try:
        mod_ast = ast.parse(path.read_text(errors="replace"))
    except Exception:
        return {"commands": [], "routes": [], "settings": [], "db_tables": [], "external_calls": [], "imports": [], "scheduler_jobs": []}
    commands: set[str] = set()
    routes: set[str] = set()
    settings: set[str] = set()
    db_tables: set[str] = set()
    external: set[str] = set()
    imports: set[str] = set()
    scheduler_jobs: set[str] = set()
    text = path.read_text(errors="replace")
    for m in re.finditer(r"sub\.add_parser\(['\"]([^'\"]+)", text):
        commands.add(m.group(1))
    for m in re.finditer(r"@(?:app|router)\.(?:get|post|put|delete)\(['\"]([^'\"]+)", text):
        routes.add(m.group(1))
    for m in re.finditer(r"os\.getenv\(['\"]([A-Z0-9_]+)", text):
        settings.add(m.group(1))
    for m in re.finditer(r"getattr\(s, ['\"]([a-zA-Z0-9_]+)", text):
        settings.add(m.group(1))
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS ([a-zA-Z0-9_]+)", text):
        db_tables.add(m.group(1))
    for m in re.finditer(r"https?://[^\s'\")<]+", text):
        external.add(m.group(0))
    for node in ast.walk(mod_ast):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mongoose") or alias.name.startswith("src.mongoose"):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("mongoose") or node.level:
                imports.add(("." * node.level) + mod)
        elif isinstance(node, ast.Subscript):
            try:
                if isinstance(node.value, ast.Name) and node.value.id == "reg":
                    sl = node.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        scheduler_jobs.add(sl.value)
            except Exception:
                pass
    return {
        "commands": sorted(commands),
        "routes": sorted(routes),
        "settings": sorted(settings),
        "db_tables": sorted(db_tables),
        "external_calls": sorted(external),
        "imports": sorted(imports),
        "scheduler_jobs": sorted(scheduler_jobs),
    }


def rebuild_module_index(root: str | Path = ".") -> dict[str, Any]:
    """Index source modules into a simple map for future hardening plans."""
    _ensure_tables()
    rootp = Path(root)
    src = rootp / "src" / "mongoose"
    tests = rootp / "tests"
    if not src.exists():
        src = Path("src/mongoose")
    files = sorted(src.rglob("*.py")) if src.exists() else []
    test_files = list(tests.glob("test_*.py")) if tests.exists() else []
    test_names = [t.name for t in test_files]
    count = 0
    now = _now()
    with connect() as con:
        for path in files:
            rel = str(path.relative_to(rootp)) if path.is_relative_to(rootp) else str(path)
            name = rel.replace("/", ".").removesuffix(".py")
            meta = _extract_strings_from_ast(path)
            # Loose coverage hint by name tokens.
            stem = path.stem
            related_tests = [t for t in test_names if stem in t or any(part in t for part in stem.split("_") if len(part) > 4)]
            purpose = ""
            try:
                mod_ast = ast.parse(path.read_text(errors="replace"))
                doc = ast.get_docstring(mod_ast) or ""
                purpose = doc.strip().splitlines()[0][:300] if doc else ""
            except Exception:
                pass
            source_text = path.read_text(errors="replace")
            claims_for_module = _infer_claims(rel, source_text)
            owner = _infer_owner(rel)
            con.execute(
                """
                INSERT INTO self_harden_module_index(name,path,purpose,commands_json,routes_json,settings_json,db_tables_json,tests_json,external_calls_json,claims_json,scheduler_jobs_json,imports_json,owner_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                  path=excluded.path, purpose=excluded.purpose, commands_json=excluded.commands_json,
                  routes_json=excluded.routes_json, settings_json=excluded.settings_json,
                  db_tables_json=excluded.db_tables_json, tests_json=excluded.tests_json,
                  external_calls_json=excluded.external_calls_json, claims_json=excluded.claims_json,
                  scheduler_jobs_json=excluded.scheduler_jobs_json, imports_json=excluded.imports_json,
                  owner_json=excluded.owner_json, updated_at=excluded.updated_at
                """,
                (name, rel, purpose, _json(meta["commands"]), _json(meta["routes"]), _json(meta["settings"]),
                 _json(meta["db_tables"]), _json(sorted(related_tests)), _json(meta["external_calls"]),
                 _json(claims_for_module), _json(meta.get("scheduler_jobs", [])), _json(meta.get("imports", [])),
                 _json(owner), now),
            )
            count += 1
        con.commit()
    _record_run("module_index", "ok", f"Indexed {count} source module(s)", {"modules": count})
    return {"modules": count, "tests_seen": len(test_names)}


def module_index(limit: int = 50, query: str = "") -> list[dict[str, Any]]:
    _ensure_tables()
    where = ""
    params: list[Any] = []
    if query:
        where = "WHERE name LIKE ? OR path LIKE ?"
        params.extend([f"%{query}%", f"%{query}%"])
    params.append(int(limit))
    with connect() as con:
        rows = con.execute(f"SELECT * FROM self_harden_module_index {where} ORDER BY name LIMIT ?", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("commands", "routes", "settings", "db_tables", "tests", "external_calls", "claims", "scheduler_jobs", "imports"):
            d[k] = _read_json(d.pop(f"{k}_json", "[]"), [])
        d["owner"] = _read_json(d.pop("owner_json", "{}"), {})
        out.append(d)
    return out


def export_packet(path: str | Path | None = None, *, include_module_index: bool = True) -> dict[str, Any]:
    """Write a hardening packet for Claude/OpenAI review.

    The packet is a markdown report plus a JSON payload. It contains observations, claims,
    recent runs, and a compact module index. It never exports secrets from .env.
    """
    _ensure_tables()
    base = Path(path) if path else Path(os.environ.get("MONGOOSE_DATA_DIR") or "~/.mongoose/data").expanduser() / "hardening_packets" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base.mkdir(parents=True, exist_ok=True)
    st = status()
    cl = claims()
    obs = recent(100)
    mods = module_index(200) if include_module_index else []
    payload = {"status": st, "claims": cl, "observations": obs, "modules": mods, "generated_at": _now()}
    json_path = base / "hardening_packet.json"
    md_path = base / "hardening_packet.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# Mongoose Hardening Packet",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "## Status",
        f"Open observations: {st['open']} (P0: {st['open_p0']})",
        "",
        "## Top observations",
    ]
    for o in obs[:25]:
        lines.append(f"- **{o['severity']}** `{o.get('claim_key') or 'unmapped'}` {o['summary']} ({o['scope']})")
    lines += ["", "## Claim registry"]
    for c in cl:
        lines.append(f"- **{c['severity']}** `{c['key']}`: {c['promise']} | invariant: {c['invariant']}")
    if mods:
        lines += ["", "## Module index sample"]
        for m in mods[:40]:
            bits = []
            if m.get("commands"):
                bits.append("commands=" + ",".join(m["commands"][:5]))
            if m.get("routes"):
                bits.append("routes=" + ",".join(m["routes"][:5]))
            if m.get("tests"):
                bits.append("tests=" + ",".join(m["tests"][:5]))
            lines.append(f"- `{m['name']}`: {'; '.join(bits) if bits else 'indexed'}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _record_run("export", "ok", f"Exported hardening packet to {base}", {"dir": str(base)})
    return {"dir": str(base), "json_path": str(json_path), "md_path": str(md_path), "observations": len(obs)}




def reingest_codebase(root: str | Path = ".", *, out: str | Path | None = None) -> dict[str, Any]:
    """Rebuild the module map and write a code-reingest packet.

    This is the safe self-harden substrate: Mongoose learns which modules own
    which claims/invariants before it asks any model to propose code changes.
    It indexes source metadata only and never sends data or patches code.
    """
    r = rebuild_module_index(root)
    rows = module_index(500)
    by_claim: dict[str, list[str]] = {}
    by_owner: dict[str, list[str]] = {}
    for m in rows:
        for c in m.get("claims", []):
            by_claim.setdefault(c, []).append(m["name"])
        owner = (m.get("owner") or {}).get("area", "unknown")
        by_owner.setdefault(owner, []).append(m["name"])
    payload = {
        "generated_at": _now(),
        "root": str(root),
        "modules": rows,
        "by_claim": {k: sorted(v) for k, v in sorted(by_claim.items())},
        "by_owner": {k: sorted(v) for k, v in sorted(by_owner.items())},
        "summary": r,
    }
    base = Path(out) if out else Path(os.environ.get("MONGOOSE_DATA_DIR") or "~/.mongoose/data").expanduser() / "hardening_packets" / ("code-reingest-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    base.mkdir(parents=True, exist_ok=True)
    (base / "code_reingest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Mongoose Code Reingest", "", f"Generated: {payload['generated_at']}", "", f"Modules indexed: {r['modules']}", "", "## Claims → modules"]
    for claim, mods in payload["by_claim"].items():
        lines.append(f"- `{claim}`: {', '.join(mods[:12])}{' ...' if len(mods) > 12 else ''}")
    lines += ["", "## Owners → modules"]
    for owner, mods in payload["by_owner"].items():
        lines.append(f"- **{owner}**: {', '.join(mods[:12])}{' ...' if len(mods) > 12 else ''}")
    (base / "code_reingest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _record_run("code_reingest", "ok", f"Indexed {r['modules']} modules into {base}", {"dir": str(base), **r})
    return {"dir": str(base), "json_path": str(base / "code_reingest.json"), "md_path": str(base / "code_reingest.md"), **r}


def _related_modules_for_claim(claim_key: str, limit: int = 20) -> list[dict[str, Any]]:
    if not claim_key:
        return []
    rows = module_index(500)
    return [m for m in rows if claim_key in (m.get("claims") or [])][:limit]


def create_reviewer_packet(
    path: str | Path | None = None,
    *,
    observation_id: str | None = None,
    reviewer: str = "claude",
    include_module_index: bool = True,
) -> dict[str, Any]:
    """Create a review packet for Claude/OpenAI/local reviewers.

    This only writes files. It does not call an API unless a later explicit submit
    command is added. The packet tells reviewers to propose tests/diagnosis/patches,
    not to become the source of truth.
    """
    _ensure_tables()
    obs: dict[str, Any] | None = None
    if observation_id:
        with connect() as con:
            row = con.execute("SELECT * FROM self_harden_observations WHERE id = ?", (observation_id,)).fetchone()
        if row:
            obs = dict(row)
            obs["evidence"] = _read_json(obs.pop("evidence_json", "{}"), {})
    base = Path(path) if path else Path(os.environ.get("MONGOOSE_DATA_DIR") or "~/.mongoose/data").expanduser() / "hardening_packets" / ("review-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    base.mkdir(parents=True, exist_ok=True)
    claim_key = (obs or {}).get("claim_key", "")
    related = _related_modules_for_claim(claim_key) if include_module_index else []
    payload = {
        "generated_at": _now(),
        "reviewer": reviewer,
        "observation": obs,
        "claim": next((c for c in claims() if c["key"] == claim_key), None),
        "related_modules": related,
        "instructions": {
            "role": "Independent hardening reviewer",
            "task": "Identify the hidden state that could falsify the user-visible claim, propose a failing regression test first, then a minimal patch. Do not weaken local-first, action gates, or receipt truth.",
            "deliverables": ["diagnosis", "failing test", "minimal patch plan", "gates to run", "risks and Mac-only checks"],
        },
    }
    (base / "review_packet.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# Mongoose Hardening Review Packet",
        "",
        f"Reviewer target: {reviewer}",
        f"Generated: {payload['generated_at']}",
        "",
        "## Instruction",
        "You are reviewing a Mongoose hardening observation. Start from the visible promise, not the feature. Find the hidden state that could make it false. Propose a failing test before any patch.",
        "",
    ]
    if obs:
        lines += ["## Observation", f"- id: `{obs['id']}`", f"- severity: **{obs['severity']}**", f"- claim: `{claim_key or 'unmapped'}`", f"- summary: {obs['summary']}", f"- scope: {obs['scope']}", ""]
    if payload.get("claim"):
        c = payload["claim"]
        lines += ["## Claim/invariant", f"Promise: {c['promise']}", f"Authority: {c['authority']}", f"Invariant: {c['invariant']}", ""]
    if related:
        lines += ["## Related modules"]
        for m in related[:20]:
            tests = m.get("tests") if isinstance(m.get("tests"), list) else []
            lines.append(f"- `{m['name']}` owner={m.get('owner',{}).get('area','?')} tests={', '.join(tests[:4])}")
    lines += ["", "## Required response format", "1. Claim failure in one sentence", "2. Hidden falsifying state", "3. Minimal failing test", "4. Minimal patch", "5. Gates/Mac-only validation"]
    md_path = base / "review_packet.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    with connect() as con:
        con.execute(
            "INSERT INTO self_harden_review_packets(id, reviewer, observation_id, packet_dir, status, payload_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (rid, reviewer, observation_id or "", str(base), "created", _json({"md_path": str(md_path), "json_path": str(base / "review_packet.json")}), _now()),
        )
        con.commit()
    _record_run("review_packet", "ok", f"Created reviewer packet for {reviewer}", {"dir": str(base), "observation_id": observation_id})
    return {"id": rid, "dir": str(base), "md_path": str(md_path), "json_path": str(base / "review_packet.json"), "related_modules": len(related)}

def create_hardening_plan(observation_id: str | None = None, *, title: str = "") -> dict[str, Any]:
    """Create an approval-gated builder plan that reproduces and fixes one hardening observation.

    It is intentionally generic: first write a failing test, then patch, then run the relevant gate.
    It does not auto-approve or run.
    """
    _ensure_tables()
    obs: dict[str, Any] | None = None
    if observation_id:
        with connect() as con:
            row = con.execute("SELECT * FROM self_harden_observations WHERE id = ?", (observation_id,)).fetchone()
        if row:
            obs = dict(row)
            obs["evidence"] = _read_json(obs.pop("evidence_json", "{}"), {})
    subject = title or (obs["summary"] if obs else "self-harden claim-integrity candidate")
    evidence = json.dumps(obs or {}, indent=2, ensure_ascii=False)
    steps = [
        {
            "id": "01-reproduce",
            "title": "Reproduce the claim failure",
            "task": "Add a focused failing regression test for this hardening observation. Do not patch product code yet. Observation/evidence:\n" + evidence,
            "verify": {"type": "pytest"},
            "retries": 1,
        },
        {
            "id": "02-patch",
            "title": "Patch the smallest safe seam",
            "task": "Patch the minimal product seam so the new regression passes without weakening security/local-first/action-gate guards. Keep the visible claim and the authoritative state aligned.",
            "verify": {"type": "pytest"},
            "depends_on": ["01-reproduce"],
            "retries": 2,
        },
        {
            "id": "03-gate-and-report",
            "title": "Run relevant gates and write audit note",
            "task": "Run the relevant targeted tests, compileall and pyflakes. Write a short audit note explaining the claim, falsifying hidden state, fix, tests, and any Mac-only validation still needed.",
            "verify": {"type": "pytest"},
            "depends_on": ["02-patch"],
            "retries": 1,
        },
    ]
    from .builder import create_plan
    plan = create_plan("self-harden-" + re.sub(r"[^a-z0-9]+", "-", subject.lower())[:40].strip("-"), steps)
    _record_run("build_plan", "ok", "Created self-harden build plan", {"plan_id": plan.get("plan_id"), "observation_id": observation_id})
    return plan


async def scheduler_scan() -> dict[str, Any]:
    return scan_runtime()


def _packet_payload_from_id_or_path(packet: str) -> tuple[dict[str, Any], Path | None, str]:
    _ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM self_harden_review_packets WHERE id = ?", (packet,)).fetchone()
    if row:
        payload_meta = _read_json(row["payload_json"], {})
        json_path = Path(payload_meta.get("json_path", "")) if payload_meta.get("json_path") else None
        if json_path and json_path.exists():
            return json.loads(json_path.read_text(encoding="utf-8")), json_path, row["id"]
        return {"packet_meta": dict(row)}, json_path, row["id"]
    path = Path(packet).expanduser()
    if path.is_dir():
        path = path / "review_packet.json"
    if not path.exists():
        raise FileNotFoundError(f"review packet not found: {packet}")
    return json.loads(path.read_text(encoding="utf-8")), path, "manual-" + uuid.uuid4().hex[:6]


def review_submission_plan(packet: str, *, reviewer: str = "claude") -> dict[str, Any]:
    """Describe what would leave the machine if a review packet were submitted.

    This is the explicit privacy/cost prompt layer. It does not call any API.
    """
    payload, json_path, packet_id = _packet_payload_from_id_or_path(packet)
    raw = json.dumps(payload, ensure_ascii=False)
    lower = raw.lower()
    risk_terms = [t for t in ["token", "secret", "password", "authorization", "cookie", "private key", "api_key", "apikey"] if t in lower]
    reviewer = (reviewer or payload.get("reviewer") or "claude").lower()
    endpoint = "manual upload"
    if reviewer == "openai":
        endpoint = "OpenAI API (only if explicitly allowed)"
    elif reviewer in ("claude", "anthropic"):
        endpoint = "Anthropic API / Claude upload (only if explicitly allowed)"
    return {
        "packet_id": packet_id,
        "packet_path": str(json_path) if json_path else "",
        "reviewer": reviewer,
        "chars": len(raw),
        "estimated_tokens": max(1, len(raw) // 4),
        "risk_terms": risk_terms,
        "endpoint": endpoint,
        "will_send_now": False,
        "requires_explicit_consent": True,
        "summary": "This packet is prepared for review but will not leave the machine unless you manually upload it or explicitly enable an external submission path.",
        "recommended_next_step": "Open the markdown packet and upload it manually, or keep it local and use a local reviewer.",
    }


def submit_review_packet(packet: str, *, reviewer: str = "claude", allow_external: bool = False, yes: bool = False) -> dict[str, Any]:
    """Gate review submission behind explicit consent.

    v0.55 deliberately stops short of automatic network submission. The function records
    the consent decision and produces a clear plan; external API calls can be added later
    behind the same gate once provider-specific redaction/cost controls are built.
    """
    plan = review_submission_plan(packet, reviewer=reviewer)
    status = "blocked_needs_explicit_consent"
    if allow_external and yes:
        status = "ready_for_manual_external_submission"
    rid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    with connect() as con:
        con.execute(
            "INSERT INTO self_harden_runs(id, kind, status, summary, payload_json, created_at) VALUES(?,?,?,?,?,?)",
            (rid, "review_submit", status, f"Review submission gate for {plan['reviewer']}", _json(plan), _now()),
        )
        con.commit()
    return {"ok": status != "blocked_needs_explicit_consent", "status": status, "plan": plan}


def list_review_packets(limit: int = 20) -> list[dict[str, Any]]:
    """List generated reviewer packets. Local metadata only; no external calls."""
    _ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM self_harden_review_packets ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        payload = _read_json(d.pop("payload_json", "{}"), {})
        d.update(payload)
        out.append(d)
    return out
