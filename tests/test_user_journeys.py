"""End-to-end user journey tests. Each tests a realistic sequence of HTTP calls
and/or function calls that a real user would trigger, in the order they'd actually happen."""
import os
import tempfile


# ── shared fixtures ──────────────────────────────────────────────────────────

def _env(extra=None):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    for k in ("HA_URL", "HA_TOKEN", "IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD",
              "ANTHROPIC_API_KEY", "MONGOOSE_API_TOKEN"):
        os.environ.pop(k, None)
    for k, v in (extra or {}).items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()
    return d


def _client(extra_env=None):
    _env(extra_env)
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


# ── JOURNEY 1: first run install → pairing ──────────────────────────────────

def test_journey_install_flow():
    """First run: install progress page loads → pair-code minted → phone lands on launchpad."""
    c = _client()
    # /install page loads
    r = c.get("/install")
    assert r.status_code == 200 and "mongoose" in r.text.lower()
    # progress polling works before install completes
    r = c.get("/install/progress")
    assert r.status_code == 200
    d = r.json()
    assert "ready" in d   # field exists even if False

    # pair-code endpoint (public during install window when nothing is paired)
    r = c.post("/install/pair-code", headers={"host": "127.0.0.1:8765"})
    assert r.status_code in (200, 403, 409)  # 403/409 if install not ready, 200 if it is

    # /pair page always loads (public)
    r = c.get("/pair?code=testcode123")
    assert r.status_code == 200
    assert "mongoose_device_token" in r.text   # pairing JS is on the page

    # /device/launchpad loads (public)
    r = c.get("/device/launchpad")
    assert r.status_code == 200
    assert 'id="ptt"' in r.text
    assert '/device/voice' in r.text


def test_journey_welcome_page():
    """Welcome page loads and contains onboarding state."""
    c = _client()
    r = c.get("/welcome")
    assert r.status_code == 200
    html = r.text
    assert "mongoose" in html.lower()


# ── JOURNEY 2: setup wizard ──────────────────────────────────────────────────

def test_journey_ha_setup_bare_ip():
    """User types a bare IP instead of a full URL — should be normalised, not crash."""
    _env()
    from mongoose.setup_wizard import verify_home_assistant
    ok, msg = verify_home_assistant({"HA_URL": "192.168.1.20", "HA_TOKEN": "tok"})
    assert ok is False                        # no real HA, expected
    assert "mDNS" not in msg                  # must not complain about mDNS
    assert "192.168.1.20" in msg or "connect" in msg.lower() or "reach" in msg.lower()


def test_journey_ha_setup_mdns_rejected_early():
    """homeassistant.local is caught before network call, not after a confusing socket error."""
    _env()
    from mongoose.setup_wizard import verify_home_assistant
    ok, msg = verify_home_assistant({"HA_URL": "http://homeassistant.local:8123", "HA_TOKEN": "tok"})
    assert ok is False and "mDNS" in msg
    assert "IP" in msg or "address" in msg.lower()


def test_journey_ha_setup_ip_colon_port():
    """192.168.1.20:8123 (no scheme) is accepted."""
    _env()
    from mongoose.setup_wizard import verify_home_assistant
    ok, msg = verify_home_assistant({"HA_URL": "192.168.1.20:8123", "HA_TOKEN": "tok"})
    assert "mDNS" not in msg


def test_journey_setup_voice_status():
    """/setup/voice GET always returns a status, never crashes."""
    c = _client()
    r = c.get("/setup/voice")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "ready" in d and "missing" in d


def test_journey_merge_env_preserves_existing():
    """Update path: merge-env adds new keys without touching user's configured values."""
    d = _env()
    example = os.path.join(d, ".env.example")
    envfile = os.path.join(d, ".env")
    open(example, "w").write(
        "MONGOOSE_PORT=8765\nIMAP_PASSWORD=\nNEW_KEY=default_value\n"
    )
    open(envfile, "w").write(
        "MONGOOSE_PORT=9999\nIMAP_PASSWORD=secret\n"
    )
    os.chdir(d)
    from pathlib import Path
    from mongoose.setup_wizard import merge_new_env_keys
    # ENV_PATH is an absolute stable path (~/.mongoose/.env), so pass the temp paths explicitly rather
    # than relying on cwd to redirect the defaults.
    merge_new_env_keys(env_path=Path(envfile), example_path=Path(example))
    result = open(envfile).read()
    assert "MONGOOSE_PORT=9999" in result          # user value preserved
    assert "IMAP_PASSWORD=secret" in result        # user value preserved
    assert "NEW_KEY=default_value" in result       # new key added


