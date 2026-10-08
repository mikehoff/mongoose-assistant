"""One place that handles uploaded files for both the quick chat-attachment lane and the large-file
staging lane, so the browser and the phone behave identically.

Two tiers, by design:
  - CHAT ATTACHMENT (<= 30MB): a file dropped on the chat composer. Small enough to handle inline with
    the turn. CSVs that look like body data or RTM data are imported straight away; everything else is
    stored as a referenceable asset and summarised so the chat turn can mention it.
  - STAGING (<= 200MB): a separate heavyweight lane for big files (build packages, large archives,
    bulky exports). Stored as an asset and classified (available for self-update / RTM / reference),
    listed in the staging area, never silently applied.

Both lanes funnel through the same asset store (content-addressed, idempotent) and the same body-data
router, so there is exactly one code path that decides what an uploaded file *is*.
"""
from __future__ import annotations

from typing import Any

# Tier limits. The chat lane is deliberately small so an attachment is a quick thing; the staging lane
# is for the genuinely large file the chat lane should not carry.
CHAT_ATTACHMENT_MAX = 30 * 1024 * 1024     # 30 MB
STAGING_MAX = 200 * 1024 * 1024            # 200 MB


def _looks_like_text_csv(filename: str, data: bytes) -> bool:
    if not (filename or "").lower().endswith(".csv"):
        return False
    # cheap binary sniff: CSVs are text
    sample = data[:4096]
    return b"\x00" not in sample


def _try_body_data(filename: str, data: bytes) -> dict[str, Any] | None:
    """If the file is a body-data CSV (Alpha/MFP), import it and return the result; else None."""
    if not _looks_like_text_csv(filename, data):
        return None
    try:
        text = data.decode("utf-8", errors="replace")
        from .body_data_nudge import _sniff_kind, ingest_csv
        if _sniff_kind(filename, text) is not None:
            res = dict(ingest_csv(filename, text) or {})
            res["lane"] = "body_data"
            return res
    except Exception:
        return None
    return None


_KNOWLEDGE_NAME_HINTS = ("dump", "thread", "knowledge", "mongoose")
_KNOWLEDGE_SIDECARS = ("canonical_facts", "preferences_constraints", "uncertainties",
                       "structured_records", "metadata.yaml", "retrieval_strategy",
                       "body_posture_wellness", "implementation_implications")


def _looks_like_knowledge_material(filename: str, data: bytes) -> bool:
    """Conservative gate for tolerant extraction: fire on dump-like names or dump-signal zip members,
    but not on arbitrary uploaded markdown or unrelated zips."""
    import io
    import zipfile
    name = (filename or "").lower()
    name_hint = any(h in name for h in _KNOWLEDGE_NAME_HINTS)
    if name.endswith((".md", ".txt", ".markdown", ".text")):
        return name_hint  # do not LLM-extract every stray markdown file
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return False
    if name_hint:
        return True
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = " ".join(n.lower() for n in z.namelist())
    except Exception:
        return False
    return ".jsonl" in members or any(s in members for s in _KNOWLEDGE_SIDECARS)


