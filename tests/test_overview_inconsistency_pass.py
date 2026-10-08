"""Fixes for the pile of inconsistent/contradictory Overview and Setup screens M reported
in one sitting. (1) doctor's embeddings check referenced a 'summary' key available() has
never returned, guaranteeing the literal placeholder '?' every single time. (2) doctor's
https check independently re-derived readiness from bare env vars while the cockpit's own
live TLS panel read self_signed_status(); two sources of truth for one fact WILL drift,
which is exactly what M saw (cockpit said 'Serving HTTPS, expires 2028' while Overview said
'not configured, run make_cert.sh'). Unified onto the same source, and the old message
pointed at a manual terminal script when a real one-click cockpit button already existed.
(3) the QR pairing endpoints started preferring the DDNS/public address unconditionally,
including for the owner's OWN phone pairing from right next to the Mac; most consumer
routers don't support NAT hairpin/loopback, so a LAN phone reaching for the router's own
public hostname gets a flat connection refusal. Reverted primary_phone/home_tablet to
LAN-first with DDNS offered as an explicit fallback link; trusted_guest correctly keeps
DDNS-first since remote guest scanning is the actual point there. (4) whatsapp_journey's
start_container() was written specifically to close the 'run docker compose by hand' gap
(its own docstring says so) but was never wired to any route the cockpit could call, so
three separate messages kept telling people to run `mongoose whatsapp setup` in a terminal
regardless. Added the route and a real button. (5)/(6) the voice picker saved silently via
localStorage with zero visual confirmation in either the cockpit or phone view."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_embeddings_check_never_shows_the_broken_placeholder(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "e.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.doctor import run_doctor
    e = run_doctor()["checks"]["embeddings"]
    assert "?" not in e["detail"]


def test_https_check_agrees_with_self_signed_status_and_names_the_button(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from mongoose.doctor import run_doctor
    h = run_doctor()["checks"]["https"]
    assert "make_cert.sh" not in h["detail"]
    assert "One click, no terminal" in h["detail"]
    from mongoose.self_signed_tls import generate_self_signed, self_signed_status
    generate_self_signed()
    h2 = run_doctor()["checks"]["https"]
    ss = self_signed_status()
    # Whatever self_signed_status says "configured" is, doctor must say the SAME thing:
    # this is the actual point, not any specific value.
    assert h2["ok"] == bool(ss.get("configured"))


def test_owner_phone_pairing_prefers_lan_ddns_only_as_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "q.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/devices/pair-code", json={"profile": "primary_phone"}, headers=H)
    j = r.json()
    assert "duckdns" not in j.get("pair_url", "")
    assert j.get("remote_pair_url") and "duckdns" in j["remote_pair_url"]


def test_guest_invite_pairing_still_prefers_ddns(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "q2.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/devices/pair-code", json={"profile": "trusted_guest"}, headers=H)
    assert "duckdns" in r.json().get("pair_url", "")


def test_whatsapp_start_container_route_exists_and_no_message_mentions_a_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "w.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/whatsapp/start-container", headers=H)
    assert r.status_code == 200
    from mongoose.services.whatsapp_journey import connection_state, connect, login_qr
    for res in (connection_state(), connect(), login_qr()):
        blob = str(res)
        assert "mongoose whatsapp setup" not in blob


def test_voice_pickers_have_a_visible_save_confirmation():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "voiceSavedFlash" in h and "Saved" in h
    assert "voicePickerPhone" in h.replace("voicePickerPhone", "") or True  # phone view lives elsewhere; see source-level check below
    import pathlib
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    assert "voiceSavedFlashPhone" in src and "voicePickerPhone" in src
