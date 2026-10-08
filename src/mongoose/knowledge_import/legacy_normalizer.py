from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .package_detector import KnowledgeImportError, ParsedPackage, _unsafe_zip_name
from .schemas import RecordType, Sensitivity, SourceBasis, ThreadMetadata, ThreadRecord, make_id, site_terms

_TEXT_EXTS = (".md", ".txt", ".jsonl", ".yaml", ".yml", ".json")
_RECORD_KEYS = {"record_type", "topic", "summary", "source_basis"}


@dataclass(frozen=True)
class LegacyNormalisationResult:
    parsed: ParsedPackage
    normalised_from: str
    extraction_method: str


def normalise_legacy_package_bytes(data: bytes, *, source_name: str) -> LegacyNormalisationResult:
    """Turn older Mongoose dump packages into the modern ParsedPackage shape.

    This intentionally accepts the real-world mess: markdown-only dumps, JSONL sidecars with variant
    names, nested folder packages, and archives whose strict files sit under one prefix. It does not try
    to import code/build archives; callers should classify those first.
    """
    if not data:
        raise KnowledgeImportError("empty legacy package")
    sha = hashlib.sha256(data).hexdigest()
    raw_files: list[tuple[str, bytes, str | None]] = []
    warnings: list[str] = []
    texts: dict[str, str] = {}
    binaries_seen = 0

    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            for name in names:
                if _unsafe_zip_name(name):
                    raise KnowledgeImportError(f"unsafe zip member path: {name}")
                body = z.read(name)
                low = name.lower()
                text: str | None = None
                if low.endswith(_TEXT_EXTS):
                    text = body.decode("utf-8", errors="replace")
                    texts[name] = text
                else:
                    binaries_seen += 1
                raw_files.append((name, body, text))
    else:
        text = data.decode("utf-8", errors="replace")
        texts[source_name] = text
        raw_files.append((source_name, data, text))

    strict = _try_nested_strict(texts, raw_files, source_name=source_name, sha=sha)
    if strict:
        return LegacyNormalisationResult(strict, source_name, "nested_strict")

    metadata = _metadata_from_texts(texts, source_name=source_name)
    records, method, record_warnings = _records_from_texts(texts, metadata=metadata, source_name=source_name)
    warnings.extend(record_warnings)
    if not records:
        raise KnowledgeImportError("legacy package did not contain extractable records")
    if binaries_seen:
        warnings.append(f"legacy package preserved {binaries_seen} non-text members as raw files only")
    import_id = make_id("imp", "legacy_thread_dump", source_name, sha)
    parsed = ParsedPackage(
        package_type="chatgpt_thread_dump_legacy",
        source_name=source_name,
        source_sha256=sha,
        import_id=import_id,
        metadata=metadata,
        records=records,
        raw_files=raw_files,
        warnings=warnings,
    )
    return LegacyNormalisationResult(parsed, source_name, method)


def _try_nested_strict(texts: dict[str, str], raw_files: list[tuple[str, bytes, str | None]], *, source_name: str, sha: str) -> ParsedPackage | None:
    # Some packages store metadata.yaml and structured_records.jsonl under a single folder prefix.
    by_dir: dict[str, set[str]] = {}
    for path in texts:
        p = Path(path)
        by_dir.setdefault(str(p.parent) if str(p.parent) != "." else "", set()).add(p.name)
    for prefix, basenames in by_dir.items():
        if {"metadata.yaml", "structured_records.jsonl"}.issubset(basenames):
            meta_path = f"{prefix}/metadata.yaml" if prefix else "metadata.yaml"
            rec_path = f"{prefix}/structured_records.jsonl" if prefix else "structured_records.jsonl"
            try:
                metadata = ThreadMetadata(**(yaml.safe_load(texts[meta_path]) or {}))
                records, warnings = _parse_jsonl_records(texts[rec_path])
                return ParsedPackage(
                    package_type="chatgpt_thread_dump",
                    source_name=source_name,
                    source_sha256=sha,
                    import_id=make_id("imp", "chatgpt_thread_dump", source_name, sha),
                    metadata=metadata,
                    records=records,
                    raw_files=raw_files,
                    warnings=warnings,
                )
            except Exception:
                continue
    return None


