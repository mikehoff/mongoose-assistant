from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from .schemas import ThreadMetadata, ThreadRecord, make_id


class KnowledgeImportError(Exception):
    """A bulk import package could not be parsed safely."""


@dataclass(frozen=True)
class ParsedPackage:
    package_type: str
    source_name: str
    source_sha256: str
    import_id: str
    metadata: ThreadMetadata
    records: list[ThreadRecord]
    raw_files: list[tuple[str, bytes, str | None]]
    warnings: list[str]


def looks_like_thread_dump_zip(data: bytes, filename: str = "") -> bool:
    if not data or not zipfile.is_zipfile(io.BytesIO(data)):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = {n.strip("/") for n in z.namelist()}
    except Exception:
        return False
    return "structured_records.jsonl" in names and "metadata.yaml" in names


def parse_package(path: Path) -> ParsedPackage:
    return parse_package_bytes(path.read_bytes(), source_name=path.name)


def parse_package_bytes(data: bytes, *, source_name: str = "upload.zip") -> ParsedPackage:
    if not data:
        raise KnowledgeImportError("empty import package")
    sha = hashlib.sha256(data).hexdigest()
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise KnowledgeImportError("knowledge import currently expects a zip package")
    raw_files: list[tuple[str, bytes, str | None]] = []
    warnings: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        safe_names = []
        for name in names:
            if _unsafe_zip_name(name):
                raise KnowledgeImportError(f"unsafe zip member path: {name}")
            if name.endswith("/"):
                continue
            safe_names.append(name)
        if "metadata.yaml" not in safe_names or "structured_records.jsonl" not in safe_names:
            raise KnowledgeImportError("not a mongoose thread dump zip: metadata.yaml and structured_records.jsonl are required")
        metadata = _parse_metadata(z.read("metadata.yaml"))
        records, record_warnings = _parse_jsonl(z.read("structured_records.jsonl"))
        warnings.extend(record_warnings)
        for name in safe_names:
            body = z.read(name)
            text: str | None = None
            if name.lower().endswith((".txt", ".md", ".jsonl", ".yaml", ".yml", ".json")):
                text = body.decode("utf-8", errors="replace")
            raw_files.append((name, body, text))
    package_type = "chatgpt_thread_dump"
    import_id = make_id("imp", package_type, source_name, sha)
    return ParsedPackage(
        package_type=package_type,
        source_name=source_name,
        source_sha256=sha,
        import_id=import_id,
        metadata=metadata,
        records=records,
        raw_files=raw_files,
        warnings=warnings,
    )


def _unsafe_zip_name(name: str) -> bool:
    p = Path(name)
    return name.startswith("/") or ".." in p.parts or "\\" in name


def _parse_metadata(data: bytes) -> ThreadMetadata:
    try:
        raw = yaml.safe_load(data.decode("utf-8", errors="replace")) or {}
    except Exception as exc:
        raise KnowledgeImportError(f"metadata.yaml could not be parsed: {exc}") from exc
    if not isinstance(raw, dict):
        raise KnowledgeImportError("metadata.yaml must contain a mapping")
    return ThreadMetadata(**raw)


def _parse_jsonl(data: bytes) -> tuple[list[ThreadRecord], list[str]]:
    text = data.decode("utf-8", errors="replace")
    records: list[ThreadRecord] = []
    warnings: list[str] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            records.append(ThreadRecord(**raw))
        except Exception as exc:
            warnings.append(f"structured_records.jsonl line {idx} skipped: {exc}")
    return records, warnings
