from pathlib import Path


def test_wizard_defaults_are_not_counted_as_configured(tmp_path, monkeypatch):
    """Copied defaults are scaffolding, not evidence that the user configured a source."""
    import mongoose.setup_wizard as SW
    monkeypatch.chdir(tmp_path)
    saved = SW.ENV_PATH
    SW.ENV_PATH = Path(".env")
    try:
        Path(".env").write_text(
            "MONGOOSE_HOST=127.0.0.1\n"
            "MONGOOSE_ALLOW_REMOTE=false\n"
            "NTFY_URL=http://127.0.0.1:8090\n"
            "NTFY_TOPIC=mongoose-private-topic\n"
            "MS_TENANT_ID=common\n"
            "GOOGLE_CLIENT_SECRETS_PATH=./secrets/google-client-secrets.json\n"
        )
        from mongoose.wizard import wizard_schema
        wiz = wizard_schema()
        flat = {ig["slug"]: ig for st in wiz["stages"] for ig in st["integrations"]}
        assert flat["remote"]["configured"] is False
        assert flat["push"]["configured"] is False
        assert flat["google_calendar"]["configured"] is False
    finally:
        SW.ENV_PATH = saved


def test_integration_status_does_not_run_verifiers_by_default(monkeypatch, tmp_path):
    """A status poll must not send test pushes, call APIs, or turn a verifier exception into a 500."""
    import mongoose.setup_wizard as SW
    monkeypatch.chdir(tmp_path)
    saved = SW.ENV_PATH
    SW.ENV_PATH = Path(".env")
    try:
        Path(".env").write_text("HA_URL=http://127.0.0.1:8123\nHA_TOKEN=tok\n")
        ha = next(i for i in SW.INTEGRATIONS if i.slug == "home_assistant")
        original = ha.verifier
        calls = []
        def boom(env):
            calls.append(env)
            raise RuntimeError("should not run on status")
        ha.verifier = boom
        try:
            status = {s["slug"]: s for s in SW.integration_status()}
        finally:
            ha.verifier = original
        assert calls == []
        assert status["home_assistant"]["configured"] is True
        assert status["home_assistant"]["verification"] is None
    finally:
        SW.ENV_PATH = saved


def test_cockpit_wizard_degrades_to_200(monkeypatch):
    import mongoose.wizard as W
    from fastapi.testclient import TestClient
    from mongoose.app import app

    monkeypatch.setattr(W, "wizard_schema", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r = TestClient(app).get("/cockpit/wizard")
    assert r.status_code == 200
    body = r.json()
    assert body["stages"] == []
    assert "boom" in body["_error"]


def test_cross_site_browser_write_to_localhost_is_rejected():
    """CORS is not a write barrier; browser-origin writes must be same-origin even on localhost."""
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    r = c.post("/memory", json={"text": "csrf"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "browser-origin" in r.json()["message"]


def test_same_origin_browser_write_reaches_route_layer():
    """The origin guard must not block legitimate same-origin cockpit/API calls."""
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    r = c.post("/memory", json={"text": "ok"}, headers={"Origin": "http://testserver"})
    assert r.status_code != 403


def test_setup_tab_no_dead_end_message():
    import mongoose.cockpit as C
    assert "Setup unavailable" not in C.COCKPIT_HTML
    assert "Setup loaded in repair mode" in C.COCKPIT_HTML


def test_legacy_setup_page_routes_google_calendar_to_google_oauth_provider():
    """The old /setup page still exists; its Google consent button must use the provider route, not the integration slug."""
    from mongoose.app import setup_page
    html = setup_page()
    assert "authorize('google', 'google_calendar')" in html
    assert "/setup/authorize/' + provider" in html
    assert "data.integrations.find" not in html