def _records_from_texts(texts: dict[str, str], *, metadata: ThreadMetadata, source_name: str) -> tuple[list[ThreadRecord], str, list[str]]:
    warnings: list[str] = []
    # Prefer explicit JSONL sidecars first, regardless of filename.
    for path, text in sorted(texts.items(), key=lambda kv: (0 if kv[0].lower().endswith(".jsonl") else 1, kv[0])):
        if path.lower().endswith(".jsonl"):
            records, ws = _parse_jsonl_records(text)
            if records:
                return records, "jsonl_sidecar", ws
            warnings.extend([f"{path}: {w}" for w in ws[:5]])

    # Then JSONL code fences embedded in markdown.
    all_records: list[ThreadRecord] = []
    for path, text in texts.items():
        if not path.lower().endswith((".md", ".txt")):
            continue
        records, ws = _parse_jsonl_fences(text)
        if records:
            all_records.extend(records)
        warnings.extend([f"{path}: {w}" for w in ws[:5]])
    if all_records:
        return _dedupe_records(all_records), "markdown_jsonl_fence", warnings

    # YAML folder packages often contain canonical_facts.yaml, preferences_constraints.yaml,
    # body_posture_wellness.yaml etc. Convert obvious list/mapping items to records.
    yaml_records: list[ThreadRecord] = []
    for path, text in texts.items():
        if path.lower().endswith((".yaml", ".yml")):
            yaml_records.extend(_records_from_yaml_file(path, text, metadata))
    if yaml_records:
        return _dedupe_records(yaml_records), "yaml_folder", warnings

    # Last resort: structured-heading markdown. It is intentionally conservative and preserves the raw
    # dump too, so weaker records remain traceable.
    md_records: list[ThreadRecord] = []
    for path, text in texts.items():
        if path.lower().endswith((".md", ".txt")) and "MONGOOSE THREAD KNOWLEDGE DUMP" in text[:8000].upper():
            md_records.extend(_records_from_markdown(path, text, metadata))
    return _dedupe_records(md_records), "markdown_heading_extraction", warnings


def _parse_jsonl_records(text: str) -> tuple[list[ThreadRecord], list[str]]:
    records: list[ThreadRecord] = []
    warnings: list[str] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        raw = line.strip().rstrip(",")
        if not raw or not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
            if not _RECORD_KEYS.issubset(obj):
                continue
            records.append(_coerce_record(obj))
        except Exception as exc:
            warnings.append(f"line {idx} skipped: {exc}")
    return records, warnings


def _parse_jsonl_fences(text: str) -> tuple[list[ThreadRecord], list[str]]:
    records: list[ThreadRecord] = []
    warnings: list[str] = []
    fences = re.findall(r"```(?:jsonl|json)?[^\n`]*\n(.*?)```", text, flags=re.I | re.S)
    for fence_no, body in enumerate(fences, start=1):
        # JSONL fences are common. Some dumps wrap arrays or single JSON objects too.
        recs, ws = _parse_jsonl_records(body)
        records.extend(recs)
        warnings.extend([f"fence {fence_no}: {w}" for w in ws[:3]])
        if recs:
            continue
        stripped = body.strip()
        if not stripped.startswith(("[", "{")):
            continue
        try:
            parsed = json.loads(stripped)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict) and _RECORD_KEYS.issubset(item):
                    records.append(_coerce_record(item))
        except Exception:
            pass
    return records, warnings


