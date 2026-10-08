"""Data dump + journal privacy.

Two features under test:

1. DATA DUMP , on-demand owner-scoped exports addressable by download link or by a dump id. The hard
   requirement is the privacy boundary: an owner dump must never contain guest-scoped private content.

2. JOURNAL PRIVACY , the two-plane model. A guest's verbatim journal is encrypted at rest and readable
   only back to that guest; meanwhile non-sensitive facts are distilled into the guest's memory scope so
   the model grows familiar. The thing that must hold: one guest cannot read another's entries, the
   verbatim is not plaintext at rest, the model still learns, and guest content never leaks into a dump.

DB isolation is automatic via conftest (_isolate_db_per_test).
"""
from __future__ import annotations

import io
import zipfile



# ── data dump ──────────────────────────────────────────────────────────────────────────────────────
def test_dump_create_and_retrieve_by_id():
    from mongoose.services import data_dump as DD
    from mongoose.db import connect
    with connect() as con:
        con.execute("INSERT INTO diary_entries(at,kind,summary) VALUES('2026-06-30T10:00:00Z','t','hello')")
    r = DD.create_dump("diary")
    assert r["ok"] is True
    assert r["dump_id"].startswith("dump_")
    # the link now carries an expiring token so it is clickable without a cockpit nonce
    assert r["download_url"].startswith(f"/dump/{r['dump_id']}/download?token=")
    assert r.get("expires_at")
    # retrievable by id
    got = DD.dump_bytes(r["dump_id"])
    assert got is not None
    data, filename = got
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert "manifest.json" in zf.namelist()
    assert "diary.json" in zf.namelist()
    assert "hello" in zf.read("diary.json").decode()


def test_dump_id_is_time_ordered_and_listed():
    from mongoose.services import data_dump as DD
    a = DD.create_dump("tasks")["dump_id"]
    b = DD.create_dump("tasks")["dump_id"]
    listed = DD.list_dumps()["dumps"]
    ids = [d["dump_id"] for d in listed]
    assert a in ids and b in ids
    # newest first
    assert ids.index(b) <= ids.index(a)


def test_owner_dump_excludes_guest_scoped_facts():
    """The privacy boundary: a guest-scoped fact must never appear in an owner dump."""
    from mongoose.services import data_dump as DD
    from mongoose.db import connect
    with connect() as con:
        con.execute("INSERT INTO memory_facts(fact,source,dedupe_key,scope,created_at,updated_at) "
                    "VALUES('owner drinks tea','explicit','k1','owner','2026-06-30','2026-06-30')")
        con.execute("INSERT INTO memory_facts(fact,source,dedupe_key,scope,created_at,updated_at) "
                    "VALUES('guest private secret','explicit','k2','guest:abc','2026-06-30','2026-06-30')")
    r = DD.create_dump("facts")
    data, _ = DD.dump_bytes(r["dump_id"])
    facts_json = zipfile.ZipFile(io.BytesIO(data)).read("facts.json").decode()
    assert "owner drinks tea" in facts_json
    assert "guest private secret" not in facts_json


def test_unknown_dump_kind_is_rejected():
    from mongoose.services import data_dump as DD
    r = DD.create_dump("nonsense_kind")
    assert r["ok"] is False
    assert "unknown dump kind" in r["error"]


# ── journal privacy ──────────────────────────────────────────────────────────────────────────────
def test_guest_journal_encrypts_at_rest_and_reads_back():
    from mongoose.services import journal_privacy as JP
    from mongoose.db import connect
    rec = JP.record_guest_journal("dev1", "Playing guitar helped me feel calmer. Private family detail.")
    assert rec["ok"] is True and rec["encrypted"] is True
    # verbatim is ciphertext at rest, NOT plaintext
    with connect() as con:
        ct = con.execute("SELECT ciphertext FROM guest_journal WHERE device_id='dev1'").fetchone()[0]
    assert ct and "guitar" not in ct and "family" not in ct
    # the guest reads their own entry back in cleartext
    back = JP.read_guest_journal("dev1")
    assert "guitar" in back["entries"][0]["text"]


def test_one_guest_cannot_read_another_guests_entry():
    """Per-guest key derivation: guest B's key cannot decrypt guest A's ciphertext."""
    from mongoose.services import journal_privacy as JP
    from mongoose.db import connect
    JP.record_guest_journal("guestA", "A's private words about something personal")
    with connect() as con:
        ct_a = con.execute("SELECT ciphertext FROM guest_journal WHERE device_id='guestA'").fetchone()[0]
    # B attempts to decrypt A's ciphertext with B's derived key , must fail
    assert JP.decrypt_for_guest("guestB", ct_a) is None
    # and A still decrypts their own fine
    assert JP.decrypt_for_guest("guestA", ct_a) is not None


def test_model_still_learns_facts_from_guest_journal():
    """The whole point of the two-plane split: familiarity grows even though verbatim stays private."""
    from mongoose.services import journal_privacy as JP
    from mongoose.db import connect
    JP.record_guest_journal("dev2", "Went for a run and then did some cooking, felt good")
    with connect() as con:
        facts = [r[0] for r in con.execute(
            "SELECT fact FROM memory_facts WHERE scope='guest:dev2'").fetchall()]
    # at least one non-sensitive fact distilled into the guest's OWN scope
    assert facts, "model learned nothing about the guest"
    assert any("run" in f.lower() or "cook" in f.lower() for f in facts)


