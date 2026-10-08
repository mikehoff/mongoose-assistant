from pathlib import Path


def test_local_probe_prefers_https_by_default_and_http_when_tls_off(monkeypatch):
    from mongoose.local_probe import local_base_candidates, preferred_local_base
    monkeypatch.delenv("MONGOOSE_TLS", raising=False)
    assert local_base_candidates(8765)[0] == "https://127.0.0.1:8765"
    assert preferred_local_base(8765).startswith("https://")
    monkeypatch.setenv("MONGOOSE_TLS", "off")
    assert local_base_candidates(8765)[0] == "http://127.0.0.1:8765"
    assert local_base_candidates(8765)[1] == "https://127.0.0.1:8765"


def test_installer_health_checks_are_not_plain_http_only():
    repo = Path(__file__).resolve().parents[1]
    boot = (repo / "scripts" / "bootstrap_macos.sh").read_text()
    assert "mongoose_local_probe.sh" in boot
    assert "mongoose_curl \"/health\"" in boot
    assert 'curl --connect-timeout 1 --max-time 2 -fsS "http://127.0.0.1:$PORT/health"' not in boot
    assert 'open "http://127.0.0.1:$PORT/install"' not in boot


def test_double_click_launchers_use_scheme_aware_probe():
    repo = Path(__file__).resolve().parents[1]
    for name in ["Install Mongoose.command", "Start Mongoose.command", "Stop Mongoose.command", "Mongoose Status.command"]:
        src = (repo / name).read_text()
        assert "mongoose_local_probe.sh" in src, name
        assert 'http://127.0.0.1:$PORT/health' not in src, name


def test_smoke_test_accepts_https_default_self_signed_localhost():
    repo = Path(__file__).resolve().parents[1]
    src = (repo / "scripts" / "smoke_test.sh").read_text()
    assert "mongoose_local_probe.sh" in src
    assert "smoke_curl \"/health\"" in src
    assert "curl -fsS \"$BASE_URL/health\"" not in src
    assert "-fsSk" in src or "mongoose_curl" in src


def test_external_watchdog_probe_is_https_aware():
    import inspect
    from mongoose.services import server_watchdog
    src = inspect.getsource(server_watchdog.probe_live)
    assert "local_base_candidates" in src
    assert "verify=False" in src
    assert "/health/live" in src


def test_user_facing_urls_never_emit_127_or_0_0_0_0(monkeypatch):
    from mongoose import access_urls as U
    monkeypatch.delenv("MONGOOSE_TLS", raising=False)
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    base = U.user_facing_base(8765, host="0.0.0.0")
    assert base.startswith("https://")
    assert "127.0.0.1" not in base
    assert "0.0.0.0" not in base
    assert U.local_browser_base(8765) == "https://127.0.0.1:8765"


def test_installer_proves_install_page_not_just_health_before_success():
    repo = Path(__file__).resolve().parents[1]
    boot = (repo / "scripts" / "bootstrap_macos.sh").read_text()
    assert 'mongoose_page_ready "/install"' in boot
    assert "API health passed but /install web UI did not render" in boot
    launcher = (repo / "Install Mongoose.command").read_text()
    assert 'mongoose_page_ready "/install"' in launcher
    assert "Mongoose answered /health, but the install web UI did not load" in launcher


def test_launchers_open_human_urls_not_internal_probe_urls():
    repo = Path(__file__).resolve().parents[1]
    helper = (repo / "scripts" / "mongoose_local_probe.sh").read_text()
    assert "mongoose_human_base" in helper
    assert "mongoose_lan_ip" in helper
    assert "open \"$url\"" in helper
    assert 'printf \'%s://localhost:%s' in helper
    start = (repo / "Start Mongoose.command").read_text()
    status = (repo / "Mongoose Status.command").read_text()
    # Owner cockpit opens on explicit loopback (mongoose_open_local -> 127.0.0.1), and the
    # separate phone/LAN launchpad URL is printed only when it differs from the owner base.
    assert 'mongoose_open_local "/ui"' in start
    assert 'Phone/LAN URL after pairing' in start
    assert 'mongoose_local_base' in status
    assert 'Phone/LAN URL after pairing' in status
    assert 'mongoose_probe_base || mongoose_preferred_base' not in status