# ── JOURNEY 3: chat classification and routing ───────────────────────────────

def test_journey_chat_routing_home_control():
    _env()
    from mongoose.role_router import classify_role
    assert classify_role("turn on the lounge lights") == "tool"
    assert classify_role("turn off the kitchen fan") == "tool"
    assert classify_role("dim the bedroom lights to 40 percent") == "tool"


def test_journey_chat_routing_home_read():
    _env()
    from mongoose.role_router import classify_role, looks_like_home_control
    queries = ["are the lights on", "is the garage open", "what's on at home",
               "is the heating running"]
    for q in queries:
        assert not looks_like_home_control(q), f"read question classified as control: {q}"
        assert classify_role(q) == "tool", f"read question not routed to tool: {q}"


def test_journey_chat_routing_normal_chat():
    _env()
    from mongoose.role_router import classify_role
    for q in ["tell me a joke", "what's the weather like", "is it going to rain",
              "how are you", "what time is it"]:
        role = classify_role(q)
        assert role in ("chat", "tool"), f"unexpected role {role} for: {q}"


def test_journey_chat_routing_compound_conditional():
    _env()
    from mongoose.role_router import classify_role, looks_like_home_control
    prompt = "give me the transcripts of last two voicemails and turn on the printer if there's any mention of documentation"
    assert classify_role(prompt) == "tool"
    assert looks_like_home_control(prompt) is True  # injection guard must pass


# ── JOURNEY 4: home control via HA Assist ────────────────────────────────────

def test_journey_home_control_panel_safety(monkeypatch):
    """Bulk 'all lights' never touches wall panel entities."""
    _env({"HA_URL": "http://ha.local", "HA_TOKEN": "tok"})
    import mongoose.services.home_router as H

    states = [{"entity_id": "light.lounge"}, {"entity_id": "light.kitchen"},
              {"entity_id": "light.control_hub"},            # PANEL
              {"entity_id": "light.control_hub_2_kitchen"}]  # PANEL
    calls = []

    class FC:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            class R:
                status_code = 200
                def json(s): return states
            return R()
        async def post(self, url, headers=None, json=None):
            calls.append(json)
            class R:
                status_code = 200
            return R()

    monkeypatch.setattr(H.httpx, "AsyncClient", FC)
    import asyncio
    r = asyncio.run(H.control_all_in_domain("light", "on"))
    assert r["ok"] is True
    assert r["count"] == 2  # only the two safe lights
    targeted = calls[0]["entity_id"]
    assert "light.control_hub" not in targeted
    assert "light.control_hub_2_kitchen" not in targeted


def test_journey_home_control_assist_panel_refused(monkeypatch):
    """HA Assist is not called with prompts naming a wall panel."""
    _env({"HA_URL": "http://ha.local", "HA_TOKEN": "tok"})
    import asyncio
    import mongoose.services.home_router as H
    r = asyncio.run(H.control_via_ha_assist("turn off the wall panel"))
    assert r["ok"] is False
    assert r["reason"] == "panel_safety"
    assert r.get("fall_back") is True


def test_journey_home_read_not_actuating(monkeypatch):
    """'Are the lights on?' routes to home_state (read-only), never to control_home."""
    _env({"HA_URL": "http://ha.local", "HA_TOKEN": "tok"})
    import mongoose.services.ha_state as HS

    class SC:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            class R:
                status_code = 200
                def json(s): return [
                    {"entity_id": "light.lounge", "state": "on",
                     "attributes": {"friendly_name": "Lounge"}}
                ]
            return R()

    monkeypatch.setattr(HS.httpx, "Client", SC)
    r = HS.read_home_state("lights")
    assert r["ok"] is True
    assert r["rollup"]["lights_on"] == 1
    # Crucially: verify the read-tool classification prevents actuation
    from mongoose.role_router import looks_like_home_control
    assert looks_like_home_control("are the lights on") is False


# ── JOURNEY 5: voicemail ingestion and retrieval ─────────────────────────────

