"""Local voicemail transcriber provisioning + the ha_staging transcript ingest/merge.

Covers the two follow-up builds after the "transcripts were dead on arrival" fix:
  - transcriber_status()/start_transcriber_install(): one-click provisioning of whisper.cpp.
  - stage_transcript(): the long-designed 'ha_staging' source, fed by the Home Assistant
    Example Court add-on, matched by canonical identity and merged with local Whisper.
"""
import os
import tempfile


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
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


VM_AT = "Monday, June 22, 2026 at 3:54:44 AM"


# --- Build A: transcriber provisioning ---------------------------------------------------------

def test_transcriber_status_shape():
    _fresh()
    from mongoose.services.model_install import transcriber_status
    st = transcriber_status()
    for key in ("ready", "binary", "ffmpeg", "model", "model_path", "detail"):
        assert key in st, f"missing key {key}"
    assert isinstance(st["ready"], bool)


def test_start_transcriber_install_short_circuits_when_ready(monkeypatch):
    _fresh()
    import mongoose.services.model_install as MI
    MI._PROGRESS.pop("transcriber", None)
    monkeypatch.setattr(MI, "transcriber_status", lambda: {"ready": True})
    res = MI.start_transcriber_install()
    assert res["ok"] and res.get("already_installed") is True


def test_transcriber_status_reports_reason_when_missing(monkeypatch):
    _fresh()
    import mongoose.services.model_install as MI
    # Force the "nothing installed" path: no binary, no ffmpeg, no model, no auto cmd.
    monkeypatch.setattr(MI, "_run_transcriber_install", lambda: None)
    import mongoose.services.voicemail as V
    monkeypatch.setattr(V, "_which", lambda b: None)
    monkeypatch.setattr(V, "_resolve_ggml_model", lambda m: None)
    monkeypatch.setattr(V, "_auto_transcribe_cmd", lambda: None)
    st = MI.transcriber_status()
    assert st["ready"] is False
    assert st["detail"] == "no local transcriber installed"


# --- Build B: ha_staging ingest + merge --------------------------------------------------------

def test_stage_transcript_creates_row_when_none_exists():
    _fresh()
    from mongoose.services.voicemail import stage_transcript, list_voicemails
    res = stage_transcript(transcript="Hello this is a staged test message.",
                           provider_uid="uuidA", caller_number="07700900123", voicemail_at=VM_AT)
    assert res["ok"] and res["action"] == "insert"
    vms = list_voicemails(limit=10)
    assert any((v.get("transcript") or "").startswith("Hello this is a staged") for v in vms)
    assert res["transcript_source"] == "ha_staging"


def test_stage_transcript_matches_existing_identity_no_duplicate(monkeypatch):
    _fresh()
    import mongoose.services.voicemail as V
    # Deterministic merge (no LLM/network in tests): keep the existing transcript.
    monkeypatch.setattr(V, "judge_transcripts",
                        lambda a, sa, b, sb: {"winner": "a", "chosen": a, "reason": "test", "diff": None})
    k = dict(provider_uid="uuidB", caller_number="07000000000", voicemail_at=VM_AT)
    V.stage_transcript(transcript="First version of the message.", **k)
    V.stage_transcript(transcript="Second slightly different version.", **k)
    matching = [v for v in V.list_voicemails(limit=10) if v.get("caller_number") == "07000000000"]
    assert len(matching) == 1, "same identity must collapse to one voicemail row"


def test_ha_staging_beats_local_whisper(monkeypatch):
    # HA staging is OpenAI gpt-4o transcription: it must deterministically WIN over local base.en
    # (local_command) when they differ , without relying on the local LLM judge, which has picked the
    # weaker transcript before. This is the fix for The owner's "the HA transcripts were better" complaint.
    _fresh()
    import mongoose.services.voicemail as V
    from mongoose.services.voicemail import (
        _upsert_voicemail, canonical_identity_key, _source_specific_dedupe_key, stage_transcript, list_voicemails)
    # Make the judge blow up if called , across tiers it must NOT be consulted.
    monkeypatch.setattr(V, "judge_transcripts",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("judge must not run across tiers")))
    ik, cu = canonical_identity_key(provider_uid="uuidC", caller_number="07111111111", voicemail_at=VM_AT)
    dk = _source_specific_dedupe_key("imap", "<mid>", "f.wav", "07111111111", VM_AT)
    _upsert_voicemail(identity_key=ik, canonical_uid=cu, dedupe_key=dk, ref={"kind": "imap"}, source="imap",
                      source_message_id="<mid>", provider="voip_provider", mailbox="INBOX", mailbox_label=None,
                      account_number=None, caller_number="07111111111", subject="s", notice_text="n",
                      voicemail_at=VM_AT, email_date=None, audio_attachment_id=None, audio_filename="f.wav",
                      audio_mime_type=None, audio_size_bytes=None, remote_audio_path=None, manage_url=None,
                      delete_url=None, new_message_count=None, old_message_count=None,
                      transcript="Local whisper says the pipes are leaking.", transcript_status="done",
                      transcript_source="local_command", privacy="medium", raw={})
    stage_transcript(transcript="HA gpt-4o says the boiler has failed.",
                     provider_uid="uuidC", caller_number="07111111111", voicemail_at=VM_AT)
    rows = [v for v in list_voicemails(limit=10) if v.get("caller_number") == "07111111111"]
    assert len(rows) == 1
    assert "HA gpt-4o" in (rows[0].get("transcript") or ""), "HA (gpt-4o) transcript must win over local base.en"
    assert "ha_staging" in (rows[0].get("transcript_source") or "")


def test_stage_transcript_rejects_empty_and_nonspeech():
    _fresh()
    from mongoose.services.voicemail import stage_transcript
    assert stage_transcript(transcript="")["ok"] is False
    assert stage_transcript(transcript="   ")["ok"] is False
    # Pure non-lexical markers collapse to nothing -> treated as no speech.
    assert stage_transcript(transcript="(silence) (silence) (silence)")["ok"] is False