def handle_chat_attachment(filename: str, data: bytes, *, uploaded_by: str | None = None,
                           surface: str | None = None, note: str | None = None,
                           mime_type: str | None = None) -> dict[str, Any]:
    """Handle a file attached to a chat message (<= 30MB).

    Body-data CSVs are imported. Everything else is stored as an asset and summarised so the chat turn
    can reference it ('I've saved <name> (<size>); ask me about it or find it in Files & staging').
    """
    if not data:
        return {"ok": False, "error": "empty file"}
    if len(data) > CHAT_ATTACHMENT_MAX:
        mb = len(data) / (1024 * 1024)
        return {"ok": False, "error": (f"that file is {mb:.0f}MB. Chat attachments are capped at 30MB. "
                                       "use Files & staging for large files (up to 200MB)."),
                "too_large_for_chat": True, "size_bytes": len(data)}
    bd = _try_body_data(filename, data)
    if bd is not None:
        return bd
    # Structured thread dumps are not generic files: ingest them into the provenance-preserving
    # knowledge layer so they become claims, review items, memory candidates and insight candidates
    # rather than a dead archive. handle_possible_import returns None for anything that isn't a dump.
    try:
        from ..knowledge_import import handle_possible_import as _handle_knowledge_import
        ki = _handle_knowledge_import(filename, data)
        if ki is not None:
            return ki
    except Exception as exc:
        return {"ok": False, "lane": "knowledge_import", "error": f"knowledge import failed: {exc}"}
    # Tolerant fallback: dumps that are not in the strict shape (prose-only, renamed/nested JSONL, YAML
    # sidecars, recursive archives). Gated so unrelated zips or stray markdown are NOT turned into records.
    # import_bulk_dump_bytes routes an outer archive through the recursive orchestrator and a single
    # non-standard dump through the legacy normalizer; both flow the same safety pipeline.
    if _looks_like_knowledge_material(filename, data):
        try:
            from ..knowledge_import import import_bulk_dump_bytes
            rep = import_bulk_dump_bytes(data, source_name=filename)
            ex = rep.model_dump() if hasattr(rep, "model_dump") else dict(rep)
            if (ex.get("records_imported") or 0) > 0:
                ex.setdefault("lane", "knowledge_import")
                return ex
        except Exception as exc:
            return {"ok": False, "lane": "knowledge_import", "error": f"tolerant extraction failed: {exc}"}
    # generic: store as a referenceable asset
    from . import assets as _assets
    rec = _assets.store_asset(data, filename, uploaded_by=uploaded_by,
                              source_surface=surface or "chat", mime_type=mime_type, note=note)
    if not rec.get("ok"):
        return rec
    size_mb = (rec.get("size_bytes") or len(data)) / (1024 * 1024)
    rec["lane"] = "asset"
    rec["summary"] = f"Saved {rec.get('filename', filename)} ({size_mb:.1f}MB). It's available for reference."
    return rec


def handle_staged_upload(filename: str, data: bytes, *, uploaded_by: str | None = None,
                         surface: str | None = None, note: str | None = None,
                         mime_type: str | None = None) -> dict[str, Any]:
    """Handle a large file in the staging lane (<= 200MB). Stored + classified, never auto-applied."""
    if not data:
        return {"ok": False, "error": "empty file"}
    if len(data) > STAGING_MAX:
        mb = len(data) / (1024 * 1024)
        return {"ok": False, "error": f"that file is {mb:.0f}MB; the staging limit is 200MB.",
                "size_bytes": len(data)}
    from . import assets as _assets
    rec = _assets.store_asset(data, filename, uploaded_by=uploaded_by,
                              source_surface=surface or "staging", mime_type=mime_type, note=note)
    if not rec.get("ok"):
        return rec
    rec["lane"] = "staged"
    # a friendly next-step, mirroring the existing phone-upload hints
    if rec.get("available_for_self_update"):
        rec["next"] = ("This is a valid Mongoose build. Apply it from the Build tab or with "
                       "`mongoose update --stage-asset " + rec["asset_id"] + "`.")
    elif rec.get("available_for_rtm"):
        rec["next"] = "Saved and available for RTM enrichment and reference."
    else:
        rec["next"] = "Saved to staging and available for reference."
    return rec


def list_staged(limit: int = 50) -> dict[str, Any]:
    """The staging area contents: assets uploaded for staging/large-file handling, newest first."""
    from . import assets as _assets
    try:
        items = _assets.list_assets(limit=limit)
    except Exception:
        items = []
    out = []
    for a in items:
        size = a.get("size_bytes") or 0
        out.append({
            "asset_id": a.get("asset_id"),
            "filename": a.get("filename"),
            "kind": a.get("kind"),
            "size_mb": round(size / (1024 * 1024), 2),
            "uploaded_at": a.get("created_at") or a.get("uploaded_at"),
            "available_for_self_update": bool(a.get("available_for_self_update")),
            "available_for_rtm": bool(a.get("available_for_rtm")),
            "surface": a.get("source_surface"),
        })
    return {"ok": True, "staged": out}