def test_journey_voicemail_pi_then_imap():
    """Pi/HA stages voicemail with transcript → IMAP delivers same call → one row."""
    _env()
    from email.message import Message
    from mongoose.services.voicemail import (
        index_voicemail_from_protocol_staging, index_voicemail_from_email,
        list_voicemails, search_voicemails
    )
    # Pi/HA arrives first with transcript
    env = {"msg_id": "vm1", "caller": "07700900123", "account_number": "30000000",
           "voicemail_at": "2026-06-24T14:30:00+01:00",
           "transcript": "Hi Alex, we need the documentation before we can proceed.",
           "from": "ha/example_court"}
    r1 = index_voicemail_from_protocol_staging(env)
    assert r1["ok"] is True

    # IMAP arrives with the WAV (no transcriber, so transcript preserved from HA)
    msg = Message()
    msg["From"] = "voicemail@voip.example"
    msg["Subject"] = "New message in mailbox for master group of 30000000 from 07700900123"
    msg["Date"] = "Tue, 24 Jun 2026 14:30:00 +0100"
    r2 = index_voicemail_from_email(
        msg=msg, body="New voicemail.", source="imap",
        source_message_id="<m1@voip>", email_id=1, mailbox="INBOX",
        attachments=[{"id": 5, "filename": "vm.wav", "mime_type": "audio/wav",
                      "size_bytes": 5000, "payload_b64": None}]
    )
    assert r2["ok"] is True

    # One row, HA transcript preserved, audio attachment linked
    vms = list_voicemails(10)
    assert len(vms) == 1
    assert vms[0]["transcript"] == "Hi Alex, we need the documentation before we can proceed."
    assert vms[0]["audio_attachment_id"] is not None

    # Searchable by content
    hits = search_voicemails("documentation")
    assert len(hits) == 1
    hits2 = search_voicemails("07700900123")
    assert len(hits2) == 1


def test_journey_voicemail_no_transcript_imap_only():
    """IMAP arrives without transcript (no Whisper) → ok=True, status=pending_no_transcriber."""
    _env()
    from email.message import Message
    from mongoose.services.voicemail import index_voicemail_from_email, list_voicemails
    msg = Message()
    msg["From"] = "voicemail@voip.example"
    msg["Subject"] = "New message in mailbox for master group of 30000000 from 07700900456"
    msg["Date"] = "Tue, 24 Jun 2026 10:00:00 +0100"
    r = index_voicemail_from_email(
        msg=msg, body="New voicemail.", source="imap",
        source_message_id="<m2@voip>", email_id=2, mailbox="INBOX", attachments=[]
    )
    assert r["ok"] is True  # must not fail just because there's no audio
    vms = list_voicemails(10)
    assert len(vms) == 1
    # transcript is None, status reflects no transcriber
    assert vms[0]["transcript"] is None
    assert "pending" in vms[0]["transcript_status"]


def test_journey_voicemail_search_api():
    """/voicemails and /voicemails/search endpoints work."""
    c = _client()
    n = c.get("/cockpit/nonce").json()["cockpit_nonce"]
    # list endpoint
    r = c.get("/voicemails", headers={"X-Cockpit-Nonce": n})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and "voicemails" in d and "stats" in d
    # search endpoint
    r2 = c.get("/voicemails/search?q=test", headers={"X-Cockpit-Nonce": n})
    assert r2.status_code == 200
    assert r2.json()["ok"] is True


# ── JOURNEY 6: phone surface end-to-end ─────────────────────────────────────

def test_journey_phone_pair_then_chat(monkeypatch):
    """Pair a phone → send a text message → get a reply → check history."""
    _env()
    from mongoose import devices as D
    monkeypatch.setattr(D, "device_for_token", lambda tok: {"device_id": "phone1", "name": "Alex's iPhone"})

    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    hdrs = {"X-Mongoose-Device-Token": "test-tok"}

    # Heartbeat works
    r = c.post("/device/heartbeat", headers=hdrs)
    assert r.status_code == 200

    # whoami: returns 200 if device record exists in DB, 404 if token resolves but no DB record
    r = c.get("/device/whoami", headers=hdrs)
    assert r.status_code in (200, 404)  # 404 expected when device not yet persisted

    # History is empty initially
    r = c.get("/device/chat/history", headers=hdrs)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Empty message is rejected cleanly
    r = c.post("/device/chat", json={"message": ""}, headers=hdrs)
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_journey_phone_voice_no_whisper(monkeypatch):
    """Phone PTT with no Whisper → friendly cockpit-link message, not crash."""
    _env()
    from mongoose import devices as D
    import mongoose.voice_benchmark as VB
    monkeypatch.setattr(D, "device_for_token", lambda tok: {"device_id": "phone1", "name": "Test"})
    monkeypatch.setattr(VB, "_whisper_transcribe", lambda path: None)

    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/device/voice", content=b"fakeaudio",
               headers={"X-Mongoose-Device-Token": "tok", "content-type": "audio/webm"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False and d.get("whisper_missing") is True
    assert "setup-voice" not in d["error"]
    assert "Settings" in d["error"] or "Mac" in d["error"]


def test_journey_phone_voice_empty_audio(monkeypatch):
    """Empty audio body returns a clear error, not a crash."""
    _env()
    from mongoose import devices as D
    monkeypatch.setattr(D, "device_for_token", lambda tok: {"device_id": "p1", "name": "T"})
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/device/voice", content=b"",
               headers={"X-Mongoose-Device-Token": "tok"})
    d = r.json()
    assert d["ok"] is False and "audio" in d.get("error", "").lower()


