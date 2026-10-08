"""RTM build journey: WordPress plugin workflow, build/deploy pipeline, cockpit Build tab."""
import os
import tempfile


def _fresh(extra=None):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    for k in ("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD"):
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


# ── WordPress connector (auth/connectivity) ──────────────────────────────────

def test_wp_status_not_configured():
    _fresh()
    from mongoose.connectors.wordpress import wp_status
    r = wp_status()
    assert r["ok"] is False and r["configured"] is False


def test_wp_status_configured_but_unreachable():
    _fresh({"WP_URL": "http://not-a-real-wordpress.invalid",
            "WP_USERNAME": "alex", "WP_APP_PASSWORD": "xxxx xxxx"})
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.connectors.wordpress import wp_status
    r = wp_status()
    assert r["ok"] is False and r["configured"] is True
    assert "unreachable" in r.get("reason", "").lower() or "connect" in r.get("reason", "").lower()


# ── plugin service unit tests ────────────────────────────────────────────────

def test_plugin_source_status_no_source():
    os.environ.pop("MONGOOSE_PLUGIN_DIR", None)
    _fresh()
    from mongoose.services.wp_plugin import plugin_source_status
    r = plugin_source_status()
    assert r["ok"] is False and r["reason"] == "no_source"
    assert "hint" in r


def test_plugin_source_status_with_source():
    tmp_d = tempfile.mkdtemp()
    _fresh()
    plugin_dir = os.path.join(tmp_d, "my-rtm-plugin")
    os.makedirs(plugin_dir)
    with open(os.path.join(plugin_dir, "my-rtm-plugin.php"), "w") as f:
        f.write("<?php\n/*\nPlugin Name: My RTM Plugin\nVersion: 1.0.0\n*/\n")
    with open(os.path.join(plugin_dir, "index.js"), "w") as f:
        f.write("console.log('hello');")
    os.environ["MONGOOSE_PLUGIN_DIR"] = plugin_dir
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.services.wp_plugin import plugin_source_status
    r = plugin_source_status()
    assert r["ok"] is True
    assert r["version"] == "1.0.0"
    assert r["main_file"] == "my-rtm-plugin.php"
    assert r["file_count"] == 2


def test_build_plugin_zip_from_source():
    import zipfile
    tmp_d = tempfile.mkdtemp()
    _fresh()
    plugin_dir = os.path.join(tmp_d, "example-court-plugin")
    os.makedirs(plugin_dir)
    with open(os.path.join(plugin_dir, "example-court-plugin.php"), "w") as f:
        f.write("<?php\n/*\nPlugin Name: Example Court\nVersion: 0.1.0\n*/\n")
    with open(os.path.join(plugin_dir, "workflow.js"), "w") as f:
        f.write("// workflow\n")
    os.environ["MONGOOSE_PLUGIN_DIR"] = plugin_dir
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.services.wp_plugin import build_plugin_zip
    r = build_plugin_zip("beta")
    assert r["ok"] is True
    assert r["file_name"].endswith(".zip")
    assert "beta" in r["file_name"]
    assert r["size_bytes"] > 0
    # verify zip contains the plugin files
    with zipfile.ZipFile(r["zip_path"]) as z:
        names = z.namelist()
    assert any("example-court-plugin.php" in n for n in names)
    assert any("workflow.js" in n for n in names)


def test_build_plugin_zip_no_source():
    os.environ.pop("MONGOOSE_PLUGIN_DIR", None)
    _fresh()
    from mongoose.services.wp_plugin import build_plugin_zip
    r = build_plugin_zip("beta")
    assert r["ok"] is False and r["reason"] == "no_source"


def test_list_builds_empty():
    _fresh()
    from mongoose.services.wp_plugin import list_builds
    assert list_builds() == []


def test_wp_upload_plugin_not_configured():
    _fresh()
    from mongoose.services.wp_plugin import wp_upload_plugin
    r = wp_upload_plugin("/tmp/nonexistent.zip")
    assert r["ok"] is False


def test_wp_set_plugin_status_not_configured():
    _fresh()
    from mongoose.services.wp_plugin import wp_set_plugin_status
    r = wp_set_plugin_status("my-plugin/my-plugin.php", "active")
    assert r["ok"] is False and r["reason"] == "not_configured"


def test_wp_set_plugin_status_bad_status():
    _fresh()
    from mongoose.services.wp_plugin import wp_set_plugin_status
    r = wp_set_plugin_status("my-plugin/my-plugin.php", "running")
    assert r["ok"] is False and "status" in r["reason"]


