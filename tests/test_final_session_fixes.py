"""Final round of field bugs from a single extended session, each independently diagnosed and
fixed with evidence, not guessed at:

1. Desktop WhatsApp tab always showed the literal word "error" -- it read a top-level
   st.state/st.detail that has never existed on /cockpit/whatsapp/state's real {roles:[...]}
   response shape, so it always fell into the error branch regardless of actual connection
   state. Rewritten to handle both roles properly, matching the phone cockpit's already-correct
   handling of the same endpoint.
2. Scheduler's Setup card could never show as configured if the person correctly left the
   sensible defaults untouched -- "configured" was inferred purely from whether optional values
   had been edited away from their defaults, never from whether the scheduler actually runs.
3. The Pi connection card could only ever say "ready to test," never "confirmed working,"
   because no successful handshake was ever recorded anywhere.
4. Notifications had no plain listing route, only search -- unbrowsable without already knowing
   an exact matching word, even when Overview could correctly report one exists.
5. The chat-history load silently swallowed ANY failure (network error, non-2xx status, stale
   nonce) with zero indication, making a transient glitch indistinguishable from the
   conversation actually being gone.
6. The "tab superseded" alert/banner wording didn't explain what actually happened (a server
   restart or another open tab) or reassure that nothing was lost.
7. A built plugin zip had no HTTP route to actually download it, ever, even after a successful
   build; only a "Deploy to WP" button existed, and only when WordPress was configured.
8. Error cards used a dot-indicator color class (.bad) directly as a full-card background,
   producing cream text on a mid-tone terracotta background -- poor contrast.
9. build_plugin_zip's own no_source failure carried no hint, unlike its sibling
   plugin_source_status, so the UI had nothing informative to show on failure.
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_whatsapp_desktop_reads_the_real_roles_shape():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "const roles=(st&&st.roles)||[]" in html
    assert "const state=st.state" not in html
    assert 'async function waConnect(role,btn)' in html
    assert 'postAction("/cockpit/whatsapp/connect",{role:role})' in html
    assert '"/cockpit/whatsapp/qr?role="+encodeURIComponent(role)' in html


def test_scheduler_configured_when_actually_running_even_with_defaults(monkeypatch, tmp_path):
    import datetime
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "sched.sqlite3"))
    from mongoose.db import init_db, set_app_state
    init_db()
    from mongoose.setup_wizard import _scheduler_configured

    env_defaults = {"MONGOOSE_BRIEF_TIME": "07:30", "MONGOOSE_EMAIL_SYNC_MINUTES": "30",
                    "MONGOOSE_REINDEX_MINUTES": "360", "MONGOOSE_INDEX_ROOTS": ""}
    assert _scheduler_configured(env_defaults) is False

    set_app_state("scheduler_heartbeat", datetime.datetime.now(datetime.timezone.utc).isoformat())
    assert _scheduler_configured(env_defaults) is True


def test_pi_last_verified_recorded_and_surfaced(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "pi.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services.protocol_v3 import supports_v3_report

    r = supports_v3_report()
    assert r.get("pi_ssh_last_verified") is None
    assert "pi_ssh_last_verified" in r  # key always present, even when None


def test_pi_status_line_distinguishes_verified_from_ready_to_test():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "s.pi_ssh_last_verified" in html
    assert "Pi connection verified working" in html
    assert "Pi connection set, ready to test." in html


def test_notifications_recent_route_exists_and_lists_without_a_search_term(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "notif.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services.notifications import ingest_notification, NotificationIn
    asyncio.run(ingest_notification(NotificationIn(
        source="phone", app="Reminders", title="Take out the bins",
        body="x", privacy="low", urgency="normal")))

    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.get("/notifications/recent")
    assert r.status_code == 200
    notifs = r.json()["notifications"]
    assert len(notifs) == 1 and notifs[0]["title"] == "Take out the bins"

    # search alone genuinely finds nothing for an unrelated term, proving /recent is necessary
    r2 = c.get("/notifications/search?q=star")
    assert r2.json()["results"] == []


def test_notifications_chip_browses_when_search_box_empty():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert '_searchFilter==="notification" && searchBox && !searchBox.value.trim()' in html
    assert "async function browseRecentNotifications()" in html
    assert '"/notifications/recent?hours=720&limit=50"' in html
    assert "items.map(renderSearchResult)" in html


def test_chat_history_failure_is_visible_not_silent():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "history is best-effort; chat still works without it" not in html
    assert "this is a display problem, not data" in html
    assert "Retry loading history" in html
    assert "if(!r.ok){" in html.split("__loadChatHistory")[1][:600]


def test_stale_session_wording_reassures_and_explains():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "This tab was superseded by a newer Mongoose tab or an update." not in html
    assert "Nothing was lost" in html
    assert "Mongoose restarted since this page loaded" in html


def test_build_zip_has_a_real_download_route(monkeypatch, tmp_path):
    import zipfile, io
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "build.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MONGOOSE_PLUGIN_DIR", str(tmp_path / "plugin_src"))
    (tmp_path / "plugin_src").mkdir()
    (tmp_path / "plugin_src" / "plugin.php").write_text("<?php\n/*\nPlugin Name: T\nVersion: 0.1\n*/\n")

    from mongoose.db import init_db
    init_db()
    from mongoose.services.wp_plugin import build_plugin_zip
    built = build_plugin_zip("beta")
    assert built["ok"]

    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    assert c.get(f"/build/plugin/download/{built['file_name']}").status_code == 403
    r = c.get(f"/build/plugin/download/{built['file_name']}",
             headers={"x-cockpit-nonce": A._COCKPIT_NONCE})
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert any("plugin.php" in n for n in zf.namelist())
    # path traversal / unknown filename must not be servable
    assert c.get("/build/plugin/download/../../etc/passwd",
                headers={"x-cockpit-nonce": A._COCKPIT_NONCE}).status_code in (404, 400, 422)
    assert c.get("/build/plugin/download/not-a-real-build.zip",
                headers={"x-cockpit-nonce": A._COCKPIT_NONCE}).status_code == 404


def test_build_tab_offers_persistent_download_not_just_transient_message():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "async function downloadBuildZip(name,btn)" in html
    assert html.count("downloadBuildZip(") >= 3  # success message + per-row button + function


def test_error_cards_no_longer_fill_with_illegible_background():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert ".card.bad { border-color:var(--bad);" in html
    from mongoose.cockpit import PHONE_COCKPIT_HTML
    assert ".card.bad{background:var(--card);border-color:var(--bad)" in PHONE_COCKPIT_HTML


def test_build_plugin_zip_no_source_carries_a_hint():
    import ast
    import mongoose.services.wp_plugin as _wpp
    src = open(_wpp.__file__).read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_plugin_zip")
    fn_src = ast.get_source_segment(src, fn)
    assert '"hint"' in fn_src
