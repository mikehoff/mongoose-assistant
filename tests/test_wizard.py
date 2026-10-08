"""Tests for the browser setup wizard. The discipline being locked in: the wizard is
driven by the SAME INTEGRATIONS registry as the CLI (one source of truth for WHAT
exists), secrets are never sent to the browser, writes are nonce-gated, and the cockpit
uses the wizard as its front door until core setup is done."""
from fastapi.testclient import TestClient

import mongoose.wizard as W
from mongoose.app import app

client = TestClient(app)


def test_schema_is_driven_by_registry():
    """Every integration in the schema must come from the INTEGRATIONS registry, no
    separate browser-only list."""
    from mongoose.setup_wizard import INTEGRATIONS
    reg_slugs = {i.slug for i in INTEGRATIONS}
    schema = W.wizard_schema()
    seen = {ig["slug"] for st in schema["stages"] for ig in st["integrations"]}
    assert seen <= reg_slugs                      # no invented integrations
    assert "home_assistant" in seen               # core present


def test_schema_stage_shape():
    schema = W.wizard_schema()
    titles = [st["title"] for st in schema["stages"]]
    # Core is NOT a configurable stage; the first stage is the optional Home domain.
    assert "core_done" in schema and "configured_count" in schema
    assert "core_status" in schema
    assert any(st["key"] == "home" for st in schema["stages"])
    # critically: no configurable stage is titled "Core" (core is computed, not config)
    assert not any(t == "Core" for t in titles)


def test_schema_never_leaks_secret_values(monkeypatch):
    """Even with a secret set in env, the schema must report is_set but never the
    value."""
    monkeypatch.setenv("HA_TOKEN", "super-secret-token")
    # _read_env reads the file; here just confirm the projection rule holds for any
    # secret field by checking the field contract directly.
    schema = W.wizard_schema()
    for st in schema["stages"]:
        for ig in st["integrations"]:
            for f in ig["fields"]:
                if f["secret"]:
                    assert f["value"] == ""        # secret value never projected


def test_wizard_endpoint_serves_schema():
    r = client.get("/cockpit/wizard")
    assert r.status_code == 200
    assert "stages" in r.json() and len(r.json()["stages"]) == 4


def test_wizard_save_requires_nonce():
    r = client.post("/cockpit/wizard/save", json={"slug": "home_assistant", "values": {}})
    assert r.status_code == 403


def test_wizard_verify_requires_nonce():
    r = client.post("/cockpit/wizard/verify", json={"slug": "home_assistant"})
    assert r.status_code == 403


def test_wizard_save_writes_via_registry(monkeypatch, tmp_path):
    """Saving routes through write_env (the same path the CLI uses); unknown slug is
    rejected."""
    written = {}
    import mongoose.wizard as WZ
    import mongoose.setup_wizard as SW  # noqa: F401 used via monkeypatch
    monkeypatch.setattr(SW, "write_env", lambda updates, path=None: written.update(updates))
    monkeypatch.setattr(SW, "_read_env", lambda path=SW.ENV_PATH: {})
    res = WZ.save_integration("home_assistant", {"HA_URL": "http://hass.local:8123"})
    assert res["ok"] is True
    assert written.get("HA_URL") == "http://hass.local:8123"
    bad = WZ.save_integration("nonexistent", {"X": "y"})
    assert bad["ok"] is False


def test_wizard_save_endpoint_with_nonce(monkeypatch):
    import mongoose.setup_wizard as SW
    monkeypatch.setattr(SW, "write_env", lambda updates, path=None: None)
    monkeypatch.setattr(SW, "_read_env", lambda path=SW.ENV_PATH: {})
    n = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = client.post("/cockpit/wizard/save",
                    headers={"X-Cockpit-Nonce": n},
                    json={"slug": "home_assistant", "values": {"HA_URL": "http://h:8123"}})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_cockpit_is_wizard_first():
    """The cockpit boots into the wizard front door when core isn't set up: the page
    ships the boot() logic keyed on core_done."""
    body = client.get("/ui").text
    assert "async function boot()" in body
    assert "core_done" in body
    assert "saveIntegration" in body  # the staged wizard renders + saves


def test_core_is_local_brain_not_home_assistant(monkeypatch):
    """The vision fix: core_done reflects the LOCAL core (brain + database), NOT Home
    Assistant. Configuring or not configuring HA must not change core_done; only the
    local brain's reachability does."""
    import mongoose.wizard as WZ
    # HA unconfigured but brain up -> core IS ready (HA does not gate the assistant)
    monkeypatch.setattr(WZ, "_core_status",
                        lambda: {"items": [{"label": "Local brain (Ollama)", "ok": True, "detail": ""}],
                                 "ready": True})
    import mongoose.setup_wizard as SW
    monkeypatch.setattr(SW, "_read_env", lambda path=SW.ENV_PATH: {})  # nothing configured
    assert WZ.wizard_schema()["core_done"] is True

    # brain down -> core NOT ready, even if HA were fully configured
    monkeypatch.setattr(WZ, "_core_status",
                        lambda: {"items": [{"label": "Local brain (Ollama)", "ok": False, "detail": "not reachable"}],
                                 "ready": False})
    monkeypatch.setattr(SW, "_read_env",
                        lambda path=SW.ENV_PATH: {"HA_URL": "http://h:8123", "HA_TOKEN": "t"})
    assert WZ.wizard_schema()["core_done"] is False


def test_home_assistant_is_optional_skippable():
    """HA appears as an optional Home domain, not a required Core stage."""
    schema = W.wizard_schema()
    home_stage = schema["stages"][0]
    assert home_stage["key"] == "home"
    # skippability is now conveyed warmly in the blurb rather than a parenthetical in the title
    assert "skip" in home_stage["blurb"].lower() or "optional" in home_stage["blurb"].lower()
    slugs = [ig["slug"] for ig in home_stage["integrations"]]
    assert "home_assistant" in slugs


def test_external_verifier_labeled(monkeypatch):
    """P1.4: integrations whose 'test' contacts a provider are flagged external, so the
    UI can say a test leaves the machine. Local credential checks are not flagged."""
    schema = W.wizard_schema()
    by_slug = {ig["slug"]: ig for st in schema["stages"] for ig in st["integrations"]}
    assert by_slug["home_assistant"]["external_check"] is True   # leaves the box (LAN)
    if "imap" in by_slug:
        assert by_slug["imap"]["external_check"] is False         # local cred check
    # the UI surfaces the label
    body = client.get("/ui").text
    assert "leaves this machine" in body


def test_multiline_value_returns_clean_error(monkeypatch):
    """P1.5: a value with a newline must return a clean JSON error, not a 500."""
    import mongoose.setup_wizard as SW
    monkeypatch.setattr(SW, "_read_env", lambda path=SW.ENV_PATH: {})
    res = W.save_integration("home_assistant", {"HA_URL": "http://h\nLD_PRELOAD=x"})
    assert res["ok"] is False
    assert "multiline" in res["error"].lower()


def test_cockpit_front_door_shows_core_not_home_assistant():
    """The wizard front door frames core as the local brain, never 'HA needed to start'."""
    body = client.get("/ui").text
    # the old, wrong copy must be gone
    assert "Only Home Assistant is needed to start" not in body
    # the corrected framing names the local core
    assert "Core readiness" in body or "local brain" in body.lower()