def test_wp_upload_plugin_mocked(monkeypatch):
    tmp_d = tempfile.mkdtemp()
    _fresh({"WP_URL": "https://example.com", "WP_USERNAME": "alex",
                "WP_APP_PASSWORD": "test pass"})
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    # create a real zip to upload
    import zipfile
    zp = os.path.join(tmp_d, "plugin.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("my-plugin/my-plugin.php", "<?php // Plugin Name: Test\n")

    import httpx
    import mongoose.services.wp_plugin as WP

    class FakeResp:
        status_code = 201
        def json(self): return {"plugin": "my-plugin/my-plugin.php", "name": "Test",
                                "version": "1.0", "status": "inactive"}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())
    r = WP.wp_upload_plugin(zp)
    assert r["ok"] is True and r["status"] == "inactive"


def test_promote_plugin_mocked(monkeypatch):
    _fresh({"WP_URL": "https://example.com", "WP_USERNAME": "alex",
            "WP_APP_PASSWORD": "test pass"})
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import httpx
    import mongoose.services.wp_plugin as WP
    calls = []

    class FakeResp:
        status_code = 200
        def json(self): return {"plugin": calls[-1], "status": "active" if len(calls)==1 else "inactive"}

    def fake_put(url, **k):
        slug = url.split("/plugins/")[1]
        calls.append(slug)
        return FakeResp()

    monkeypatch.setattr(httpx, "put", fake_put)
    r = WP.promote_plugin("my-plugin-beta/main.php", "my-plugin/main.php")
    assert r["ok"] is True
    assert len(calls) == 2  # activate beta, deactivate prod


# ── build API endpoints ──────────────────────────────────────────────────────

def test_build_status_endpoint():
    c = _client()
    r = c.get("/build/status")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and "version" in d and "recent_addenda" in d


def test_build_status_has_addenda():
    c = _client()   # _fresh() chdirs into a fresh temp dir
    # The release tree does not ship docs/MERGE_NOTES.md, so provide a minimal one in the
    # working directory (the endpoint looks there first) to exercise the addenda parser.
    os.makedirs("docs", exist_ok=True)
    with open(os.path.join("docs", "MERGE_NOTES.md"), "w", encoding="utf-8") as f:
        f.write("# Merge notes\n\n### v1.0.0 addendum 1: first change\nSummary line.\n\n"
                "### v1.0.1 addendum 2: second change\nAnother summary.\n")
    d = c.get("/build/status").json()
    assert len(d["recent_addenda"]) >= 1
    assert d["recent_addenda"][0]["title"].startswith("v1.0.1 addendum 2")


def test_build_stage_requires_nonce():
    c = _client()
    assert c.post("/build/stage", json={"url": "https://x.com/a.zip"}).status_code == 403


def test_build_deploy_requires_nonce():
    c = _client()
    assert c.post("/build/deploy", json={"url": "https://x.com/a.zip"}).status_code == 403


def test_build_stage_no_source_returns_error():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/build/stage", json={}, headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    d = r.json()
    assert d["ok"] is False
    assert "url" in d["error"].lower() or "source" in d["error"].lower()


def test_plugin_source_endpoint():
    c = _client()
    r = c.get("/build/plugin/source")
    assert r.status_code == 200
    d = r.json()
    assert "ok" in d


def test_plugin_build_requires_nonce():
    c = _client()
    assert c.post("/build/plugin/build", json={}).status_code == 403


def test_plugin_deploy_beta_requires_nonce():
    c = _client()
    assert c.post("/build/plugin/deploy-beta", json={}).status_code == 403


def test_plugin_activate_requires_nonce():
    c = _client()
    assert c.post("/build/plugin/activate", json={}).status_code == 403


def test_plugin_promote_requires_nonce():
    c = _client()
    assert c.post("/build/plugin/promote", json={}).status_code == 403


def test_plugin_builds_list():
    c = _client()
    r = c.get("/build/plugin/builds")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_wp_status_endpoint_is_public():
    c = _client()
    r = c.get("/build/wordpress/status")
    assert r.status_code == 200
    d = r.json()
    assert "connection" in d and "source" in d and "builds" in d


def test_plugin_build_via_api():
    """Full build journey: source dir → build zip via API."""
    import zipfile
    tmp_d = tempfile.mkdtemp()
    _fresh()
    plugin_dir = os.path.join(tmp_d, "rtm-workflow")
    os.makedirs(plugin_dir)
    with open(os.path.join(plugin_dir, "rtm-workflow.php"), "w") as f:
        f.write("<?php\n/*\nPlugin Name: RTM Workflow\nVersion: 0.1.0-beta\n*/\n")
    os.environ["MONGOOSE_PLUGIN_DIR"] = plugin_dir
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/build/plugin/build", json={"version_suffix": "beta"},
               headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    d_resp = r.json()
    assert d_resp["ok"] is True
    assert d_resp["file_name"].endswith(".zip")
    # verify it's a real zip
    with zipfile.ZipFile(d_resp["zip_path"]) as z:
        assert any("rtm-workflow.php" in n for n in z.namelist())
    # builds list now shows it
    r2 = c.get("/build/plugin/builds")
    assert any("rtm-workflow" in b["name"] for b in r2.json()["builds"])


