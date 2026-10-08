"""Archive import report must reflect what the child dumps actually compiled.

Regression: the recursive archive report used to hardcode zeros for canonical_memories/persona_rules/
scheduler/insight/safety counts even though the database was populated correctly, so the import looked
thinner than it was. This builds a minimal recursive archive (a dump zip nested inside an archive zip)
and asserts the top-level report sums the child compiler counts."""
from __future__ import annotations

import io
import warnings
import zipfile

warnings.filterwarnings("ignore")

from mongoose.db import init_db
from mongoose.knowledge_import import import_bulk_dump_bytes

_V2_DUMP = '''# MONGOOSE THREAD KNOWLEDGE DUMP

## 0. Dump metadata

```yaml
dump_version: "mongoose-thread-dump-v2"
thread_title: "Test body thread"
extracted_at: "2026-07-01"
primary_domains:
  - "body"
  - "health"
sensitivity_level: "medium"
overall_confidence: "high"
```

## Useful raw details

- The owner swims weekly and uses a yoga for recovery after training.
- The owner gets bloated after spicy food and avoids it before events.
- The owner takes a prescribed medication daily.
- After desk time the owner's right side compresses, so they do a short posture reset.

## Things that repeatedly came up

- Swimming and yoga recovery is a recurring theme.
- Posture reset after desk time keeps coming up.
'''


def _archive_bytes() -> bytes:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("test_thread_mongoose_dump.md", _V2_DUMP)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("test_thread_mongoose_dump.zip", inner.getvalue())
    return outer.getvalue()


def test_archive_report_includes_child_compiler_counts():
    init_db()
    rep = import_bulk_dump_bytes(_archive_bytes(), source_name="mini_archive.zip").model_dump()
    assert rep["records_imported"] >= 1
    assert rep["claims_created"] >= 1
    compiler = ["canonical_memories_created", "persona_rules_created",
                "scheduler_candidates_created", "insight_candidates_created", "safety_rules_created"]
    # The bug returned all zeros here even when the DB was populated.
    assert sum(rep[k] for k in compiler) > 0, f"report did not sum child counts: {rep}"
    # Health content (a prescribed medication) must produce at least one safety rule.
    assert rep["safety_rules_created"] >= 1
