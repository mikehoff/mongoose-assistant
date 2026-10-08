"""RTM document ingest + subcontext: any file is stored, text-extracted, indexed into the unified
file search, and correlated with voicemails/emails. Uses a temp DB + data dir so nothing real is
touched, and creates the shared files/file_fts schema so the global-search integration is exercised."""
import base64

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    # Create the shared files + file_fts schema (owned by db.py) so search integration works here.
    from mongoose.db import connect
    with connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, "
                    "title TEXT, body TEXT, mtime TEXT, device TEXT, indexed_at TEXT)")
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(title, body, path, content='')")
        con.commit()
    import importlib
    import mongoose.services.rtm_docs as rd
    importlib.reload(rd)
    return rd


def test_ingest_stores_extracts_and_indexes_into_global_search(env):
    rd = env
    r = rd.ingest(content=b"Service charge receipt. Flat 7. Invoice ref ACME-9001. Amount paid: 123.45.",
                  filename="receipt.txt", doc_type="receipt", flat_id="7")
    assert r["ok"] and r["id"] and r["doc_type"] == "receipt"
    assert r["extracted_chars"] > 0
    # it must be findable in the owner's UNIFIED search (the shared files index)
    from mongoose.services.file_index import search_files
    hits = search_files("ACME-9001", limit=5)
    assert any("receipt" in (h.get("title") or "").lower() for h in hits), hits


def test_unknown_doc_type_is_rejected(env):
    r = env.ingest(content=b"x", filename="x.txt", doc_type="banana")
    assert r["ok"] is False and "doc_type" in r["error"]


def test_list_get_and_context_summary(env):
    rd = env
    rd.ingest(content=b"Invoice for works. Flat 3.", filename="inv.txt", doc_type="invoice", flat_id="3")
    rd.ingest(content=b"Complaint about noise.", filename="c.txt", doc_type="complaint")
    docs = rd.list_documents()
    assert len(docs) == 2
    assert {d["doc_type"] for d in docs} == {"invoice", "complaint"}
    detail = rd.get_document(docs[0]["id"])
    assert detail and "related" in detail
    ctx = rd.context_summary()
    assert ctx["documents"] == 2 and ctx["by_type"].get("invoice") == 1
    assert set(rd.DOC_TYPES) == {"receipt", "invoice", "contract", "ticket", "feedback",
                                 "complaint", "photo_evidence", "other_evidence"}


def test_correlation_runs_and_is_empty_without_calls_or_emails(env):
    rd = env
    r = rd.ingest(content=b"Receipt. Flat 5. Ref ZZ-1234. 50.00.", filename="r.txt", doc_type="receipt", flat_id="5")
    # no voicemails/emails in this fresh DB, so no correlations , but the step must run cleanly
    assert r["correlations"] == 0
    assert rd.related(r["id"]) == []


def test_image_document_is_ocred_and_searchable(env, monkeypatch):
    rd = env
    # stub the on-device OCR so the test never needs Swift/Vision: prove an IMAGE routes through OCR
    # and its recognised text lands in the document AND in the unified search index.
    import mongoose.services.ocr as ocr
    monkeypatch.setattr(ocr, "ocr_image", lambda path, timeout=90: "Cleaning agreement with Cleanerson for flat 6")
    r = rd.ingest(content=b"\xff\xd8\xffnotarealjpegjustbytes", filename="contract.jpg",
                  doc_type="contract", flat_id="6")
    assert r["ok"] and r["extracted_chars"] > 0
    from mongoose.services.file_index import search_files
    hits = search_files("Cleanerson", limit=5)
    assert any("contract" in (h.get("title") or "").lower() for h in hits), hits


def test_reprocess_backfills_ocr_without_duplicating_search_rows(env, monkeypatch):
    rd = env
    import mongoose.services.ocr as ocr
    # first ingest with OCR unavailable (returns ''), so the image has no text yet
    monkeypatch.setattr(ocr, "ocr_image", lambda path, timeout=90: "")
    r = rd.ingest(content=b"bytes", filename="receipt.png", doc_type="receipt", flat_id="2")
    assert r["extracted_chars"] == 0
    from mongoose.db import connect
    with connect() as con:
        before = con.execute("SELECT COUNT(*) FROM files WHERE device='rtm-doc'").fetchone()[0]
    # now OCR "comes online" and we backfill
    monkeypatch.setattr(ocr, "ocr_image", lambda path, timeout=90: "Receipt total 250.00 for flat 2")
    out = rd.reprocess_missing()
    assert out["gained_text"] >= 1
    with connect() as con:
        after = con.execute("SELECT COUNT(*) FROM files WHERE device='rtm-doc'").fetchone()[0]
    assert after == before, "reprocess must UPDATE the search row in place, not duplicate it"
    from mongoose.services.file_index import search_files
    assert search_files("250.00", limit=5), "backfilled OCR text must be searchable"


