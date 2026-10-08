"""Regression tests for the v0.52.7 UX/functional fixes from real install feedback:
HA .local DNS message, core-ready timeout/gating, liveness false-positives, search self-description,
scheduler descriptions+health, pairing reachability. These are the substantive (non-cosmetic) fixes."""
import os
import tempfile



def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


# ---- HA verifier gives actionable .local / auth / refused messages -------------

def test_ha_verifier_explains_local_dns(monkeypatch):
    _fresh()
    import httpx
    from mongoose import setup_wizard as SW

    def boom(*a, **k):
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
    monkeypatch.setattr(httpx, "get", boom)
    ok, msg = SW.verify_home_assistant({"HA_URL": "http://homeassistant.local:8123", "HA_TOKEN": "x"})
    assert ok is False
    assert "ip address" in msg.lower()  # tells the user to use the IP
    assert "homeassistant.local" in msg or "mDNS" in msg or "resolve" in msg.lower()


def test_ha_verifier_explains_bad_token(monkeypatch):
    _fresh()
    import httpx
    from mongoose import setup_wizard as SW

    class _R:
        status_code = 401
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    ok, msg = SW.verify_home_assistant({"HA_URL": "http://192.168.1.5:8123", "HA_TOKEN": "bad"})
    assert ok is False
    assert "token" in msg.lower()


# ---- core readiness: reachable + chat model present => ready (not gated on tool) ----

def test_core_ready_when_reachable_even_if_tool_model_missing(monkeypatch):
    _fresh()
    import mongoose.doctor as DOC
    import mongoose.wizard as W
    monkeypatch.setattr(DOC, "run_doctor", lambda timeout_seconds=2.0: {"checks": {
        "ollama": {"ok": False, "detail": "Ollama reachable; pull missing role models",
                   "present": {"chat": True, "tool": False}, "error": None},
        "database": {"ok": True}}})
    assert W._core_status()["ready"] is True


def test_core_not_ready_when_ollama_down(monkeypatch):
    _fresh()
    import mongoose.doctor as DOC
    import mongoose.wizard as W
    monkeypatch.setattr(DOC, "run_doctor", lambda timeout_seconds=2.0: {"checks": {
        "ollama": {"ok": False, "detail": "Ollama not reachable", "error": "refused"},
        "database": {"ok": True}}})
    assert W._core_status()["ready"] is False


# ---- liveness: files/scheduler are UNUSED on a fresh install, not "stale" ----

def test_liveness_files_and_scheduler_not_falsely_stale():
    _fresh()
    os.environ.pop("MONGOOSE_INDEX_ROOTS", None)
    from mongoose.liveness import liveness
    rep = liveness()
    checks = rep.get("checks", []) if isinstance(rep, dict) else rep
    by = {c["subsystem"]: c for c in checks}
    # both should read as not-configured/unused, never STALE "configured but no data yet"
    assert by["files"]["detail"] == "not set up"
    assert by["scheduler"]["detail"] == "not set up"
    assert by["files"]["state"] != "stale"
    assert by["scheduler"]["state"] != "stale"


def test_liveness_files_configured_when_roots_set():
    _fresh()
    os.environ["MONGOOSE_INDEX_ROOTS"] = "/tmp/somewhere"
    try:
        from mongoose.liveness import liveness
        rep = liveness()
        checks = rep.get("checks", []) if isinstance(rep, dict) else rep
        files = {c["subsystem"]: c for c in checks}["files"]
        # now configured, so with no data it's allowed to say "no data yet" (it IS set up)
        assert files["detail"] != "not set up"
    finally:
        os.environ.pop("MONGOOSE_INDEX_ROOTS", None)


# ---- search describes what it searched + empty-index state ----

def test_find_reports_sources_and_empty_index():
    _fresh()
    from mongoose.services.find import find
    r = find("anything")
    assert "searched" in r and set(r["searched"]) >= {"file", "email"}
    assert r["empty_index"] is True
    assert r["indexed_count"] == 0


# ---- scheduler: jobs carry descriptions; running status is reported ----

def test_scheduler_jobs_have_descriptions_and_running_flag():
    _fresh()
    from mongoose.services.scheduler import job_status, _scheduler_running
    jobs = job_status()
    assert isinstance(jobs, list)
    # at least the known jobs should have non-empty descriptions
    described = [j for j in jobs if j.get("description")]
    assert described, "expected at least some jobs to have human descriptions"
    # fresh install: nothing has run, so not running
    assert _scheduler_running() is False


# ---- pairing reachability note fires when server is loopback-only ----

def test_pairing_warns_when_not_lan_reachable(monkeypatch):
    _fresh()
    import mongoose.app as APP
    # default host 127.0.0.1 + allow_remote false => not reachable => warning present.
    # _pairing_reachability_note() returns a sentinel string; the JS renders the actual
    # "Enable phone access" button — no terminal instructions in the return value.
    note = APP._pairing_reachability_note()
    assert note is not None
    # The sentinel is "not_lan_reachable" — JS interprets and renders the action button
    assert "not_lan_reachable" in note or len(note) > 0


def test_pairing_no_warning_when_lan_reachable(monkeypatch):
    """CORRECTED (this used to assert no-warning from config alone, with no real socket
    listening anywhere: exactly the false assumption that let a stale loopback-bound process
    render a confident QR while the phone got ERR_CONNECTION_REFUSED). Config being right is
    necessary but not sufficient; genuine live reachability must ALSO hold for 'no warning'
    to be correct, so this now mocks the live socket check succeeding too."""
    _fresh()
    import mongoose.app as APP

    class _S:
        host = "0.0.0.0"
        allow_remote = True
        port = 8765
    monkeypatch.setattr(APP, "get_settings", lambda: _S())
    monkeypatch.setattr(APP, "_lan_socket_reachable", lambda settings, timeout=0.75: True)
    assert APP._lan_reachable_now() is True
    assert APP._pairing_reachability_note() is None


def test_pairing_warns_when_config_says_ready_but_socket_is_not_listening(monkeypatch):
    """The adversarial case the old test was missing: config alone must never be enough."""
    _fresh()
    import mongoose.app as APP

    class _S:
        host = "0.0.0.0"
        allow_remote = True
        port = 8765
    monkeypatch.setattr(APP, "get_settings", lambda: _S())
    monkeypatch.setattr(APP, "_lan_socket_reachable", lambda settings, timeout=0.75: False)
    assert APP._lan_reachable_now() is False
    assert APP._pairing_reachability_note() is not None


# ---- /install page can't be served stale: no-store header + version stamp ----

def test_install_page_is_no_store_and_version_stamped():
    """The /install page is the most likely to be reopened across installs, so it must be served
    no-store (a cached copy is the 'I opened a previous /install' bug) and must show its version so
    a stale page is visibly identifiable."""
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    from mongoose import __version__
    c = TestClient(app)
    r = c.get("/install")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc
    # version is stamped both as a data attribute (for the stale-detector) and visibly
    assert f'data-version="{__version__}"' in r.text
    assert f"Mongoose v{__version__}" in r.text
    # the page carries the client-side stale detector
    assert "checkFresh" in r.text


def test_install_html_has_no_unfilled_version_placeholder():
    _fresh()
    from mongoose.cockpit import install_html
    assert "__MONGOOSE_VERSION__" not in install_html()
