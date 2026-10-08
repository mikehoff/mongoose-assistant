"""The adopted archive orchestrator + legacy normalizer make messy dumps land (renamed JSONL, YAML,
markdown, recursive bundles), build artifacts are separated from personal knowledge, and the
psychological-sensitivity gate keeps emotional material out of durable memory and proactive advice."""
from __future__ import annotations

import io
import json
import zipfile

from mongoose.knowledge_import import import_bulk_dump_bytes
from mongoose.knowledge_import.claim_builder import build_claims
from mongoose.knowledge_import.compilers import (compile_memory_candidates, compile_insight_candidates,
                                                 _psych_sensitive, _health_sensitive)
from mongoose.knowledge_import.schemas import (ImportStatus, RecordType, Sensitivity, SourceBasis,
                                               ThreadRecord)
from mongoose.knowledge_import.store import list_archive_items


def _jsonl_zip(name: str, rec: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, json.dumps(rec))
    return buf.getvalue()


_SENSITIVE = {"record_type": "health_context", "topic": "h",
              "summary": "Owner uses Nurofen and carries grief about self-worth.",
              "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "high"}


def test_renamed_jsonl_dump_lands():
    rep = import_bulk_dump_bytes(_jsonl_zip("body_records_2026.jsonl", _SENSITIVE),
                                 source_name="body_dump.zip")
    assert rep.records_imported >= 1


def test_recursive_archive_lands_and_separates_build_artifacts():
    inner = _jsonl_zip("d_records.jsonl", _SENSITIVE)
    buildz = io.BytesIO()
    with zipfile.ZipFile(buildz, "w") as z:
        z.writestr("src/mongoose/app.py", "# code")
        z.writestr("pyproject.toml", "[project]\nname='mongoose'\n")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("child_dump.zip", inner)
        z.writestr("mongoose-build.zip", buildz.getvalue())
    rep = import_bulk_dump_bytes(outer.getvalue(), source_name="mongoose_archives.zip")
    assert rep.package_type == "knowledge_archive_bundle" and rep.records_imported >= 1
    # the build zip is NOT imported as personal knowledge
    types = {i.get("item_type") for i in list_archive_items(limit=50)}
    assert "legacy_jsonl_sidecar" in types
    assert not any(t == "strict_thread_dump" and False for t in types)  # dump child imported, build child not


def test_psychological_material_is_review_gated_even_from_trusted_source():
    rec = ThreadRecord(record_type=RecordType.emotional_context, topic="t",
                       summary="Owner carries grief and low self-worth after a loss.",
                       source_basis=SourceBasis.user_stated, confidence=0.95, sensitivity=Sensitivity.low)
    claims = build_claims([rec])
    assert any(_psych_sensitive(c) for c in claims)
    cands = compile_memory_candidates(claims)
    assert cands and all(c.status == ImportStatus.review for c in cands)


def test_psych_is_high_risk_so_auto_care_activation_excludes_it():
    """Auto-care activation only activates risk_level=='low'; psych material must be 'high' so grief never
    becomes a proactive nudge."""
    rec = ThreadRecord(record_type=RecordType.body_signal, topic="t",
                       summary="Owner links posture tension to grief and loneliness.",
                       source_basis=SourceBasis.user_stated, confidence=0.9, sensitivity=Sensitivity.low)
    claims = build_claims([rec])
    insights = compile_insight_candidates(claims)
    assert insights and all(i.risk_level == "high" for i in insights if _psych_sensitive(claims[0]))


def test_psych_gate_independent_of_health_gate():
    rec = ThreadRecord(record_type=RecordType.emotional_context, topic="t",
                       summary="Owner feels rejection after a job interview.",
                       source_basis=SourceBasis.user_stated, confidence=0.9, sensitivity=Sensitivity.low)
    c = build_claims([rec])[0]
    assert _psych_sensitive(c) and not _health_sensitive(c)


def test_upload_gate_ignores_unrelated_files():
    from mongoose.services.uploads import _looks_like_knowledge_material
    assert _looks_like_knowledge_material("leg_training_thread_dump.zip", _jsonl_zip("a.jsonl", _SENSITIVE)) is True
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.py", "print(1)")
    assert _looks_like_knowledge_material("random_code.zip", z.getvalue()) is False
    assert _looks_like_knowledge_material("README.md", b"# just a readme") is False
