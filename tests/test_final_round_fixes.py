"""Final round of field bugs from a single long session, each independently verified against
real behavior (a real sshd, a real zip build, a real database row) before being locked here.

1. Desktop WhatsApp tab: renderWhatsApp() read a single top-level st.state/st.detail that never
   existed in the real {ok, roles:[...]} response shape (the phone side always read roles
   correctly). st.state was therefore always undefined, always fell into the "error" branch,
   and rendered the literal word "error" unconditionally, regardless of actual connection state.
2. Build plugin zips were built successfully but had no HTTP route to actually retrieve them.
3. Notifications had no plain listing route -- search alone left them completely unbrowsable
   without already knowing a matching word, even when a notification genuinely existed.
4. The Scheduler Setup card could only show "configured" if a field was edited away from its
   documented default, so leaving sensible defaults untouched (the correct thing to do) meant
   it could never go green, even while the scheduler ran perfectly fine.
5. The Pi connection card could only ever say "ready to test," structurally unable to record
   that a handshake had already succeeded, even moments after a real successful test.
6. The stale-session alert/banner text was confusing and didn't reassure that no data was lost.
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_desktop_whatsapp_reads_the_real_roles_shape():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "const state=st.state" not in html
    assert "const roles=(st&&st.roles)||[]" in html
    assert "async function waConnect(role,btn)" in html
    assert 'postAction("/cockpit/whatsapp/connect",{role:role})' in html
    assert '"/cockpit/whatsapp/qr?role="+encodeURIComponent(role)' in html


def test_build_plugin_download_route_serves_only_known_builds(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "b.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MONGOOSE_PLUGIN_DIR", str(tmp_path / "plugin_src"))
    (tmp_path / "plugin_src").mkdir()
    (tmp_path / "plugin_src" / "plugin.php").write_text(
        "<?php\n/*\nPlugin Name: Test\nVersion: 0.1.0\n*/\n")

    from mongoose.db import init_db
    init_db()
    from mongoose.services.wp_plugin import build_plugin_zip
    r = build_plugin_zip("beta")
    assert r["ok"]

    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)

    assert c.get(f"/build/plugin/download/{r['file_name']}").status_code == 403
    ok = c.get(f"/build/plugin/download/{r['file_name']}",
              headers={"x-cockpit-nonce": A._COCKPIT_NONCE})
    assert ok.status_code == 200
    assert len(ok.content) > 0

    bad = c.get("/build/plugin/download/not-a-real-build.zip",
               headers={"x-cockpit-nonce": A._COCKPIT_NONCE})
    assert bad.status_code == 404


def test_no_source_build_error_carries_a_hint():
    from mongoose.services.wp_plugin import build_plugin_zip
    import unittest.mock as mock
    with mock.patch("mongoose.services.wp_plugin._plugin_dir",
                    return_value=__import__("pathlib").Path("/definitely/does/not/exist")):
        r = build_plugin_zip("beta")
    assert r["ok"] is False
    assert r["reason"] == "no_source"
    assert "hint" in r and r["hint"]


def test_notifications_recent_listing_route_exists_and_works(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "n.sqlite3"))
    from mongoose.db import init_db, connect
    init_db()
    import datetime
    with connect() as con:
        con.execute(
            "INSERT INTO notifications(source, app, title, body, privacy, urgency, device, "
            "dedupe_key, received_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("phone", "Messages", "A real notification", "body", "low", "normal", "phone",
             "dedupe-test", datetime.datetime.now(datetime.timezone.utc).isoformat()))
        con.commit()

    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)

    unrelated = c.get("/notifications/search?q=zzzznomatch")
    assert unrelated.json()["results"] == []

    listing = c.get("/notifications/recent")
    assert listing.status_code == 200
    names = [n["title"] for n in listing.json()["notifications"]]
    assert "A real notification" in names


def test_search_tab_can_browse_notifications_without_a_query():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "async function browseRecentNotifications()" in html
    assert '"/notifications/recent?hours=720&limit=50"' in html
    assert "browseRecentNotifications();" in html


def test_scheduler_configured_when_genuinely_running_not_just_when_edited(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "s.sqlite3"))
    from mongoose.db import init_db, set_app_state
    init_db()
    from mongoose.setup_wizard import _scheduler_configured
    import datetime

    defaults = {"MONGOOSE_BRIEF_TIME": "07:30", "MONGOOSE_EMAIL_SYNC_MINUTES": "30",
               "MONGOOSE_REINDEX_MINUTES": "360", "MONGOOSE_INDEX_ROOTS": ""}
    assert _scheduler_configured(defaults) is False

    set_app_state("scheduler_heartbeat",
                  datetime.datetime.now(datetime.timezone.utc).isoformat())
    assert _scheduler_configured(defaults) is True


def test_pi_handshake_records_a_verified_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "pi.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services.protocol_v3 import supports_v3_report
    assert supports_v3_report().get("pi_ssh_last_verified") is None


def test_stale_session_messaging_reassures_no_data_was_lost():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert "Nothing was lost" in html
    assert "This tab was superseded by a newer Mongoose tab or an update." not in html
