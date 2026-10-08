from __future__ import annotations

import re

from mongoose.cockpit import cockpit_html, phone_cockpit_html


def _desktop_entries(html: str):
    out = []
    for m in re.finditer(
        r'\{label:"([^"]+)",\s*tab:"([^"]+)"(?:,\s*el:"([^"]*)")?,\s*kw:"([^"]+)"',
        html,
    ):
        out.append({"label": m.group(1), "tab": m.group(2), "el": m.group(3) or "", "kw": m.group(4).lower()})
    return out


def _phone_entries(html: str):
    out = []
    # function rows: {ic:'...',label:'Run doctor',kw:'...',view:'system'}
    for m in re.finditer(r"label:'([^']+)',kw:'([^']+)'(?:,view:'([^']+)'|,ui:'([^']+)')", html):
        out.append({"label": m.group(1), "view": m.group(3) or m.group(4), "kw": (m.group(1) + " " + m.group(2)).lower()})
    # area rows are generated from AREAS, so include the labels too.
    for m in re.finditer(r"\{ic:'[^']+',lb:'([^']+)'(?:,screen:'([^']+)'|,ui:'([^']+)'|,open:function)", html):
        out.append({"label": m.group(1), "view": m.group(2) or m.group(3) or "open", "kw": ("menu " + m.group(1)).lower()})
    return out


def _matches(entries, query: str):
    terms = query.lower().split()
    return [e for e in entries if all(t in e["kw"] for t in terms)]


REAL_LIVED_QUERIES = [
    "free memory",
    "log bug",
    "apply fix",
    "data dump",
    "journal",
    "wellness",
    "model stuck",
    "phone notifications",
    "search files",
    "search email",
    "guest invite",
    "rtm build",
    "surpriser",
    "voicemail",
    "file crawl",
    "imap",
    "revoke device",
]


def test_desktop_ribbon_resolves_new_lived_use_features():
    entries = _desktop_entries(cockpit_html())
    dead = [q for q in REAL_LIVED_QUERIES if not _matches(entries, q)]
    assert not dead


def test_desktop_ribbon_scroll_targets_exist():
    html = cockpit_html()
    entries = _desktop_entries(html)
    missing = [e for e in entries if e.get("el") and (f'id="{e["el"]}"' not in html and f"id='{e['el']}'" not in html)]
    assert not missing


def test_phone_ribbon_resolves_lived_use_features_and_targets_real_views():
    html = phone_cockpit_html()
    entries = _phone_entries(html)
    dead = [q for q in REAL_LIVED_QUERIES if not _matches(entries, q)]
    assert not dead
    views = set(re.findall(r'<section class="view" id="v-([^"]+)"', html)) | {"open"}
    bad = [e for e in entries if e["view"] not in views]
    assert not bad


def test_phone_search_is_native_not_forbidden_full_ui_handoff():
    html = phone_cockpit_html()
    assert "screen:'search'" in html
    assert 'id="v-search"' in html
    assert "function loadSearch" in html
    assert "/find?q=" in html
    assert "/item/actions" in html
    assert "/ui#" not in html


def test_phone_cockpit_api_respects_long_action_timeouts():
    html = phone_cockpit_html()
    assert "const timeoutMs = opts.timeoutMs || 15000" in html
    assert "setTimeout(()=>ctrl.abort(), timeoutMs)" in html
    # These actions legitimately exceed 15s; the helper must honour their requested timeouts.
    assert "timeoutMs:120000" in html
    assert "timeoutMs:60000" in html


def test_owner_phone_search_endpoint_is_reachable_but_guest_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MONGOOSE_ALLOW_REMOTE", raising=False)
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    from mongoose import devices as D
    owner_code = D.create_pairing_code(profile="primary_phone")["code"]
    owner_token = D.claim_pairing_code(owner_code, "Owner phone", "phone")["device_token"]
    guest_code = D.create_pairing_code(profile="trusted_guest")["code"]
    guest_token = D.claim_pairing_code(guest_code, "Guest phone", "phone")["device_token"]
    from fastapi.testclient import TestClient
    import mongoose.app as A
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", {"127.0.0.1", "::1", "localhost"})
    c = TestClient(A.app, raise_server_exceptions=False)
    assert c.get("/find?q=sentinel", headers={"X-Mongoose-Device-Token": owner_token}).status_code == 200
    assert c.get("/find?q=sentinel", headers={"X-Mongoose-Device-Token": guest_token}).status_code == 403
