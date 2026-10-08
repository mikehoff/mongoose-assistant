from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..db import utc_now_iso
from .package_detector import KnowledgeImportError, looks_like_thread_dump_zip, parse_package_bytes, _unsafe_zip_name
from .legacy_normalizer import normalise_legacy_package_bytes
from .schemas import ImportReport, ThreadMetadata, make_id
from .store import ensure_tables, insert_archive_item, insert_import, insert_raw_file

MAX_RECURSION_DEPTH = 4
MAX_CHILD_ARCHIVES = 200
MAX_CHILD_BYTES = 90 * 1024 * 1024


@dataclass
class ArchiveChildResult:
    path: str
    item_type: str
    status: str
    message: str = ""
    import_id: str | None = None
    records: int = 0
    claims: int = 0
    review: int = 0
    raw_files: int = 0
    memories: int = 0
    persona_rules: int = 0
    nudges: int = 0
    insights: int = 0
    safety_rules: int = 0


@dataclass
class ArchiveRun:
    import_id: str
    source_name: str
    source_sha256: str
    children: list[ArchiveChildResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    child_archives_seen: int = 0
    code_builds_archived: int = 0
    strict_dumps_imported: int = 0
    legacy_dumps_imported: int = 0
    markdown_dumps_normalised: int = 0
    jsonl_dumps_normalised: int = 0
    yaml_dumps_normalised: int = 0


def looks_like_mongoose_archive(data: bytes, filename: str = "") -> bool:
    if not data or not zipfile.is_zipfile(io.BytesIO(data)):
        return False
    if looks_like_thread_dump_zip(data, filename):
        return True
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
    except Exception:
        return False
    low = "\n".join(n.lower() for n in names[:400])
    return any(n.lower().endswith(".zip") for n in names) or "mongoose thread knowledge dump" in low or "structured_records.jsonl" in low or "src/mongoose/" in low


def import_archive(path: str | Path, *, apply_accepted_memory: bool = True,
                   activate_low_risk: bool = True) -> ImportReport:
    p = Path(path)
    return import_archive_bytes(p.read_bytes(), source_name=p.name, apply_accepted_memory=apply_accepted_memory,
                                activate_low_risk=activate_low_risk)


def import_archive_bytes(data: bytes, *, source_name: str = "archive.zip",
                         apply_accepted_memory: bool = True,
                         activate_low_risk: bool = True) -> ImportReport:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise KnowledgeImportError("archive import expects a zip")
    sha = hashlib.sha256(data).hexdigest()
    root_id = make_id("imp", "knowledge_archive_bundle", source_name, sha)
    ensure_tables()
    metadata = ThreadMetadata(
        dump_version="archive-orchestrated-v1",
        thread_title=f"Knowledge archive bundle: {source_name}",
        thread_date_range="mixed",
        extracted_at=utc_now_iso(),
        primary_domains=["mixed_archive"],
        sensitivity_level="mixed",
        overall_confidence="mixed",
        source_quality_notes=["Recursive archive import: child packages classified and normalised before graph integration."],
    )
    insert_import(root_id, package_type="knowledge_archive_bundle", source_name=source_name,
                  source_sha256=sha, metadata=metadata)
    insert_raw_file(make_id("raw", root_id, source_name, sha), root_id, source_name, sha, len(data), None)
    run = ArchiveRun(root_id, source_name, sha)
    _walk_zip(data, source_name=source_name, run=run, depth=0, apply_accepted_memory=apply_accepted_memory)

    # After all children land, run the automatic care/research loops once for the whole bundle. This is
    # the delightful layer: low-risk, user-stated care ideas become active, while sensitive/current items
    # are queued for research/review rather than left inert.
    auto_notes: list[str] = []
    try:
        from .entity_fusion import run_entity_fusion
        fus = run_entity_fusion()
        auto_notes.append(f"archive entity fusion created {fus.get('fusions', 0)} canonical links")
    except Exception as exc:
        run.warnings.append(f"archive entity fusion skipped: {exc}")
    try:
        from .entity_suggestions import suggest_entity_fusions
        sug = suggest_entity_fusions(limit=120, use_llm=False)
        auto_notes.append(f"archive queued {sug.get('created', 0)} reviewable fusion suggestions")
    except Exception as exc:
        run.warnings.append(f"archive fusion suggestions skipped: {exc}")
    try:
        from .synthesis import run_synthesis_cycle
        syn = run_synthesis_cycle(use_llm=False, limit=80)
        auto_notes.append(f"archive synthesis inserted {syn.get('inserted', 0)} cards")
    except Exception as exc:
        run.warnings.append(f"archive synthesis skipped: {exc}")
    try:
        from .reflection import run_reflection_cycle
        refl = run_reflection_cycle("archive_import", queue_research=True,
                                    activate_low_risk=bool(activate_low_risk), limit=40)
        auto_notes.append(
            f"archive reflection queued {len(refl.get('research_queued') or [])} research requests and activated {len(refl.get('low_risk_activated') or [])} low-risk care plans"
        )
    except Exception as exc:
        run.warnings.append(f"archive reflection skipped: {exc}")

    imported_children = [c for c in run.children if c.status == "imported"]
    records_seen = sum(c.records for c in imported_children)
    claims_created = sum(c.claims for c in imported_children)
    raw_preserved = sum(c.raw_files for c in imported_children) + 1
    review_items = sum(c.review for c in imported_children)
    memories_created = sum(c.memories for c in imported_children)
    persona_rules_created = sum(c.persona_rules for c in imported_children)
    nudges_created = sum(c.nudges for c in imported_children)
    insights_created = sum(c.insights for c in imported_children)
    safety_rules_created = sum(c.safety_rules for c in imported_children)
    warnings = [
        f"archive children seen: {run.child_archives_seen}",
        f"strict dumps imported: {run.strict_dumps_imported}",
        f"legacy dumps imported: {run.legacy_dumps_imported}",
        f"code/build artifacts archived separately: {run.code_builds_archived}",
        *auto_notes,
        *run.warnings[:80],
    ]
    return ImportReport(
        ok=not run.errors,
        import_id=root_id,
        package_type="knowledge_archive_bundle",
        thread_title=metadata.thread_title,
        records_seen=records_seen,
        records_imported=records_seen,
        records_needing_review=review_items,
        claims_created=claims_created,
        canonical_memories_created=memories_created,
        persona_rules_created=persona_rules_created,
        scheduler_candidates_created=nudges_created,
        insight_candidates_created=insights_created,
        safety_rules_created=safety_rules_created,
        raw_files_preserved=raw_preserved,
        errors=run.errors[:80],
        warnings=warnings,
    )


def _walk_zip(data: bytes, *, source_name: str, run: ArchiveRun, depth: int,
              apply_accepted_memory: bool) -> None:
    if depth > MAX_RECURSION_DEPTH:
        run.warnings.append(f"{source_name}: recursion limit reached")
        return
    if run.child_archives_seen >= MAX_CHILD_ARCHIVES:
        run.warnings.append("child archive limit reached")
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = [n for n in z.namelist() if not n.endswith("/")]
            # First, if this zip itself is a strict or legacy dump, import it as one unit.
            if depth > 0:
                imported = _try_import_child(data, path=source_name, run=run, apply_accepted_memory=apply_accepted_memory)
                if imported:
                    return
            # Otherwise treat it as a bundle and recurse/import member files.
            for name in members:
                if _unsafe_zip_name(name):
                    run.errors.append(f"unsafe member skipped: {name}")
                    continue
                low = name.lower()
                try:
                    body = z.read(name)
                except Exception as exc:
                    run.errors.append(f"{name}: read failed: {exc}")
                    continue
                if len(body) > MAX_CHILD_BYTES:
                    _record_child(run, name, "oversize", "skipped", f"member too large: {len(body)} bytes")
                    continue
                if low.endswith(".zip") and zipfile.is_zipfile(io.BytesIO(body)):
                    run.child_archives_seen += 1
                    if _is_mongoose_build_zip(body):
                        _archive_build_artifact(run, name, body)
                    else:
                        _walk_zip(body, source_name=name, run=run, depth=depth + 1,
                                  apply_accepted_memory=apply_accepted_memory)
                elif low.endswith((".md", ".txt", ".jsonl", ".yaml", ".yml")):
                    # A single loose dump file can still be normalised by wrapping it as a synthetic zip.
                    if _looks_like_dump_text(body, name):
                        synthetic = _zip_single(name, body)
                        run.child_archives_seen += 1
                        _try_import_child(synthetic, path=name, run=run,
                                          apply_accepted_memory=apply_accepted_memory)
                else:
                    continue
    except zipfile.BadZipFile as exc:
        run.errors.append(f"{source_name}: bad zip: {exc}")


def _try_import_child(data: bytes, *, path: str, run: ArchiveRun, apply_accepted_memory: bool) -> bool:
    from .service import _import_parsed  # local import avoids module-cycle at import time
    try:
        if looks_like_thread_dump_zip(data, path):
            parsed = parse_package_bytes(data, source_name=path)
            report = _import_parsed(parsed, apply_accepted_memory=apply_accepted_memory, run_post_import=False)
            run.strict_dumps_imported += 1
            _record_report(run, path, "strict_thread_dump", report, "imported")
            return True
        legacy = normalise_legacy_package_bytes(data, source_name=path)
        report = _import_parsed(legacy.parsed, apply_accepted_memory=apply_accepted_memory, run_post_import=False)
        run.legacy_dumps_imported += 1
        if legacy.extraction_method.startswith("markdown"):
            run.markdown_dumps_normalised += 1
        elif "jsonl" in legacy.extraction_method:
            run.jsonl_dumps_normalised += 1
        elif "yaml" in legacy.extraction_method:
            run.yaml_dumps_normalised += 1
        _record_report(run, path, f"legacy_{legacy.extraction_method}", report, "imported")
        return True
    except KnowledgeImportError as exc:
        _record_child(run, path, "non_dump_zip", "skipped", str(exc))
        return False
    except Exception as exc:
        _record_child(run, path, "import_error", "error", str(exc))
        run.errors.append(f"{path}: {exc}")
        return False


def _record_report(run: ArchiveRun, path: str, item_type: str, report: ImportReport, status: str) -> None:
    child = ArchiveChildResult(
        path=path,
        item_type=item_type,
        status=status,
        message="ok" if report.ok else "; ".join(report.errors[:2]),
        import_id=report.import_id,
        records=report.records_imported,
        claims=report.claims_created,
        review=report.records_needing_review,
        raw_files=report.raw_files_preserved,
        memories=report.canonical_memories_created,
        persona_rules=report.persona_rules_created,
        nudges=report.scheduler_candidates_created,
        insights=report.insight_candidates_created,
        safety_rules=report.safety_rules_created,
    )
    run.children.append(child)
    insert_archive_item(run.import_id, child.__dict__)
    if report.warnings:
        run.warnings.extend(f"{path}: {w}" for w in report.warnings[:6])
    if report.errors:
        run.errors.extend(f"{path}: {e}" for e in report.errors[:3])


def _record_child(run: ArchiveRun, path: str, item_type: str, status: str, message: str) -> None:
    child = ArchiveChildResult(path=path, item_type=item_type, status=status, message=message)
    run.children.append(child)
    insert_archive_item(run.import_id, child.__dict__)


def _archive_build_artifact(run: ArchiveRun, path: str, data: bytes) -> None:
    sha = hashlib.sha256(data).hexdigest()
    imp_id = make_id("imp", "mongoose_build_artifact", path, sha)
    meta = ThreadMetadata(
        dump_version="build-artifact-v1",
        thread_title=f"Mongoose build artifact: {Path(path).name}",
        primary_domains=["project", "build_artifact"],
        sensitivity_level="low",
        overall_confidence="high",
        source_quality_notes=["Classified as code/build artifact and deliberately not imported into owner memory."],
    )
    insert_import(imp_id, package_type="mongoose_build_artifact", source_name=path,
                  source_sha256=sha, metadata=meta)
    insert_raw_file(make_id("raw", imp_id, path, sha), imp_id, path, sha, len(data), None)
    run.code_builds_archived += 1
    child = ArchiveChildResult(path=path, item_type="mongoose_build_artifact", status="archived",
                               message="preserved as project/build artifact; not owner memory", import_id=imp_id,
                               raw_files=1)
    run.children.append(child)
    insert_archive_item(run.import_id, child.__dict__)


def _is_mongoose_build_zip(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = {n.lower() for n in z.namelist()[:2000]}
    except Exception:
        return False
    return any(n.endswith("src/mongoose/cli.py") for n in names) and any(n.endswith("release.json") or n.endswith("pyproject.toml") for n in names)


def _looks_like_dump_text(data: bytes, name: str) -> bool:
    if len(data) > 2_000_000:
        return False
    text = data[:80_000].decode("utf-8", errors="replace")
    low = text.lower()
    return "mongoose thread knowledge dump" in low or "\"record_type\"" in low or name.lower().endswith(".jsonl")


def _zip_single(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, body)
    return buf.getvalue()