def test_local_intake_accepts_any_file_including_zip(env):
    # the owner's local store is not web-served, so any file (incl. zip) is accepted and stored
    for name in ("archive.zip", "evidence.tar.gz", "notes.txt"):
        r = env.ingest(content=b"payload-bytes", filename=name, doc_type="other_evidence")
        assert r["ok"], name


def test_auditor_is_read_only_and_cannot_upload(env):
    r = env.ingest(content=b"x", filename="x.txt", doc_type="receipt", role="auditor")
    assert r["ok"] is False and "read-only" in r["error"]


def test_uploader_roles_are_allowed(env):
    rd = env
    for role in ("owner", "director", "admin", "leaseholder"):
        r = rd.ingest(content=f"doc for {role}".encode(), filename=f"{role}.txt",
                      doc_type="invoice", role=role)
        assert r["ok"], (role, r)


def test_unknown_role_is_rejected(env):
    r = env.ingest(content=b"x", filename="x.txt", doc_type="receipt", role="wizard")
    assert r["ok"] is False and "role" in r["error"]


def test_auditor_note_seam(env):
    rd = env
    doc = rd.ingest(content=b"Invoice. Flat 4.", filename="i.txt", doc_type="invoice", flat_id="4")
    # auditor cannot upload but CAN annotate
    n = rd.add_note(doc_id=doc["id"], text="Charge looks duplicated vs March.", author="ext-auditor",
                    role="auditor")
    assert n["ok"] and n["role"] == "auditor"
    detail = rd.get_document(doc["id"])
    assert any("duplicated" in nt["text"] for nt in detail["notes"])


def test_canonical_flat_linkage_and_evidence_registration(env):
    rd = env
    # flat_id given loosely; it must normalise to canonical 1ECF1, and a demand number in the text
    # must be picked up as a strong link.
    r = rd.ingest(content=b"Service charge receipt for demand 26-TO-DEC/1ECF3R0. Paid in full.",
                  filename="receipt.txt", doc_type="receipt", flat_id="1ecf1")
    assert r["ok"]
    doc = rd.get_document(r["id"])
    assert doc["flat_id"] == "1ECF1", "flat must be stored canonical"
    kinds = {(l["kind"], l["ref"]) for l in doc["related"]}
    assert ("flat", "1ECF1") in kinds, kinds
    assert ("demand", "26-TO-DEC/1ECF3R0") in kinds, kinds
    # and it must land in the GOVERNED evidence store, linked to the flat
    from mongoose.services import rtm_management as M
    ev = M.list_evidence(target_type="flat", target_id="1ECF1")
    assert ev and any("1ECF1" in (e.get("notes") or "") for e in ev), ev


def test_flat_situation_and_living_recorrelation(env):
    rd = env
    rd.ingest(content=b"Complaint about noise. Flat 1ECF2.", filename="c.txt",
              doc_type="complaint", flat_id="1ECF2")
    sit = rd.flat_situation("1ecf2")
    assert sit["ok"] and sit["flat_id"] == "1ECF2" and sit["document_count"] == 1
    assert "related" in sit["documents"][0]
    # living correlation re-runs cleanly over recent docs
    out = rd.recorrelate_recent()
    assert out["ok"] and out["recorrelated"] >= 1


def test_documents_tool_is_registered_read_only_and_in_catalogue():
    from mongoose import tools as T
    assert "rtm_documents" in T.TOOLS
    assert "rtm_documents" in T.READ_TOOLS
    assert any(t.get("name") == "rtm_documents" for t in T.tool_catalogue())


def test_upload_endpoint_ingests_base64(env, monkeypatch):
    # exercise the cockpit endpoint path with the nonce guard mocked off
    import mongoose.app as app_mod
    monkeypatch.setattr(app_mod, "_require_cockpit_nonce", lambda request: None)
    from starlette.testclient import TestClient
    c = TestClient(app_mod.app)
    payload = {"filename": "contract.txt", "doc_type": "contract", "flat_id": "9",
               "content_b64": "data:text/plain;base64," + base64.b64encode(b"Lease contract for flat 9.").decode()}
    r = c.post("/cockpit/rtm/documents/upload", json=payload).json()
    assert r["ok"] and r["doc_type"] == "contract"
    listing = c.get("/cockpit/rtm/documents").json()
    assert listing["ok"] and any(d["filename"] == "contract.txt" for d in listing["documents"])