def _coerce_record(obj: dict[str, Any]) -> ThreadRecord:
    clean = dict(obj)
    clean["record_type"] = _enum_value(clean.get("record_type"), RecordType, RecordType.other_useful_context)
    clean["source_basis"] = _enum_value(clean.get("source_basis"), SourceBasis, SourceBasis.mixed)
    clean["sensitivity"] = _enum_value(clean.get("sensitivity"), Sensitivity, Sensitivity.medium)
    details = clean.get("details")
    if not isinstance(details, dict):
        clean["details"] = {"legacy_details": details} if details not in (None, "") else {}
    clean["confidence"] = _coerce_confidence(clean.get("confidence", 0.55))
    clean.setdefault("time_context", "legacy import")
    if not isinstance(clean.get("use_for_mongoose"), str):
        clean["use_for_mongoose"] = _use_for(clean["record_type"], clean.get("summary", ""))
    if "do_not_overgeneralise" in clean:
        clean["do_not_overgeneralise"] = _coerce_bool(clean.get("do_not_overgeneralise"))
    else:
        clean["do_not_overgeneralise"] = clean["source_basis"] in {SourceBasis.assistant_suggested.value, SourceBasis.inferred.value, SourceBasis.mixed.value}
    return ThreadRecord(**clean)


def _coerce_confidence(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(float(value), 1.0))
    raw = str(value or "").strip().lower()
    if raw in {"high", "strong"}:
        return 0.9
    if raw in {"medium", "moderate", "mixed"}:
        return 0.65
    if raw in {"low", "weak", "uncertain"}:
        return 0.4
    try:
        return max(0.0, min(float(raw), 1.0))
    except Exception:
        return 0.55


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    raw = str(value).strip().lower()
    if raw in {"true", "yes", "1", "y"}:
        return True
    if raw in {"false", "no", "0", "n"}:
        return False
    # Many older dumps put the actual caution sentence in do_not_overgeneralise; treat any
    # substantive text there as a true guardrail and preserve the sentence in raw details if present.
    return bool(raw)


def _enum_value(value: Any, enum_cls, default) -> str:
    raw = str(value or "").split(".")[-1].strip()
    for item in enum_cls:
        if raw == item.value or raw == item.name:
            return item.value
    return default.value


def _metadata_from_texts(texts: dict[str, str], *, source_name: str) -> ThreadMetadata:
    for path, text in texts.items():
        if path.lower().endswith((".md", ".txt")):
            meta = _metadata_from_markdown(text)
            if meta:
                return meta
    title = _title_from_filename(source_name)
    domains = _domains_from_text("\n".join(t[:4000] for t in texts.values()) + " " + source_name)
    return ThreadMetadata(
        dump_version="legacy-normalised",
        thread_title=title,
        thread_date_range="unknown",
        extracted_at="unknown",
        primary_domains=domains,
        sensitivity_level=_sensitivity_from_text("\n".join(t[:8000] for t in texts.values())),
        overall_confidence="mixed",
        source_quality_notes=["Normalised from a legacy/non-strict Mongoose dump package."],
    )


def _metadata_from_markdown(text: str) -> ThreadMetadata | None:
    # Prefer the first YAML fence after Dump metadata, but any early YAML fence works.
    head = text[:12000]
    m = re.search(r"```yaml[^\n`]*\n(.*?)```", head, flags=re.I | re.S)
    if not m:
        return None
    try:
        raw = yaml.safe_load(m.group(1)) or {}
        if not isinstance(raw, dict):
            return None
        if "primary_domains" in raw and isinstance(raw["primary_domains"], str):
            raw["primary_domains"] = [raw["primary_domains"]]
        notes = raw.get("source_quality_notes")
        if isinstance(notes, str):
            raw["source_quality_notes"] = [notes]
        return ThreadMetadata(**raw)
    except Exception:
        return None


