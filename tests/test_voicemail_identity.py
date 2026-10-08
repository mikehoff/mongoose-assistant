"""Canonical voicemail identity , dual-source reconciliation (IMAP VoIP provider + Pi/HA staging)."""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    for k in ("PROTOCOL_V3_ENABLED", "PI_SSH_HOST", "PI_SSH_PASSWORD", "PI_SSH_KEY"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    return d


UUID = "2fb7242e082749b9a5107dd849ccd04d"


# --- identity logic ---

def test_uuid_collapses_imap_and_pi():
    from mongoose.services.voicemail_identity import identity_from_imap, identity_from_staging
    imap_key, imap_uid, _ = identity_from_imap(
        sender="voicemail@voip.example",
        subject="New message in mailbox for master group of 30000000 from 07700900123",
        body=f"Delete: https://voip.example/x?uuid={UUID}",
        message_id="<a@voip>", email_date="Monday, June 22, 2026 at 3:54:44 AM")
    env = {"msg_id": "vm_x", "timestamp": "2026-06-22T03:54:44+01:00"}
    norm = {"uid": UUID, "caller": "07700900123", "wav_rel_path": f"/config/{UUID}.wav"}
    pi_key, pi_uid, _ = identity_from_staging(env, norm)
    assert imap_key == pi_key
    assert imap_uid == pi_uid == UUID


def test_fingerprint_collapses_across_tz_formats():
    """No UUID: account+caller+minute, with the VoIP provider naive time treated as BST so it matches an
    offset-aware ISO timestamp for the same call."""
    from mongoose.services.voicemail_identity import canonical_identity
    k1, _ = canonical_identity(account_number="30000000", caller_number="07700900123",
                               voicemail_at="Monday, June 22, 2026 at 3:54:44 AM")
    k2, _ = canonical_identity(account_number="30000000", caller_number="+447700900123",
                               voicemail_at="2026-06-22T03:54:44+01:00")
    assert k1 == k2 and k1.startswith("voicemail:fingerprint:")


def test_different_calls_differ():
    from mongoose.services.voicemail_identity import canonical_identity
    k1, _ = canonical_identity(caller_number="07700900123", voicemail_at="2026-06-22T03:54:44+01:00")
    k2, _ = canonical_identity(caller_number="07999999999", voicemail_at="2026-06-22T03:54:44+01:00")
    assert k1 != k2


def test_recogniser_is_narrow():
    from mongoose.services.voicemail_identity import is_provider_voicemail
    assert is_provider_voicemail(sender="voicemail@voip.example",
                                 subject="New message in mailbox for X from Y")
    assert not is_provider_voicemail(sender="bob@gmail.com", subject="re: a voicemail idea")
    assert not is_provider_voicemail(sender="voicemail@voip.example", subject="account statement")


def test_extract_uid_from_paths_and_urls():
    from mongoose.services.voicemail_identity import extract_uid
    assert extract_uid(f"https://x/delete?uuid={UUID}") == UUID
    assert extract_uid(f"/config/audio/{UUID}.wav") == UUID
    assert extract_uid("vm_" + UUID) == UUID
    assert extract_uid("nothing here") is None


# --- drainer integration: order-independent cross-source dedup ---

def _drainer(monkeypatch):
    _fresh(PROTOCOL_V3_ENABLED="true", PI_SSH_HOST="pi@192.168.1.20")
    import mongoose.services.protocol_v3_runtime as RT
    import mongoose.services.protocol_v3 as P
    monkeypatch.setattr(RT, "transport_status", lambda: {"reachable": True, "configured": True})
    monkeypatch.setattr(P, "_append_to_bus", lambda env: {"ok": True})
    monkeypatch.setattr(RT, "_append_to_bus", lambda env: {"ok": True})
    return RT


def test_drainer_collapses_same_uid_different_msgids(monkeypatch):
    RT = _drainer(monkeypatch)
    vm = "\n".join([
        f'{{"protocol_version":"3","msg_id":"vm_a","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":{{"uid":"{UUID}","caller":"07700900123","wav_rel_path":"/config/{UUID}.wav"}}}}',
        f'{{"protocol_version":"3","msg_id":"vm_b","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":{{"uid":"{UUID}","caller":"07700900123","wav_rel_path":"/config/{UUID}.wav"}}}}',
    ])
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=262144: (True, vm))
    r = RT.drain_voicemail()
    assert r["count"] == 1                 # one canonical voicemail
    assert r["acked"] == 2                 # both observations acked
    assert r["drained"][0]["identity_key"] == f"voicemail:uid:{UUID}"


def test_drainer_stays_collapsed_on_redrain(monkeypatch):
    RT = _drainer(monkeypatch)
    vm = f'{{"protocol_version":"3","msg_id":"vm_a","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":{{"uid":"{UUID}","caller":"07700900123","wav_rel_path":"/config/{UUID}.wav"}}}}'
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=262144: (True, vm))
    assert RT.drain_voicemail()["count"] == 1
    # new msg_id, same identity -> no new row
    vm2 = f'{{"protocol_version":"3","msg_id":"vm_c","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":true,"body":{{"uid":"{UUID}","caller":"07700900123","wav_rel_path":"/config/{UUID}.wav"}}}}'
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=262144: (True, vm2))
    assert RT.drain_voicemail()["count"] == 0


def test_drainer_preserves_source_refs(monkeypatch):
    RT = _drainer(monkeypatch)
    vm = f'{{"protocol_version":"3","msg_id":"vm_a","from":"ha/example_court","to":"mongoose","topic":"example_court.voicemail","needs_ack":false,"body":{{"uid":"{UUID}","caller":"07700900123","transcript":"please call back about flat 9"}}}}'
    monkeypatch.setattr(RT, "_read_remote_file", lambda path, tail_bytes=262144: (True, vm))
    r = RT.drain_voicemail()
    refs = r["drained"][0]["source_refs"]
    assert refs and refs[0]["source"] == "pi_ha"
    assert "flat 9" in refs[0]["transcript"]