# ── cockpit Build tab ────────────────────────────────────────────────────────

def test_cockpit_has_build_tab():
    c = _client()
    html = c.get("/ui").text
    assert "loadBuild" in html
    assert "build" in html


def test_build_tab_plugin_workflow_ui():
    c = _client()
    html = c.get("/ui").text
    assert "buildPlugin" in html          # build zip button
    assert "deployBeta" in html           # deploy beta button
    assert "activatePlugin" in html       # activate/deactivate
    assert "promotePlugin" in html        # promote beta → production
    assert "stageAndDeploy" in html       # Mongoose self-deploy
    assert "WordPress" in html            # WP section present


# ── wp_plugin chat tool ──────────────────────────────────────────────────────

def test_wp_plugin_tool_in_catalogue():
    _fresh()
    from mongoose.tools import TOOLS, READ_TOOLS, _tool_catalogue
    assert "wp_plugin_status" in TOOLS
    assert "wp_plugin_status" in READ_TOOLS
    assert "wp_plugin_status" in _tool_catalogue()
    assert TOOLS["wp_plugin_status"]["description"].startswith("Read-only")
    assert "wp_plugin_action" in TOOLS  # acting tool


def test_wp_plugin_tool_status():
    _fresh()
    import asyncio
    from mongoose.tools import TOOLS
    r = asyncio.run(TOOLS["wp_plugin_status"]["executor"]({"action": "status"}))
    assert "connection" in r or "ok" in r


def test_wp_plugin_tool_source():
    _fresh()
    import asyncio
    from mongoose.tools import TOOLS
    r = asyncio.run(TOOLS["wp_plugin_status"]["executor"]({"action": "source"}))
    assert "ok" in r


def test_wp_plugin_tool_builds():
    _fresh()
    import asyncio
    from mongoose.tools import TOOLS
    r = asyncio.run(TOOLS["wp_plugin_status"]["executor"]({"action": "builds"}))
    assert r["ok"] is True and "builds" in r


def test_wp_settings_in_env_example():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # this checkout (repo root of the test file)
    text = open(os.path.join(repo, ".env.example")).read()
    assert "WP_URL" in text
    assert "WP_APP_PASSWORD" in text
    assert "MONGOOSE_PLUGIN_DIR" in text


# ── new: multipart upload, redeploy, scaffold ─────────────────────────────────

