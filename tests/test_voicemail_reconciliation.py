"""Voicemail dual-source reconciliation, hardened: all order combinations, edge cases, no audio, old field names."""
import os
import tempfile
from email.message import Message


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ.pop("MONGOOSE_INPROCESS_SCHEDULER", None)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def _vmsg(date=None):
    msg = Message()
    msg["From"] = "voicemail@voip.example"
    msg["Subject"] = "New message in mailbox for master group of 30000000 from 07700900123"
    if date:
        msg["Date"] = date
    return msg


def _audio():
    return [{"id": 5, "filename": "vm.wav", "mime_type": "audio/wav", "size_bytes": 1000}]


def _pi(voicemail_at_key="voicemail_at"):
    return {"msg_id": "vm_xyz", "caller": "07700900123", "account_number": "30000000",
            voicemail_at_key: "2026-06-24T14:30:00+01:00",
            "transcript": "Call me back.", "from": "ha/example_court"}


def test_imap_with_audio_then_pi_is_one_row():
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_email, index_voicemail_from_protocol_staging, list_voicemails
    index_voicemail_from_email(msg=_vmsg("Tue, 24 Jun 2026 14:30:00 +0100"), body="New voicemail.",
        source="imap", source_message_id="<m1>", email_id=1, mailbox="INBOX", attachments=_audio())
    index_voicemail_from_protocol_staging(_pi())
    vms = list_voicemails(10)
    assert len(vms) == 1
    assert vms[0].get("audio_attachment_id") is not None
    assert vms[0].get("transcript") == "Call me back."


def test_imap_without_audio_then_pi_is_one_row():
    """IMAP notice arrives before attachment download completes — must still index and reconcile."""
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_email, index_voicemail_from_protocol_staging, list_voicemails
    r = index_voicemail_from_email(msg=_vmsg("Tue, 24 Jun 2026 14:30:00 +0100"), body="New voicemail.",
        source="imap", source_message_id="<m1>", email_id=1, mailbox="INBOX", attachments=[])
    assert r["ok"] is True, f"expected ok=True with no audio, got {r}"
    index_voicemail_from_protocol_staging(_pi())
    assert len(list_voicemails(10)) == 1


def test_pi_first_then_imap_is_one_row():
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_email, index_voicemail_from_protocol_staging, list_voicemails
    index_voicemail_from_protocol_staging(_pi())
    index_voicemail_from_email(msg=_vmsg("Tue, 24 Jun 2026 14:30:00 +0100"), body="New voicemail.",
        source="imap", source_message_id="<m1>", email_id=1, mailbox="INBOX", attachments=_audio())
    vms = list_voicemails(10)
    assert len(vms) == 1
    assert vms[0].get("audio_attachment_id") is not None


def test_repeated_pi_drain_no_duplicate():
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_protocol_staging, list_voicemails
    index_voicemail_from_protocol_staging(_pi())
    index_voicemail_from_protocol_staging(_pi())   # HA add-on re-sends same line
    assert len(list_voicemails(10)) == 1


def test_imap_no_date_header_still_indexes():
    """No Date header: falls back to hash key; must still succeed so the voicemail isn't lost."""
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_email
    r = index_voicemail_from_email(msg=_vmsg(), body="New voicemail.",
        source="imap", source_message_id="<m-no-date>", email_id=1, mailbox="INBOX", attachments=[])
    assert r["ok"] is True
    assert r.get("identity_key")   # must produce *some* stable key, not empty


def test_imap_malformed_date_still_indexes():
    """Garbage Date header: must not crash, falls back to hash key."""
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_email
    r = index_voicemail_from_email(msg=_vmsg("not a date at all"), body="New voicemail.",
        source="imap", source_message_id="<m-bad-date>", email_id=1, mailbox="INBOX", attachments=[])
    assert r["ok"] is True


def test_imap_date_header_gives_fingerprint_not_hash():
    """When Date is present the identity key should be a stable fingerprint, not a hash."""
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_email
    r = index_voicemail_from_email(msg=_vmsg("Tue, 24 Jun 2026 14:30:00 +0100"), body="New voicemail.",
        source="imap", source_message_id="<m1>", email_id=1, mailbox="INBOX", attachments=[])
    assert r["ok"] is True
    assert "fingerprint" in (r.get("identity_key") or ""), f"expected fingerprint key, got {r.get('identity_key')}"


def test_pi_old_timestamp_field_reconciles():
    """Old Pi envelopes use 'timestamp' instead of 'voicemail_at' — must still fingerprint correctly."""
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_email, index_voicemail_from_protocol_staging, list_voicemails
    index_voicemail_from_email(msg=_vmsg("Tue, 24 Jun 2026 14:30:00 +0100"), body="New voicemail.",
        source="imap", source_message_id="<m1>", email_id=1, mailbox="INBOX", attachments=_audio())
    index_voicemail_from_protocol_staging(_pi(voicemail_at_key="timestamp"))
    assert len(list_voicemails(10)) == 1


def test_different_calls_are_separate_rows():
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_email, list_voicemails
    index_voicemail_from_email(msg=_vmsg("Tue, 24 Jun 2026 14:30:00 +0100"), body="New voicemail.",
        source="imap", source_message_id="<m1>", email_id=1, mailbox="INBOX", attachments=[])
    index_voicemail_from_email(msg=_vmsg("Tue, 24 Jun 2026 15:00:00 +0100"), body="New voicemail.",
        source="imap", source_message_id="<m2>", email_id=2, mailbox="INBOX", attachments=[])
    assert len(list_voicemails(10)) == 2


def test_voicemail_searchable_by_caller():
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_protocol_staging, search_voicemails
    index_voicemail_from_protocol_staging(_pi())
    hits = search_voicemails("07700900123")
    assert len(hits) >= 1


def test_voicemail_search_normalises_spaced_phone_number():
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_protocol_staging, search_voicemails
    index_voicemail_from_protocol_staging(_pi())
    hits = search_voicemails("07700 900123")
    assert len(hits) >= 1


def test_setup_wizard_normalises_bare_ip():
    _fresh()
    from mongoose.setup_wizard import verify_home_assistant
    ok, msg = verify_home_assistant({"HA_URL": "192.168.1.20", "HA_TOKEN": "tok"})
    # Will fail to connect (no real HA) but must NOT produce an mDNS or crash error
    assert "mDNS" not in msg
    assert "Traceback" not in msg


def test_setup_wizard_rejects_mdns_before_connecting():
    _fresh()
    from mongoose.setup_wizard import verify_home_assistant
    ok, msg = verify_home_assistant({"HA_URL": "http://homeassistant.local:8123", "HA_TOKEN": "tok"})
    assert ok is False and "mDNS" in msg


def test_setup_wizard_rejects_bare_mdns():
    _fresh()
    from mongoose.setup_wizard import verify_home_assistant
    ok, msg = verify_home_assistant({"HA_URL": "homeassistant.local", "HA_TOKEN": "tok"})
    assert ok is False and "mDNS" in msg
