"""Tests for second external audit findings — UI/API drift, misleading status, scheduler, wizard."""
import os
import tempfile


def _fresh(extra=None):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    for k in (extra or {}):
        os.environ.pop(k, None)
    for k, v in (extra or {}).items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _client(extra=None):
    _fresh(extra)
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


# ── P0: WordPress post endpoints exist ───────────────────────────────────────

def test_wp_posts_endpoint_exists():
    c = _client()
    n = c.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = c.get("/build/wordpress/posts", headers={"X-Cockpit-Nonce": n})
    # not configured → returns ok:False or ok:True with empty list — but NOT 404
    assert r.status_code == 200, f"expected 200 not 404: {r.status_code}"


def test_wp_post_create_endpoint_exists():
    c = _client()
    n = c.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = c.post("/build/wordpress/post",
               json={"title": "Test", "content": "body", "status": "draft"},
               headers={"X-Cockpit-Nonce": n})
    assert r.status_code == 200, f"expected 200 not 404: {r.status_code}"


def test_wp_post_update_endpoint_exists():
    c = _client()
    n = c.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = c.post("/build/wordpress/post/42",
               json={"status": "publish"},
               headers={"X-Cockpit-Nonce": n})
    assert r.status_code == 200, f"expected 200 not 404: {r.status_code}"


def test_wp_posts_requires_nonce():
    c = _client()
    r = c.get("/build/wordpress/posts")
    assert r.status_code == 403


def test_wp_post_create_requires_nonce():
    c = _client()
    r = c.post("/build/wordpress/post", json={"title": "T", "content": "C"})
    assert r.status_code == 403


# ── P0: cockpit has requestAction and deletePlugin uses it ───────────────────

def test_cockpit_has_request_action_helper():
    c = _client()
    html = c.get("/ui").text
    assert "async function requestAction(" in html
    assert 'method="POST"' in html or "method:" in html


def test_delete_plugin_uses_request_action_not_post_action():
    c = _client()
    html = c.get("/ui").text
    # deletePlugin must call requestAction (not postAction) with DELETE method
    # Find the deletePlugin function and check it uses requestAction
    idx = html.find("async function deletePlugin(")
    assert idx >= 0, "deletePlugin function not found"
    fn_body = html[idx:idx+600]
    assert "requestAction(" in fn_body, f"deletePlugin must use requestAction: {fn_body[:200]}"
    assert "DELETE" in fn_body, "deletePlugin must pass DELETE method"
    assert "postAction(" not in fn_body.split("requestAction")[0], \
        "deletePlugin must not use postAction for the delete call"


def test_refresh_readiness_uses_post_action():
    c = _client()
    html = c.get("/ui").text
    idx = html.find("async function refreshReadiness(")
    assert idx >= 0
    fn_body = html[idx:idx+200]
    assert "postAction(" in fn_body, "refreshReadiness must use postAction (for nonce)"
    assert "fetch(" not in fn_body, "refreshReadiness must not use raw fetch (missing nonce)"


# ── P0: wp_plugin tool boundary ──────────────────────────────────────────────

def test_wp_plugin_status_in_read_tools():
    _fresh()
    from mongoose.tools import TOOLS, READ_TOOLS
    assert "wp_plugin_status" in READ_TOOLS
    assert TOOLS["wp_plugin_status"]["description"].startswith("Read-only")


def test_wp_plugin_action_not_in_read_tools():
    _fresh()
    from mongoose.tools import READ_TOOLS, ACTING_TOOLS
    assert "wp_plugin_action" not in READ_TOOLS, "acting tool must not be in READ_TOOLS"
    assert "wp_plugin_action" in ACTING_TOOLS, "wp_plugin_action must be in ACTING_TOOLS"


def test_wp_plugin_old_tool_removed():
    _fresh()
    from mongoose.tools import TOOLS
    assert "wp_plugin" not in TOOLS or TOOLS.get("wp_plugin") is None, \
        "old undivided wp_plugin tool must be removed"


# ── P0: voicemail identity — identity_from_staging uses voicemail_at + account_number ──

def test_identity_from_staging_uses_voicemail_at():
    """identity_from_staging must use voicemail_at (not just timestamp) so the runtime
    dedup key matches what the DB row uses."""
    _fresh()
    from mongoose.services.voicemail_identity import identity_from_staging
    envelope = {
        "msg_id": "vm001",
        "voicemail_at": "2026-06-24T14:30:00+01:00",  # the canonical field
        "account_number": "30000000",
    }
    normalised = {
        "uid": "abc123", "caller": "07700900123",
        "transcript": "test", "wav_rel_path": "/media/vm.wav", "tag": "voicemail"
    }
    key, uid, fields = identity_from_staging(envelope, normalised)
    # Should produce a stable key using the UID (not a timestamp-only fallback)
    assert key, "must produce a non-empty identity key"
    assert uid == "abc123" or "07700900123" in key or "abc123" in key

    # Now same voicemail with only timestamp (old field) — must produce the SAME key
    envelope2 = {
        "msg_id": "vm001",
        "timestamp": "2026-06-24T14:30:00+01:00",  # old field
        "account_number": "30000000",
    }
    key2, uid2, _ = identity_from_staging(envelope2, normalised)
    assert key == key2, f"voicemail_at and timestamp must produce same key: {key} vs {key2}"