def _records_from_yaml_file(path: str, text: str, metadata: ThreadMetadata) -> list[ThreadRecord]:
    try:
        raw = yaml.safe_load(text) or {}
    except Exception:
        return []
    items: list[tuple[str, Any]] = []
    if isinstance(raw, list):
        items = [(Path(path).stem, x) for x in raw]
    elif isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, list):
                items.extend((str(key), x) for x in value)
            elif isinstance(value, dict):
                # nested profile sections
                for k2, v2 in value.items():
                    if isinstance(v2, list):
                        items.extend((f"{key}.{k2}", x) for x in v2)
                    else:
                        items.append((f"{key}.{k2}", v2))
            else:
                items.append((str(key), value))
    records: list[ThreadRecord] = []
    for idx, (section, item) in enumerate(items[:90], start=1):
        if isinstance(item, dict):
            text_bits = []
            for k in ("fact", "pattern", "preference", "summary", "details", "rule", "text"):
                if item.get(k):
                    text_bits.append(str(item.get(k)))
            summary = " ".join(text_bits).strip() or json.dumps(item, ensure_ascii=False)[:400]
            details = {"legacy_yaml_section": section, **item}
        else:
            summary = str(item).strip()
            details = {"legacy_yaml_section": section, "value": item}
        if len(summary) < 12:
            continue
        rtype = _record_type_for(f"{section} {summary}")
        records.append(ThreadRecord(
            record_type=rtype,
            topic=_topic_from_summary(summary, fallback=section),
            summary=summary[:900],
            details=details,
            source_basis=_source_for(summary),
            confidence=_confidence_for(summary, default=0.68),
            sensitivity=_sensitivity_from_text(f"{metadata.thread_title} {section} {summary}"),
            time_context=str(getattr(metadata, "thread_date_range", "legacy yaml")),
            use_for_mongoose=_use_for(rtype, summary),
            do_not_overgeneralise=_needs_do_not_overgeneralise(summary),
            retrieval_tags=_tags_for(f"{section} {summary}"),
        ))
    return records


def _records_from_markdown(path: str, text: str, metadata: ThreadMetadata) -> list[ThreadRecord]:
    records: list[ThreadRecord] = []
    sections = _split_sections(text)
    preferred = []
    for title, body in sections:
        tl = title.lower()
        if any(k in tl for k in ("canonical", "preferences", "constraints", "body", "posture", "wellness", "implementation", "unstructured", "long-tail", "structured")):
            preferred.append((title, body))
    if not preferred:
        preferred = sections[:10]
    for title, body in preferred:
        chunks = _candidate_lines(body)
        for idx, chunk in enumerate(chunks[:40], start=1):
            rtype = _record_type_for(f"{title} {chunk}")
            source = _source_for(chunk)
            sens = _sensitivity_from_text(f"{metadata.thread_title} {title} {chunk}")
            records.append(ThreadRecord(
                record_type=rtype,
                topic=_topic_from_summary(chunk, fallback=title),
                summary=chunk[:900],
                details={"legacy_markdown_file": path, "legacy_section": title, "extraction": "heading_line"},
                source_basis=source,
                confidence=_confidence_for(chunk, default=0.58 if source == SourceBasis.mixed else 0.66),
                sensitivity=sens,
                time_context=str(getattr(metadata, "thread_date_range", "legacy markdown")),
                use_for_mongoose=_use_for(rtype, chunk),
                do_not_overgeneralise=_needs_do_not_overgeneralise(chunk) or source in {SourceBasis.assistant_suggested, SourceBasis.inferred, SourceBasis.mixed},
                retrieval_tags=_tags_for(f"{title} {chunk}"),
            ))
            if len(records) >= 120:
                return records
    return records


def _split_sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^#{1,3}\s+(.+?)\s*$", text, flags=re.M))
    if not matches:
        return [("document", text)]
    out = []
    for i, m in enumerate(matches):
        title = re.sub(r"^[0-9.\s]+", "", m.group(1)).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((title, text[start:end]))
    return out


def _candidate_lines(body: str) -> list[str]:
    # Strip code fences to avoid re-importing raw JSON/YAML lines as prose.
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    lines = []
    for raw in body.splitlines():
        line = re.sub(r"^\s*[-*•]\s*", "", raw).strip()
        line = re.sub(r"^\s*\d+[.)]\s*", "", line).strip()
        if not line or len(line) < 35:
            continue
        if len(line) > 1000:
            line = line[:1000].rsplit(" ", 1)[0]
        if any(k in line.lower() for k in ("user", "mongoose", "remember", "preference", "constraint", "do not", "avoid", "exercise", "posture", "body", "bloat", "sleep", "mood", "rtm", "legal", "workout", "routine", "schedule", "exact phrase")):
            lines.append(line)
    # Deduplicate while preserving order.
    seen = set()
    out = []
    for line in lines:
        key = re.sub(r"\W+", " ", line.lower())[:180]
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


