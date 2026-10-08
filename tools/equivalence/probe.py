"""Equivalence probe: run inside ONE Mongoose codebase against a sandboxed copy of a brain and print a
JSON fingerprint of everything an install's identity and data must reproduce.

Run by tools/equivalence/check.py, once with the current live code and once with the release code plus
the site profile. Never run it against a real brain: check.py gives each side its own copy, a throwaway
HOME (no .env, no credentials) and a dead network.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

OUT: dict[str, Any] = {}
ERRORS: dict[str, str] = {}


def probe(name):
    def deco(fn):
        try:
            OUT[name] = fn()
        except Exception as exc:  # recorded, so both sides failing the same way still compares equal
            ERRORS[name] = f"{type(exc).__name__}: {str(exc)[:300]}"
            OUT[name] = {"error": ERRORS[name]}
        return fn
    return deco


def _norm(v: Any) -> Any:
    """Drop volatile fields (timestamps, tokens, temp paths) so only meaningful differences remain."""
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items())
                if k not in {"generated_at", "at", "now", "created_at", "updated_at", "token", "download_url",
                             "path", "docx_path", "pdf_path", "elapsed_ms", "cached_at", "as_of"}}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str):
        v = re.sub(r"/(private/)?tmp/[^\s\"']+", "<tmp>", v)
        v = re.sub(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?", "<ts>", v)
    return v


from mongoose.services import rtm_management as M  # noqa: E402


@probe("flats")
def _flats():
    return sorted(({k: f.get(k) for k in ("flat_id", "leaseholder_name", "payment_reference", "greeting_name",
                                           "leaseholder_email", "excluded")} for f in M.list_flats(include_excluded=True)),
                  key=lambda f: f["flat_id"])


@probe("statements")
def _statements():
    return {f["flat_id"]: _norm(M.flat_statement(f["flat_id"])) for f in M.list_flats(include_excluded=True)}


@probe("bank_matching")
def _bank_matching():
    from mongoose.db import connect
    with connect() as con:
        rows = con.execute("SELECT reference, description, amount_pence FROM rtm_mgmt_bank_transactions "
                           "WHERE direction IN ('in', 'credit') ORDER BY bank_date, id").fetchall()
    out = []
    for r in rows:
        m = M._match_bank_credit(r["reference"] or "", r["description"] or "", int(r["amount_pence"] or 0))
        out.append({k: m.get(k) for k in ("match_status", "flat_id", "document_number")})
    return {"count": len(out), "digest": hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest(),
            "statuses": sorted({json.dumps(o, sort_keys=True) for o in out})[:400]}


@probe("flat_reference_forms")
def _flat_reference_forms():
    # Every way a person types a flat, resolved by the chat/document parsers of this codebase.
    from mongoose.services import rtm_document_chat as DC, chat_actions as CA
    forms = []
    for f in M.list_flats(include_excluded=True):
        fid = f["flat_id"]
        m = re.fullmatch(r"(\d+)([A-Z]+)F(\d+)", fid)
        if not m:
            continue
        num, letters, n = m.groups()
        forms += [fid, fid.lower(), f"{num}{letters} F{n}", f"{num} {letters.lower()} f {n}", f"flat {n} {num} {letters}",
                  f"Flat {n}, {num}{letters}"]
    res = {}
    for s in forms:
        a = getattr(DC, "_parse_flat", lambda x: None)(s)
        b = None
        rx = CA._flat_re() if hasattr(CA, "_flat_re") else getattr(CA, "_FLAT_RE", None)
        if rx is not None:
            mm = rx.search(s)
            b = mm.group(0) if mm else None
        res[s] = [a, b]
    return res


@probe("dashboard")
def _dashboard():
    return _norm(M.management_dashboard())


@probe("collection_and_funds")
def _funds():
    return {"collection": _norm(M.collection_position()), "funds": _norm(M.fund_position())}


@probe("correspondence")
def _cc():
    return {f["flat_id"]: M.correspondence_cc(f["flat_id"]) for f in M.list_flats(include_excluded=True)}


@probe("portal_snapshot")
def _snapshot():
    from mongoose.services import rtm_portal as P
    snap = P.build_portal_snapshot(full=True)
    return {"digest": hashlib.sha256(json.dumps(_norm(snap), sort_keys=True, default=str).encode()).hexdigest(),
            "body": _norm(snap)}


@probe("documents")
def _documents():
    # Render identity-bearing documents for real (non-excluded) flats and compare their TEXT: the letterhead,
    # company identity, block address, cover emails and a demand rendered from a real demand row.
    import zipfile
    from mongoose.services import rtm_templates as T
    from mongoose.services.rtm_apportionment_docs import render_demand_docx
    from mongoose.db import connect

    def docx_text(p):
        if not p or not Path(p).exists() or not str(p).endswith(".docx"):
            return ""
        with zipfile.ZipFile(p) as z:
            xml = " ".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist()
                           if n.startswith("word/") and n.endswith(".xml"))
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml)).strip()

    flats = [f for f in M.list_flats() if not f.get("excluded")][:3]
    out = {}
    for f in flats:
        fid = f["flat_id"]
        for tpl in ("branded_letter", "welcome"):
            res = T.generate_document(tpl, flat_id=fid, letter_date="1 October 2026",
                                      leaseholder_name=f.get("leaseholder_name") or "Resident",
                                      address=f.get("correspondence_address") or f"Flat {fid}",
                                      recipient_email=f.get("leaseholder_email") or "")
            out[f"{tpl}:{fid}"] = {"ok": res.get("ok"), "reason": res.get("reason"),
                                   "text": _norm(docx_text(res.get("docx_path") or res.get("path")))}
        for key, spec in sorted(getattr(T, "EMAIL_TEMPLATES", {}).items()):
            ctx = {k: "X" for k in spec.get("fields", [])}
            ctx.update(recipient_email=f.get("leaseholder_email") or "x@example.com", flat_number=fid[-1],
                       first_name=(f.get("greeting_name") or "Resident"), amount_received="£100.00",
                       received_date="1 October 2026")
            res = T.render_email(key, ctx)
            out[f"email:{key}:{fid}"] = _norm({k: res.get(k) for k in ("ok", "text", "subject", "missing")})
    with connect() as con:
        recs = con.execute("SELECT flat_id, receipt_no FROM portal_receipt_pdfs ORDER BY flat_id, receipt_no LIMIT 2").fetchall()
        dem = con.execute("SELECT d.flat_id, d.document_number FROM rtm_mgmt_demands d JOIN rtm_mgmt_flats f "
                          "ON f.flat_id=d.flat_id WHERE COALESCE(f.excluded,0)=0 ORDER BY d.flat_id LIMIT 2").fetchall()
    for r in recs:
        res = T.generate_document("cover_email_receipt", flat_id=r["flat_id"])
        out[f"cover_email_receipt:{r['flat_id']}"] = _norm({k: res.get(k) for k in ("ok", "reason", "subject", "body", "text", "to", "cc")})
    for r in dem:
        tmp = Path.cwd() / "tmp" / f"demand-{r['flat_id']}.docx"
        res = render_demand_docx(flat_id=r["flat_id"], year="2027", period_label="1 July 2026 to 31 December 2026",
                                 demand_date="1 June 2026", due_date="1 July 2026", doc_number=r["document_number"],
                                 out_path=str(tmp))
        out[f"demand:{r['flat_id']}"] = {"ok": res.get("ok"), "text": _norm(docx_text(str(tmp)))}
    return out


@probe("search")
def _search():
    from mongoose.services.find import find
    qs = ["service charge", "demand", "insurance", "lift", "receipt", "voicemail", "minutes", "budget"]
    qs += [f["flat_id"] for f in M.list_flats()[:4]]
    out = {}
    for q in qs:
        hits = find(q, limit=10)
        hits = hits.get("results", hits) if isinstance(hits, dict) else hits
        out[q] = [{k: h.get(k) for k in ("kind", "title", "id", "source_id", "filename", "subject")} for h in (hits or [])]
    return out


@probe("identity")
def _identity():
    # Per-install identity as each codebase computes it.
    from mongoose import settings as S
    s = S.get_settings()
    out = {"owner_id": getattr(s, "owner_id", None), "home": [getattr(s, "home_lat", None), getattr(s, "home_lon", None)],
           "site_domain": (S.site_domain() if hasattr(S, "site_domain") else None)}
    try:
        from mongoose.services import mailbox as MB
        out["owner_display_name"] = MB.name_for(MB.OWNER)
    except Exception as exc:
        out["owner_display_name"] = f"error: {exc}"
    # The company-profile module's name differs between installs; find it by what it provides.
    import importlib
    import pkgutil
    import mongoose.services as _svc
    RC = None
    for m in pkgutil.iter_modules(_svc.__path__):
        if m.name.startswith("rtm_") and m.name not in ("rtm_management", "rtm_portal", "rtm_trust"):
            mod = importlib.import_module(f"mongoose.services.{m.name}")
            if hasattr(mod, "company_profile") and hasattr(mod, "load_property"):
                RC = mod
                break
    try:
        out["company"] = _norm(RC.company_profile())
    except Exception as exc:
        out["company"] = f"error: {exc}"
    return out


@probe("http_routes")
def _routes():
    # The full HTTP surface (methods + path) of the app, as anything that lists app.routes sees it.
    import mongoose.app as A
    out = []
    for r in A.app.routes:
        path = getattr(r, "path", None)
        out.append(f"{','.join(sorted(getattr(r, 'methods', None) or [])) or '-'} {path if path else type(r).__name__}")
    return sorted(out)


@probe("introspection")
def _introspection():
    from mongoose.services import introspection as I
    routes = sorted(f"{','.join(sorted(r.get('methods') or []))} {r.get('path')}" for r in I.collect_routes())
    return {"routes": routes, "intents": sorted(I.collect_intents()), "jobs": sorted(I.collect_scheduler_jobs())}


if __name__ == "__main__":
    try:
        json.dump({"results": OUT, "errors": ERRORS}, sys.stdout, sort_keys=True, default=str, indent=1)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