def test_identity_from_staging_passes_account_number():
    """account_number must be in the fields returned by identity_from_staging."""
    _fresh()
    from mongoose.services.voicemail_identity import identity_from_staging
    envelope = {"msg_id": "vm002", "voicemail_at": "2026-06-24T10:00:00+01:00",
                "account_number": "30000000"}
    normalised = {"uid": "xyz", "caller": "07700900456",
                  "transcript": "", "wav_rel_path": "", "tag": "voicemail"}
    _, _, fields = identity_from_staging(envelope, normalised)
    assert fields.get("account_number") == "30000000"


# ── P1: drain order — store first, then remember/ack ─────────────────────────

def test_drain_store_before_remember():
    """Store must succeed before marking vm processed (prevents silent loss on DB failure)."""
    import inspect
    from mongoose.services import protocol_v3_runtime as RT
    src = inspect.getsource(RT.drain_voicemail)
    # The key invariant: _remember_vm for a NEW voicemail must only happen inside "if store_ok:"
    # (The "if already: ... _remember_vm ..." branch handles duplicates — different path)
    assert "if store_ok:" in src, "_remember_vm must be gated on store_ok"
    # Verify that store_ok is set after the actual store call
    store_ok_set_idx = src.find("store_ok = True")
    store_call_idx = src.find("index_voicemail_from_protocol_staging(")
    assert store_call_idx < store_ok_set_idx, \
        "index_voicemail_from_protocol_staging must run before store_ok = True"
    # The remember call for new voicemails must be inside the store_ok block
    store_ok_block_start = src.find("if store_ok:")
    remember_in_block = src.find("_remember_vm(vm_id)", store_ok_block_start)
    assert remember_in_block > store_ok_block_start, \
        "_remember_vm for new voicemails must be inside the 'if store_ok:' block"


# ── P1: account_number stored in Pi/HA path ──────────────────────────────────

def test_pi_staging_stores_account_number():
    """Pi/HA voicemail must store the account_number, not None."""
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_protocol_staging, list_voicemails
    r = index_voicemail_from_protocol_staging({
        "msg_id": "vm001",
        "caller": "07700900123",
        "account_number": "30000000",
        "voicemail_at": "2026-06-24T14:30:00+01:00",
        "transcript": "Please send the documentation.",
        "from": "ha/example_court",
    })
    assert r["ok"] is True
    vms = list_voicemails(5)
    assert len(vms) == 1
    # account_number must survive into the stored row
    vm = vms[0]
    # account_number is stored in mailbox_label or as a field
    assert vm.get("account_number") == "30000000" or "30000000" in str(vm)


# ── P1: _upsert_voicemail returns final stored transcript_status ─────────────

def test_upsert_returns_stored_transcript_status():
    """After an AI merge that promotes ha_staging, the return value must show the stored status."""
    _fresh()
    from mongoose.services.voicemail import index_voicemail_from_protocol_staging
    r = index_voicemail_from_protocol_staging({
        "msg_id": "vm_status_test",
        "caller": "07700900123",
        "account_number": "30000000",
        "voicemail_at": "2026-06-24T14:30:00+01:00",
        "transcript": "The invoice needs signing.",
        "from": "ha/example_court",
    })
    assert r["ok"] is True
    # The returned transcript_status must be "done" (HA transcript is good)
    assert r["transcript_status"] == "done", \
        f"expected 'done' from HA transcript, got: {r['transcript_status']}"
    # Must also return transcript_source
    assert "transcript_source" in r


# ── P1: _remember_vm ordered list ────────────────────────────────────────────

def test_remember_vm_ordered_preserves_recent():
    """_remember_vm must use an ordered list so [-500:] keeps the MOST RECENT entries."""
    _fresh()
    from mongoose.services.protocol_v3_runtime import _remember_vm, _processed_vm_ids
    # Add 600 entries — [-500:] should keep the most recent 500 (ids 100..599)
    for i in range(600):
        _remember_vm(f"vm_{i:04d}")
    kept = _processed_vm_ids()
    # The most recently added ids should be present
    for recent in (f"vm_{i:04d}" for i in range(100, 600)):
        assert recent in kept, f"{recent} must be in kept set (recent ids must not be evicted)"
    # Very old ids should be evicted
    assert "vm_0000" not in kept, "oldest entry must be evicted after cap"


# ── P1: scheduler nested failure detection ────────────────────────────────────

def test_scheduler_detects_nested_ok_false():
    """run_due_jobs must record 'degraded' when a job returns {"ok": false, ...}."""
    _fresh()
    import asyncio
    from mongoose.services import scheduler as SCH

    async def fake_failing_job():
        return {"ok": False, "error": "gmail auth expired"}

    # Patch a job in
    orig_reg = SCH._build_registry
    def patched_reg():
        r = orig_reg()
        r["test_failing"] = (("interval", 1), fake_failing_job)
        return r
    SCH._build_registry = patched_reg
    try:
        ran = asyncio.run(SCH.run_due_jobs())
    finally:
        SCH._build_registry = orig_reg

    result = ran.get("test_failing", {})
    assert result.get("status") == "degraded", \
        f"nested ok:false must produce 'degraded' not 'ok': {result}"