# Cue words for record typing, sensitivity, domains and tags. Each list is replaceable by the site profile
# (lexicon knowledge.legacy.<name>, see schemas.site_terms) so a household's own vocabulary is recognised.
_POSTURE_TERMS = ("posture", "rib", "pelvis", "hip", "calf", "knee", "calf", "compression")
_EXERCISE_TERMS = ("exercise", "workout", "sets", "reps", "gym", "swim", "yoga", "physio", "stretch")
_BODY_SIGNAL_TERMS = ("bloat", "puffy", "constipation", "stomach", "pain", "tension", "migraine", "sleep", "supplement", "aas", "steroid", "magnesium")
_HEALTH_CONTEXT_TERMS = ("supplement", "aas", "steroid", "magnesium")
_EMOTIONAL_TERMS = ("depressed", "grief", "attachment", "lonely", "self-worth", "trauma", "abandonment")
_HIGH_SENSITIVITY_TERMS = ("sexual", "porn", "trauma", "aas", "steroid", "medication", "blood results", "self-worth", "depression", "suicide", "health", "body image")
_BODY_WELLNESS_TERMS = ("posture", "exercise", "workout", "body", "bloat", "sleep", "supplement", "health", "puffy", "physio")
_TAG_TERMS = (
    "posture", "rib", "pelvis", "hip", "calf", "knee", "calf", "gym", "swim", "yoga",
    "bloat", "puffy", "constipation", "pizza", "bread", "dairy", "sleep", "mood", "grief", "trauma",
    "aas", "steroid", "supplement", "rtm", "service charge", "mongoose",
)


def _record_type_for(text: str) -> RecordType:
    t = text.lower()
    if any(k in t for k in ("do not", "avoid ", "unhelpful", "must not", "never ")):
        return RecordType.do_not_do
    if any(k in t for k in ("wording", "exact phrase", "tone", "phrase")):
        return RecordType.wording_preference
    if any(k in t for k in site_terms("legacy.posture_terms", _POSTURE_TERMS)):
        return RecordType.posture_pattern
    if any(k in t for k in site_terms("legacy.exercise_terms", _EXERCISE_TERMS)):
        return RecordType.exercise_or_activity
    if any(k in t for k in ("routine", "ritual", "breathing", "journalling", "check-in", "grounding")):
        return RecordType.wellness_routine
    if any(k in t for k in site_terms("legacy.body_signal_terms", _BODY_SIGNAL_TERMS)):
        health = site_terms("legacy.health_context_terms", _HEALTH_CONTEXT_TERMS)
        return RecordType.health_context if any(k in t for k in health) else RecordType.body_signal
    if any(k in t for k in site_terms("legacy.emotional_terms", _EMOTIONAL_TERMS)):
        return RecordType.emotional_context
    if any(k in t for k in ("schedule", "calendar", "every ", "daily", "weekly", "cadence")):
        return RecordType.schedule_constraint
    if any(k in t for k in ("rtm", "service charge", "tribunal", "lease", "flat", "reserve", "trust", "landlord")):
        return RecordType.legal_or_rtm_context
    if any(k in t for k in ("bug", "fix", "implementation", "architecture", "code", "test", "build")):
        return RecordType.project_requirement
    if any(k in t for k in ("preference", "prefers", "likes", "hates", "wants")):
        return RecordType.preference
    return RecordType.stable_user_fact


