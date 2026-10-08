import re


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_INPROCESS_SCHEDULER", "false")
    monkeypatch.setenv("MONGOOSE_RUNTIME_WATCHDOG", "false")
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()


def test_rajdhani_css_route_is_local_fast_fallback(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setenv("MONGOOSE_FONT_PROVISIONING", "off")
    from fastapi.testclient import TestClient
    from mongoose.app import app

    r = TestClient(app, raise_server_exceptions=False).get("/static/fonts/rajdhani.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert "font-family:'Rajdhani'" in r.text
    assert "fonts.gstatic.com" not in r.text
    assert "no-cache" in r.headers.get("cache-control", "")


def test_rajdhani_provisioner_rewrites_remote_font_urls_to_local_cache(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setenv("MONGOOSE_FONT_PROVISIONING", "auto")
    import mongoose.font_assets as F

    css = """
    @font-face{font-family:'Rajdhani';font-style:normal;font-weight:600;
    src:url(https://fonts.gstatic.com/s/rajdhani/v99/example-latin.woff2) format('woff2');}
    """
    monkeypatch.setattr(F, "_fetch_text", lambda url, *, timeout: css)
    monkeypatch.setattr(F, "_fetch_bytes", lambda url, *, timeout: b"fake-woff2")

    res = F.provision_rajdhani(timeout=3, force=True)
    assert res["ok"] is True
    cached = F.cached_rajdhani_css()
    assert cached is not None
    assert "fonts.gstatic.com" not in cached
    assert "/static/fonts/provisioned/rajdhani/" in cached
    name = re.search(r"/static/fonts/provisioned/rajdhani/([^)]*?\.woff2)", cached).group(1)
    path = F.resolve_rajdhani_file(name)
    assert path is not None
    assert path.read_bytes() == b"fake-woff2"


def test_guest_invite_page_loads_rajdhani_css_and_uses_it_for_display(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.cockpit import guest_invite_html

    html = guest_invite_html("token", {"ok": True, "server_version": "1.1"})
    assert 'href="/static/fonts/rajdhani.css"' in html
    assert 'font-family:"Rajdhani","Inter",system-ui,sans-serif' in html
    assert "GuestDisplay" not in html
    assert "You\u2019re invited to join Mongoose" in html


def test_guest_invite_records_display_font_status_without_blocking(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setenv("MONGOOSE_FONT_PROVISIONING", "off")
    from mongoose.guest_invites import create_guest_invite

    res = create_guest_invite(owner_label="Jordan", base_url="https://yourname.duckdns.org:8765")
    assert res["ok"] is True
    assert res["display_font"]["family"] == "Rajdhani"
    assert res["display_font"]["source"] == "disabled"