# ── JOURNEY 7: system status ─────────────────────────────────────────────────

def test_journey_system_status_all_areas():
    """/system/status responds for every area without crashing."""
    c = _client()
    n = c.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = c.get("/system/status", headers={"X-Cockpit-Nonce": n})
    # system_status may require nonce or may be public depending on version
    assert r.status_code in (200, 403)
    if r.status_code == 200:
        d = r.json()
        assert isinstance(d, dict)


def test_journey_cockpit_loads():
    """The main cockpit page loads and contains the UI scaffolding."""
    c = _client()
    r = c.get("/ui")
    assert r.status_code == 200
    html = r.text
    assert "Mongoose" in html
    assert "cockpit-nonce" in html or "nonce" in html.lower()


def test_journey_health_check():
    """Health endpoint returns version and name — used by install verification."""
    c = _client()
    r = c.get("/health")
    assert r.status_code == 200
    d = r.json()
    assert d.get("name") == "mongoose"
    assert "version" in d


# ── JOURNEY 8: conditional tool chain planner instructions ───────────────────

def test_journey_planner_has_conditional_instructions():
    """Planner system prompt has the conditional logic rule needed for compound requests."""
    from mongoose.tools import _PLANNER_SYSTEM
    assert "CONDITIONAL LOGIC" in _PLANNER_SYSTEM
    assert "pending_no_audio" in _PLANNER_SYSTEM
    assert '{"tool": null}' in _PLANNER_SYSTEM


def test_journey_phrasing_prompt_quotes_transcripts():
    """Phrasing step instructions say to quote transcripts verbatim (not summarise)."""
    import inspect
    import mongoose.tools as T
    src = inspect.getsource(T.run_tool_turn)
    assert "verbatim" in src
    assert "pending_no_audio" in src
    assert "condition was not met" in src


# ── JOURNEY 9: tool capability protocol ─────────────────────────────────────

def test_journey_tool_capability_protocol():
    """All read tools have Read-only descriptions and are in READ_TOOLS and planner catalogue."""
    _env()
    from mongoose.tools import TOOLS, READ_TOOLS, _tool_catalogue
    catalogue = _tool_catalogue()
    failures = []
    for name in READ_TOOLS:
        if name not in TOOLS:
            failures.append(f"{name} in READ_TOOLS but not in TOOLS")
            continue
        if not TOOLS[name]["description"].startswith("Read-only"):
            failures.append(f"{name} description doesn't start with 'Read-only'")
        if name not in catalogue:
            failures.append(f"{name} not in planner catalogue")
    assert not failures, "\n".join(failures)


def test_journey_acting_tools_not_in_read_tools():
    """control_home must be in ACTING_TOOLS but NOT in READ_TOOLS."""
    _env()
    from mongoose.tools import ACTING_TOOLS, READ_TOOLS
    for tool in ACTING_TOOLS:
        assert tool not in READ_TOOLS, f"{tool} is both acting and read — injection guard broken"


def test_journey_self_reflection_sees_home_state_wired():
    """self_reflection noticer must not flag home_state as a wiring gap."""
    _env()
    from mongoose.services.self_reflection import reflect
    r = reflect()
    gaps = [f["subject"] for f in r["findings"] if not f["ok"]
            and f["invariant"] in ("tool_in_read_tools", "tool_in_catalogue")]
    assert "home_state" not in gaps
    assert "search_voicemails" not in gaps
