"""Guests can raise bugs, and the owner's Approvals tab becomes a real triage surface (WhatsApp drafts,
guest/owner bugs, imported-knowledge decisions). The guest bug path reveals nothing about the owner and
the approvals surface stays owner-only."""
from __future__ import annotations

import io
import json
import zipfile


from mongoose.services import bug_workflow as BW
import mongoose.app as A


def test_guest_can_raise_bug_and_it_is_flagged():
    r = BW.log_bug("the guest room light will not turn off", actor="guest:devX")
    bug_id = r["bug_id"]
    openb = BW.list_open_bugs()
    match = [b for b in openb if b["bug_id"] == bug_id]
    assert match and match[0]["from_guest"] is True and match[0]["reporter"] == "guest:devX"


def test_guest_bug_via_natural_language():
    out = A._maybe_handle_bug_report("something is broken: the wifi keeps dropping",
                                     is_guest=True, dev={"device_id": "devY"})
    assert out and out.get("bug_id") and "logged" in out.get("reply", "").lower()
    # the bug is now visible to the owner
    assert any(b["bug_id"] == out["bug_id"] for b in BW.list_open_bugs())


def test_owner_is_not_intercepted_by_bug_nl():
    # owners have the log_bug tool + full workflow; the deterministic guest path must not fire for them
    assert A._maybe_handle_bug_report("something is broken", is_guest=False, dev={"device_id": "mac"}) is None


def test_bug_dismiss_removes_from_queue():
    bug_id = BW.log_bug("transient thing", actor="owner")["bug_id"]
    assert any(b["bug_id"] == bug_id for b in BW.list_open_bugs())
    res = BW.dismiss_bug(bug_id)
    assert res.get("status") == "dismissed"
    assert not any(b["bug_id"] == bug_id for b in BW.list_open_bugs())


def test_approvals_aggregates_bugs_and_knowledge(monkeypatch):
    from mongoose.knowledge_import import import_bulk_dump_bytes
    BW.log_bug("the hallway light is stuck", actor="guest:devZ")

    buf = io.BytesIO()
    recs = [{"record_type": "health_context", "topic": "h", "summary": "Owner uses Nurofen and mentions grief.",
             "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "high"},
            {"record_type": "stable_user_fact", "topic": "f", "summary": "Owner lives in Anytown.",
             "source_basis": "user_stated", "confidence": 0.9, "sensitivity": "low"}]
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("knowledge_records.jsonl", "\n".join(json.dumps(r) for r in recs))
    import_bulk_dump_bytes(buf.getvalue(), source_name="knowledge_dump.zip")

    appr = A.cockpit_approvals()
    kinds = {a["kind"] for a in appr["approvals"]}
    assert "bug" in kinds and "knowledge_review" in kinds
    # every approval carries labelled actions the renderer can POST
    assert all(a.get("actions") and all("url" in ac and "label" in ac for ac in a["actions"])
               for a in appr["approvals"])
    # knowledge items show the real content (the fact), not just a category label
    kr = [a for a in appr["approvals"] if a["kind"] == "knowledge_review"]
    assert kr and any(a.get("body") for a in kr)
    # a guest-raised bug is flagged so the owner can see a guest hit a problem
    bugs = [a for a in appr["approvals"] if a["kind"] == "bug"]
    assert any(a.get("from_guest") for a in bugs)


def test_approvals_surface_is_owner_only():
    # a trusted_guest device is never allowed to reach the approvals surface
    assert not A._guest_allowed_device_route("/cockpit/approvals")
    # but the guest bug-report route IS allowed
    assert A._guest_allowed_device_route("/device/guest/bug")


def test_knowledge_resolve_action_bodies_are_valid():
    """The approval actions for knowledge items must carry the ids the resolve endpoints expect."""
    appr = A.cockpit_approvals()
    for a in appr["approvals"]:
        if a["kind"] == "knowledge_review":
            for ac in a["actions"]:
                assert ac["url"] == "/cockpit/knowledge/review/resolve"
                assert ac["body"]["review_id"] and ac["body"]["action"] in ("approve", "reject")