def test_guest_journal_summary_returns_counts_not_content():
    from mongoose.services import journal_privacy as JP
    JP.record_guest_journal("dev3", "Something private and personal I do not want exposed")
    summ = JP.guest_journal_summary("dev3")
    assert summ["entry_count"] == 1
    assert summ["facts_learned"] >= 0
    # the summary must not carry the verbatim journal text
    blob = str(summ).lower()
    assert "personal i do not want" not in blob
    assert "do not want exposed" not in blob


def test_guest_journal_never_appears_in_owner_dump():
    """Defense in depth: even though guest_journal is encrypted, it is also structurally excluded from
    dumps. An owner 'everything' dump must contain no guest journal content at all."""
    from mongoose.services import journal_privacy as JP
    from mongoose.services import data_dump as DD
    JP.record_guest_journal("devD", "Secret guest reflection about anxiety and family")
    r = DD.create_dump("everything")
    data, _ = DD.dump_bytes(r["dump_id"])
    zf = zipfile.ZipFile(io.BytesIO(data))
    allblob = " ".join(zf.read(n).decode() for n in zf.namelist())
    assert "anxiety" not in allblob
    assert "guest_journal" not in zf.namelist()


def test_owner_journal_is_not_routed_to_guest_encryption():
    """Owner journal entries (non-guest conversation) must NOT create guest_journal rows , the owner's
    own data on the owner's machine is not encrypted into the per-guest store."""
    from mongoose.services import body_literacy as BL
    from mongoose.db import connect
    # owner conversation id (not 'guest:...')
    BL._save_journal_entry("freeform", {"happened": "owner wrote this", "emotion": "fine"}, "owner-convo")
    with connect() as con:
        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='guest_journal'").fetchone()
        # the owner path must not have created any guest journal rows. If the table was never created at
        # all, that equally proves the owner path did not touch the guest store.
        n = con.execute("SELECT COUNT(*) FROM guest_journal").fetchone()[0] if exists else 0
    assert n == 0


# --- totality-audit regressions (data_dump gating + private-text redaction) ---

def test_data_dump_chat_route_is_sensitive_not_immediate():
    """A chat request for a data dump must PROPOSE (need confirmation), never package private data
    inline. data_dump belongs in the sensitive tier."""
    from mongoose.tools import SENSITIVE_TOOLS
    assert "data_dump" in SENSITIVE_TOOLS
    from mongoose.services.sensitive_tools import propose_sensitive_action
    prop = propose_sensitive_action("data_dump", {"text": "give me a data dump"})
    assert prop["needs_confirmation"] is True
    assert prop["confirm_token"]  # a token to confirm with
    # the proposal alone must not have produced a dump
    from mongoose.db import connect
    with connect() as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dump_registry'").fetchone()
        n = con.execute("SELECT COUNT(*) FROM dump_registry").fetchone()[0] if exists else 0
    assert n == 0


def test_data_dump_executes_only_after_confirmation_and_returns_token_link():
    from mongoose.services.sensitive_tools import propose_sensitive_action, confirm_sensitive_action
    prop = propose_sensitive_action("data_dump", {"text": "export my data"})
    res = confirm_sensitive_action(prop["confirm_token"])
    assert res.get("ok") is True
    assert "?token=" in res.get("download_url", "")


def test_dump_token_download_link_is_clickable_without_nonce_but_plain_id_is_not():
    """The token link must serve bytes without a cockpit nonce (it self-validates the token); the bare
    id without a token must still require the nonce."""
    import os
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    from mongoose.services import data_dump as DD
    r = DD.create_dump("everything")
    did = r["dump_id"]
    token = r["download_url"].split("token=")[1]
    from fastapi.testclient import TestClient
    from mongoose.app import app
    with TestClient(app) as c:
        ok = c.get(f"/dump/{did}/download?token={token}")
        assert ok.status_code == 200 and ok.content[:2] == b"PK"
        bad = c.get(f"/dump/{did}/download?token=wrong")
        assert bad.status_code == 403
        nonce_needed = c.get(f"/dump/{did}/download")
        assert nonce_needed.status_code == 403


def test_journal_dump_is_narrow_and_support_dump_redacts_journal_text():
    """An explicit journal dump includes the private wording; a broad everything dump redacts it but
    still shows that journaling happened."""
    import io
    import zipfile
    from mongoose.services import body_state, data_dump as DD
    body_state.log_journal(mode="journal:freeform", body_signal="tight chest",
                           text="deeply private worry about the case", emotion_words=["anxious"],
                           need="calm", source="test")

    def blob_for(kind):
        rr = DD.create_dump(kind)
        data, _ = DD.dump_bytes(rr["dump_id"])
        zf = zipfile.ZipFile(io.BytesIO(data))
        return " ".join(zf.read(n).decode() for n in zf.namelist())

    assert "deeply private worry" in blob_for("journal")
    broad = blob_for("everything")
    assert "deeply private worry" not in broad
    assert "private journal text redacted" in broad