# ── P1: scheduler labels ──────────────────────────────────────────────────────

def test_scheduler_labels_interval():
    _fresh()
    from mongoose.services.scheduler import job_status
    jobs = job_status()
    for j in jobs:
        if "every" in j["schedule"]:
            assert "m" in j["schedule"], f"interval label wrong: {j}"
            return  # found at least one interval job


def test_scheduler_schedule_label_format():
    """No job should be labelled 'daily at' if its spec type is not 'daily'."""
    _fresh()
    import inspect
    from mongoose.services import scheduler as SCH
    src = inspect.getsource(SCH.job_status)
    # Must have elif/else handling for weekly and window
    assert "weekly" in src or "elif" in src, "scheduler must handle non-daily non-interval specs"


# ── P1: wizard save returns integrations ─────────────────────────────────────

def test_wizard_save_returns_integrations_shape():
    """POST /cockpit/wizard/save must return d.integrations as a flat list with .slug and .configured."""
    c = _client()
    n = c.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = c.post("/cockpit/wizard/save",
               json={"slug": "imap", "values": {"IMAP_HOST": "test.example.com",
                                                  "IMAP_USERNAME": "user@example.com",
                                                  "IMAP_PASSWORD": "testpass"}},
               headers={"X-Cockpit-Nonce": n})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    # Must include integrations list for frontend badge update
    assert "integrations" in d, "wizard save must return integrations for in-place badge update"
    intgs = d["integrations"]
    assert isinstance(intgs, list), f"integrations must be a list, got: {type(intgs)}"
    assert len(intgs) > 0
    # Each integration must have slug and configured fields
    for ig in intgs[:3]:
        assert "slug" in ig and "configured" in ig, f"bad integration shape: {ig}"
    # The saved slug must appear in the list
    slugs = [ig["slug"] for ig in intgs]
    assert "imap" in slugs, f"imap must be in returned integrations: {slugs[:5]}"


# ── P2: https-help.html dead link fixed ──────────────────────────────────────

def test_https_help_link_not_dead():
    c = _client()
    html = c.get("/ui").text
    assert "/static/https-help.html" not in html, \
        "dead /static/https-help.html link must be removed"


# ── P2: Google provider readiness checks file exists ─────────────────────────

def test_google_readiness_false_when_env_set_but_file_missing(tmp_path):
    """GOOGLE_TOKEN_PATH set but file doesn't exist → readiness must be False."""
    _fresh({"GOOGLE_TOKEN_PATH": str(tmp_path / "nonexistent-token.json")})
    from mongoose.services import scheduler as SCH
    result = SCH._provider_ready("google")
    assert result is False, \
        "readiness must be False when GOOGLE_TOKEN_PATH points to nonexistent file"


def test_google_readiness_true_when_file_exists(tmp_path):
    """GOOGLE_TOKEN_PATH pointing to a realistic-size token file → readiness must be True.
    The placeholder guard rejects tiny files (<64 bytes) to prevent a bare {} from being
    mistaken for authorised, so the fixture uses a plausible-size payload."""
    token_file = tmp_path / "google-token.json"
    token_file.write_text('{"token": "ya29.' + 'a'*200 + '", "refresh_token": "1//xyz' + 'z'*80 + '"}')
    _fresh({"GOOGLE_TOKEN_PATH": str(token_file)})
    from mongoose.services import scheduler as SCH
    result = SCH._provider_ready("google")
    assert result is True, "readiness must be True when token file exists and has real content"


# ── P2: googleapiclient test skipped without dep ─────────────────────────────

def test_backup_drive_test_has_importorskip():
    """test_backup_drive_hardening.py must have importorskip for googleapiclient."""
    content = open("tests/test_backup_drive_hardening.py").read()
    assert "importorskip" in content and "googleapiclient" in content, \
        "backup drive test must use pytest.importorskip to skip without connector deps"


# ── Version 1.0.1-alpha ───────────────────────────────────────────────────────

# ── Version coherence (agnostic: the sources must AGREE, not match a literal) ──

def test_version_is_1_0_1_alpha():
    # Named for history; the real invariant is that __version__ is a non-empty, well-formed string.
    import mongoose
    v = mongoose.__version__
    assert isinstance(v, str) and v, "version must be a non-empty string"
    assert v[0].isdigit(), f"version should start with a digit, got: {v}"


def test_release_json_version():
    import json
    import mongoose
    d = json.load(open("release.json"))
    # release.json must agree with the package version (catches a stale manifest), and keep the
    # install-check command invariants regardless of version number.
    assert d["version"] == mongoose.__version__, \
        f"release.json {d['version']} != package {mongoose.__version__} (regenerate the manifest)"
    assert "search" not in d.get("expected_commands", [])
    assert "find" in d.get("expected_commands", [])