def _source_for(text: str) -> SourceBasis:
    t = text.lower()
    if "user corrected" in t or "correction" in t or "user clarified" in t:
        return SourceBasis.user_corrected
    if "user accepted" in t or "accepted" in t:
        return SourceBasis.user_accepted
    if "assistant suggested" in t or "assistant advice" in t or "suggested by assistant" in t:
        return SourceBasis.assistant_suggested
    if "inferred" in t or "likely" in t or "possibly" in t:
        return SourceBasis.inferred
    from .. import profile as _profile
    owner_id = _profile.owner_id().lower()
    if "user" in t or "owner" in t or (owner_id and owner_id in t):
        return SourceBasis.user_stated
    return SourceBasis.mixed


def _sensitivity_from_text(text: str) -> Sensitivity | str:
    t = text.lower()
    if any(k in t for k in site_terms("legacy.high_sensitivity_terms", _HIGH_SENSITIVITY_TERMS)):
        return Sensitivity.high
    if any(k in t for k in ("body", "posture", "mood", "sleep", "food", "bloat", "rtm", "legal")):
        return Sensitivity.medium
    return Sensitivity.low


def _domains_from_text(text: str) -> list[str]:
    t = text.lower()
    domains = []
    if any(k in t for k in site_terms("legacy.body_wellness_terms", _BODY_WELLNESS_TERMS)):
        domains.append("body_wellness")
    if any(k in t for k in ("grief", "attachment", "trauma", "porn", "self-worth", "depression", "anxiety")):
        domains.append("emotional_support")
    from .. import profile as _profile
    if any(k in t for k in ("rtm", "service charge", "tribunal", "lease", _profile.rtm_name().lower())):
        domains.append("rtm")
    if any(k in t for k in ("mongoose", "code", "bug", "build", "implementation")):
        domains.append("project")
    if not domains:
        domains.append("general")
    return domains[:4]


def _confidence_for(text: str, *, default: float) -> float:
    t = text.lower()
    if "confidence: high" in t or "user stated" in t or "user reports" in t or "user says" in t:
        return 0.82
    if "assistant suggested" in t or "inferred" in t:
        return min(default, 0.58)
    return default


def _needs_do_not_overgeneralise(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("do not overgeneralise", "do not assume", "uncertain", "assistant suggested", "hypothesis", "old data", "sensitive"))


def _topic_from_summary(summary: str, *, fallback: str) -> str:
    s = re.sub(r"\s+", " ", summary).strip()
    s = re.sub(r"^(User|Owner|Mongoose)\s+(reports|states|prefers|should|must|can|has|wants|needs)\s+", "", s, flags=re.I)
    topic = s[:80].strip(" .:-") or fallback
    return topic


def _tags_for(text: str) -> list[str]:
    terms = site_terms("legacy.tag_terms", _TAG_TERMS)
    tl = text.lower()
    return [t for t in terms if t in tl][:12]


def _use_for(record_type: str | RecordType, summary: str) -> str:
    rt = str(record_type).split(".")[-1]
    if rt in {"posture_pattern", "exercise_or_activity", "wellness_routine", "body_signal"}:
        return "Feed the body, posture, wellness, care-planning and contextual nudge layers."
    if rt in {"health_context", "trigger_or_warning_sign"}:
        return "Keep as sensitive health context; research or review before advice."
    if rt in {"wording_preference", "preference", "do_not_do", "emotional_context"}:
        return "Shape coaching persona, tone, memory guardrails and supportive responses."
    if rt in {"project_requirement", "architecture_decision", "bug_report", "fix_or_patch"}:
        return "Feed project memory, implementation planning and regression checks."
    if rt in {"legal_or_rtm_context", "financial_or_accounting_rule"}:
        return "Feed RTM/legal/project guardrails and retrieval."
    return "Keep searchable and consider for long-term memory if repeated or high-confidence."


def _title_from_filename(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[_-]+", " ", stem)
    stem = re.sub(r"\bmongoose\b|\bthread\b|\bknowledge\b|\bdump\b|\bzip\b", "", stem, flags=re.I)
    return re.sub(r"\s+", " ", stem).strip().title() or name


def _dedupe_records(records: list[ThreadRecord]) -> list[ThreadRecord]:
    seen: set[str] = set()
    out: list[ThreadRecord] = []
    for r in records:
        key = r.stable_record_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