def test_wp_upload_uses_multipart(monkeypatch):
    """Plugin upload must use multipart/form-data (WP REST API requirement), not raw binary."""
    import zipfile
    import httpx
    import mongoose.services.wp_plugin as WP

    tmp_d = tempfile.mkdtemp()
    _fresh({"WP_URL": "https://example.com", "WP_USERNAME": "alex",
            "WP_APP_PASSWORD": "test pass"})
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()

    zp = os.path.join(tmp_d, "my-plugin.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("my-plugin/my-plugin.php", "<?php // Plugin Name: Test\n")

    captured = {}
    class FakeResp:
        status_code = 201
        def json(self): return {"plugin": "my-plugin/my-plugin.php",
                                "name": "Test", "version": "1.0", "status": "inactive"}

    def fake_post(url, files=None, **k):
        captured["files"] = files
        captured["kwargs"] = k
        return FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    WP.wp_upload_plugin(zp)

    # Must have used multipart files= not raw content=
    assert "files" in captured, "upload must use multipart files= parameter"
    assert captured["files"] is not None
    assert "pluginzip" in captured["files"], \
        f"multipart field must be 'pluginzip', got keys: {list(captured['files'].keys())}"
    # Must not have sent raw Content-Type: application/zip
    assert "content" not in captured.get("kwargs", {}), \
        "upload must not send raw binary content= (wrong format for WP REST API)"


def test_wp_upload_redeploy_replaces_existing(monkeypatch):
    """Redeploying a plugin that's already installed must auto-delete and re-upload."""
    import zipfile, httpx
    import mongoose.services.wp_plugin as WP

    tmp_d = tempfile.mkdtemp()
    _fresh({"WP_URL": "https://example.com", "WP_USERNAME": "alex",
            "WP_APP_PASSWORD": "test pass"})
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()

    zp = os.path.join(tmp_d, "example-court-rtm.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("example-court-rtm/example-court-rtm.php", "<?php // Plugin Name: Example Court RTM\n")

    call_seq = []

    def fake_post(url, **k):
        call_seq.append(("POST", url))
        is_retry = len([c for c in call_seq if c[0]=="POST"]) > 1
        class R:
            status_code = 201 if is_retry else 400
            text = '{"message":"Plugin already installed."}' if not is_retry else "{}"
            def json(self):
                if self.status_code == 400:
                    return {"message": "Plugin already installed."}
                return {"plugin": "example-court-rtm/example-court-rtm.php",
                        "name": "Example Court RTM", "version": "0.2.0", "status": "inactive"}
        return R()

    def fake_get(url, **k):
        call_seq.append(("GET", url))
        class R:
            status_code = 200
            def json(self):
                return [{"plugin": "example-court-rtm/example-court-rtm.php",
                         "name": "Example Court RTM", "version": "0.1.0", "status": "inactive"}]
        return R()

    def fake_put(url, **k):
        call_seq.append(("PUT", url))
        class R:
            status_code = 200
            def json(self): return {"plugin": url.split("/plugins/")[1], "status": "inactive"}
        return R()

    def fake_delete(url, **k):
        call_seq.append(("DELETE", url))
        class R:
            status_code = 200
            def json(self): return {"deleted": True}
        return R()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "put", fake_put)
    monkeypatch.setattr(httpx, "delete", fake_delete)

    r = WP.wp_upload_plugin(zp, overwrite=True)
    assert r["ok"] is True, f"redeploy should succeed: {r}"
    assert r.get("replaced") is not None, "result must record what was replaced"
    # Verify it actually deleted and re-uploaded
    methods = [c[0] for c in call_seq]
    assert "DELETE" in methods, "redeploy must delete the old plugin"
    assert methods.count("POST") >= 2, "redeploy must re-upload after deleting"


def test_scaffold_creates_example_court_plugin():
    """scaffold_example_court_plugin must create a working starter plugin."""
    tmp_d = tempfile.mkdtemp()
    _fresh()
    os.environ["MONGOOSE_PLUGIN_DIR"] = os.path.join(tmp_d, "example-court-rtm")

    from mongoose.services.wp_plugin import scaffold_example_court_plugin, plugin_source_status
    r = scaffold_example_court_plugin("0.1.0")
    assert r["ok"] is True
    assert len(r["files_created"]) >= 2  # at least .php and readme

    # Must be a detectable valid plugin
    status = plugin_source_status()
    assert status["ok"] is True
    assert status["version"] == "0.1.0"
    assert status["main_file"] is not None
    assert "Example Court" in open(
        os.path.join(status["path"], status["main_file"])).read()


def test_scaffold_refuses_non_empty_directory():
    tmp_d = tempfile.mkdtemp()
    _fresh()
    plugin_dir = os.path.join(tmp_d, "existing-plugin")
    os.makedirs(plugin_dir)
    open(os.path.join(plugin_dir, "existing.php"), "w").write("<?php\n")
    os.environ["MONGOOSE_PLUGIN_DIR"] = plugin_dir

    from mongoose.services.wp_plugin import scaffold_example_court_plugin
    r = scaffold_example_court_plugin()
    assert r["ok"] is False
    assert "not_empty" in r["reason"]


def test_scaffold_endpoint_requires_nonce():
    c = _client()
    assert c.post("/build/plugin/scaffold", json={}).status_code == 403


def test_scaffold_endpoint_creates_plugin():
    """Scaffold endpoint must create the plugin source when directory is empty."""
    tmp_d = tempfile.mkdtemp()
    _fresh()
    plugin_dir = os.path.join(tmp_d, "new-plugin")
    os.environ["MONGOOSE_PLUGIN_DIR"] = plugin_dir
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/build/plugin/scaffold", json={"version": "0.1.0"},
               headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert len(d["files_created"]) >= 2


def test_wp_plugin_tool_scaffold_action():
    tmp_d = tempfile.mkdtemp()
    _fresh()
    os.environ["MONGOOSE_PLUGIN_DIR"] = os.path.join(tmp_d, "pc-rtm")
    import asyncio
    from mongoose.tools import TOOLS
    r = asyncio.run(TOOLS["wp_plugin_action"]["executor"]({"action": "scaffold"}))
    assert r["ok"] is True


def test_wp_plugin_tool_status_includes_next_steps():
    """Brain context: status response must include actionable next_steps."""
    _fresh()
    import asyncio
    from mongoose.tools import TOOLS
    r = asyncio.run(TOOLS["wp_plugin_status"]["executor"]({"action": "status"}))
    assert "next_steps" in r
    assert isinstance(r["next_steps"], list)
    assert len(r["next_steps"]) > 0


def test_cockpit_build_tab_has_scaffold_button():
    c = _client()
    html = c.get("/ui").text
    assert "scaffoldPlugin" in html
    assert "Scaffold" in html
