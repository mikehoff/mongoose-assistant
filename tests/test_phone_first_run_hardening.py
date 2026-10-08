"""First-run phone/PWA/network/voice hardening regression tests.

Adapted from the Pro hardening spec to this codebase's actual endpoint names: the device manifest
is /device/manifest.webmanifest, push handlers live in the shared /sw.js, and the device PWA name is
"Mongoose". The substance of the spec (bounded fetches, offline outbox, voice MIME detection,
off-loop Whisper with timeouts, truthful notification UX, accessible PTT) is what these lock in."""
import inspect
import json
import os
import tempfile


def _client():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False)


# ── Phone PWA shell ──────────────────────────────────────────────────────────────────────────

def test_device_manifest_is_phone_shell_not_owner_cockpit():
    c = _client()
    m = json.loads(c.get("/device/manifest.webmanifest").content)
    assert m["display"] == "standalone"
    assert m["start_url"] == "/device/launchpad"
    assert m["scope"] == "/device/"


def test_service_worker_has_push_and_notification_handlers():
    c = _client()
    sw = c.get("/sw.js").text
    assert "/device/launchpad" in sw          # caches the phone shell
    assert "notificationclick" in sw
    assert "showNotification" in sw
    assert "addEventListener('push'" in sw


def test_launchpad_wires_pwa_resilience_features():
    c = _client()
    html = c.get("/device/launchpad").text
    assert "/device/manifest.webmanifest" in html   # links its own manifest
    assert "/sw.js" in html                          # registers the service worker
    assert "navigator.onLine" in html                # network awareness
    assert "AbortController" in html                 # bounded fetches
    assert "mongoose_device_outbox_v1" in html       # offline outbox
    assert "MediaRecorder.isTypeSupported" in html   # cross-browser voice MIME
    assert "X-Audio-Mime" in html                    # declares the MIME to the server


def test_launchpad_offline_outbox_queues_and_flushes():
    c = _client()
    html = c.get("/device/launchpad").text
    # the outbox primitives must all be present
    assert "function getOutbox(" in html
    assert "function queueText(" in html
    assert "async function flushOutbox(" in html
    # flush must be wired to the reconnect path and to boot
    assert "flushOutbox()" in html


def test_launchpad_ptt_is_pointer_and_keyboard_accessible():
    c = _client()
    html = c.get("/device/launchpad").text
    assert "pointerdown" in html, "PTT should use pointer events (mouse+touch+pen)"
    # keyboard Space/Enter support on the PTT button
    assert "keydown" in html and ("e.key===' '" in html or 'e.key==="Enter"' in html or "e.key==='Enter'" in html)
    # tabs are real buttons with tab roles
    assert 'role="tab"' in html
    assert 'aria-selected' in html


# ── Voice endpoint hardening ─────────────────────────────────────────────────────────────────

def test_device_voice_is_off_event_loop_and_timeout_bounded():
    import mongoose.app as A
    src = inspect.getsource(A.device_voice_route)
    assert "to_thread" in src, "Whisper must run off the event loop"
    assert "wait_for" in src, "transcription/brain must be timeout-bounded"
    assert "MONGOOSE_DEVICE_VOICE_STT_TIMEOUT_S" in src
    assert "MONGOOSE_DEVICE_VOICE_LLM_TIMEOUT_S" in src
    assert "x-audio-mime" in src.lower(), "server must honour the declared audio MIME"


def test_device_voice_picks_extension_from_mime():
    """The server must map common phone MIME types to sensible extensions (not always .webm)."""
    import mongoose.app as A
    src = inspect.getsource(A.device_voice_route)
    # iOS Safari commonly produces mp4/aac; these must be handled
    assert "audio/mp4" in src
    assert "audio/aac" in src
    assert '"m4a"' in src or "'m4a'" in src
