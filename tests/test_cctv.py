"""CCTV viewer: listing, safe file resolution, owner-only access, and the Home Assistant token feed."""
from __future__ import annotations

import pytest


@pytest.fixture
def jo(tmp_path, monkeypatch):
    base = tmp_path / "camera"
    cont = base / "recordings_smart" / "continuous"
    ev = base / "recordings_smart" / "events"
    old = base / "recordings"
    live = base / "recordings_smart" / ".live"
    for d in (cont, ev, old, live):
        d.mkdir(parents=True)
    (cont / "smart_20261015_100000.mp4").write_bytes(b"A" * 5000)
    (cont / "smart_20261015_101500.mp4").write_bytes(b"B" * 100)
    (cont / "smart_20261014_230000.mp4").write_bytes(b"C" * 100)
    (cont / "notes.txt").write_text("x")
    (ev / "person_20261015_101203.mp4").write_bytes(b"E" * 300)
    (ev / "person_20261015_101203_still1.jpg").write_bytes(b"j")
    (ev / "person_20261015_101203_still1_face.jpg").write_bytes(b"j")
    (ev / "person_20261015_101203_still2_body.jpg").write_bytes(b"j")
    (ev / "person_20261015_120000_still1.jpg").write_bytes(b"j")
    (old / "jo_20261012_212408.mp4").write_bytes(b"O" * 50)
    (live / "latest.jpg").write_bytes(b"\xff\xd8live")
    (live / "init.mp4").write_bytes(b"init")
    (live / "seg_00063.m4s").write_bytes(b"seg")
    (live / "live.m3u8").write_text('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2.0,\nseg_00063.m4s\n')
    (tmp_path / "secret.mp4").write_bytes(b"nope")
    monkeypatch.setenv("MONGOOSE_CCTV_DIR", str(base))
    monkeypatch.setenv("MONGOOSE_TTS_CMD", "mongoose-test-no-tts")
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite3"))
    # Import the app (and load settings) first: settings loading can pull a real .env into
    # os.environ, so the "no token configured" state is pinned AFTER that, to an empty value.
    import mongoose.app as _app_mod
    assert _app_mod.app is not None
    from mongoose.settings import get_settings
    get_settings()
    monkeypatch.setenv("MONGOOSE_CCTV_HA_TOKEN", "")
    from mongoose.db import init_db
    init_db()
    return base


def test_listing(jo):
    from mongoose.services import cctv
    assert cctv.list_days() == ["20261015", "20261014", "20261012"]
    cont = cctv.list_continuous("20261015")
    assert [c["name"] for c in cont] == ["smart_20261015_100000.mp4", "smart_20261015_101500.mp4"]
    assert cont[0]["time"] == "10:00:00" and cont[0]["size"] == 5000
    evs = cctv.list_events("20261015")
    assert len(evs) == 2
    e = evs[0]
    assert e["clip"] == "person_20261015_101203.mp4" and e["time"] == "10:12:03"
    assert e["stills"][0] == {"index": 1, "full": "person_20261015_101203_still1.jpg", "body": None,
                              "face": "person_20261015_101203_still1_face.jpg"}
    assert e["stills"][1]["body"] == "person_20261015_101203_still2_body.jpg"
    assert evs[1]["clip"] is None
    assert [o["name"] for o in cctv.list_older("20261012")] == ["jo_20261012_212408.mp4"]
    assert cctv.list_continuous("../../x") == []


def test_resolve_rejects_traversal(jo):
    from mongoose.services import cctv
    assert cctv.resolve("continuous", "smart_20261015_100000.mp4") is not None
    for kind, name in [("continuous", "../secret.mp4"), ("continuous", "../../secret.mp4"),
                       ("continuous", "notes.txt"), ("event", "smart_20261015_100000.mp4"),
                       ("still", "person_20261015_101203.mp4"), ("live", "../continuous/x.mp4"),
                       ("nope", "smart_20261015_100000.mp4"), ("continuous", "smart_20261015_999999.mp4"),
                       ("continuous", "smart_20261015_100000.mp4/")]:
        assert cctv.resolve(kind, name) is None, (kind, name)


def test_resolve_rejects_symlink_escape(jo, tmp_path):
    from mongoose.services import cctv
    link = jo / "recordings_smart" / "continuous" / "smart_20261013_000000.mp4"
    link.symlink_to(tmp_path / "secret.mp4")
    assert cctv.resolve("continuous", link.name) is None


def _client(host="192.168.1.60"):
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app, client=(host, 50000))


def _device(profile):
    from mongoose.devices import create_pairing_code, claim_pairing_code
    code = create_pairing_code(profile)["code"]
    return claim_pairing_code(code, "Dev " + profile, "web")["device_token"]


def test_owner_allowed_loopback_and_phone(jo):
    loop = _client("127.0.0.1")
    r = loop.get("/cctv")
    assert r.status_code == 200 and "/cctv/app.js" in r.text
    assert loop.get("/cctv/api/days").json()["days"][0] == "20261015"
    j = loop.get("/cctv/api/day/20261015").json()
    assert len(j["events"]) == 2 and len(j["continuous"]) == 2
    phone = _device("primary_phone")
    r = _client().get("/cctv/api/days", headers={"X-Mongoose-Device-Token": phone})
    assert r.status_code == 200
    r = _client().get("/cctv/live/live.m3u8", headers={"X-Mongoose-Device-Token": phone})
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]


def test_guest_and_anonymous_denied(jo):
    guest = _device("trusted_guest")
    for path in ("/cctv", "/cctv/app.js", "/cctv/api/days", "/cctv/api/day/20261015",
                 "/cctv/file/continuous/smart_20261015_100000.mp4", "/cctv/live/latest.jpg"):
        assert _client().get(path, headers={"X-Mongoose-Device-Token": guest}).status_code == 403, path
        assert _client().get(path).status_code == 403, path
    from mongoose import app as A
    assert not any(p.startswith("/cctv") for p in A._GUEST_DEVICE_ROUTES)


def test_guest_account_session_cookie_denied(jo):
    from mongoose.services import auth_accounts as AA
    from mongoose.session_auth import issue_session, SESSION_COOKIE
    acct = AA.create_account("guestcam", "a long guest password 1", "Guest Jo")
    assert acct.get("ok")
    c = _client()
    c.cookies.set(SESSION_COOKIE, issue_session(remember=False, user_id=acct["user_id"]))
    assert c.get("/cctv/api/days").status_code == 403


def test_bad_names_404(jo):
    loop = _client("127.0.0.1")
    assert loop.get("/cctv/file/continuous/notes.txt").status_code == 404
    assert loop.get("/cctv/file/continuous/..%2F..%2Fsecret.mp4").status_code == 404
    assert loop.get("/cctv/file/live/latest.jpg").status_code == 404
    assert loop.get("/cctv/live/secret.mp4").status_code == 404


def test_range_request_returns_206(jo):
    r = _client("127.0.0.1").get("/cctv/file/continuous/smart_20261015_100000.mp4",
                                 headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-99/5000"
    assert len(r.content) == 100
    assert r.headers["content-type"] == "video/mp4"
    d = _client("127.0.0.1").get("/cctv/file/event/person_20261015_101203.mp4?download=1")
    assert d.status_code == 200 and "attachment" in d.headers.get("content-disposition", "")


def test_ha_token_required(jo, monkeypatch):
    lan = _client("192.168.1.20")
    assert lan.get("/cctv/ha/still.jpg?token=x").status_code == 404  # no token configured
    monkeypatch.setenv("MONGOOSE_CCTV_HA_TOKEN", "t0ken-abc")
    assert lan.get("/cctv/ha/still.jpg").status_code == 403
    assert lan.get("/cctv/ha/still.jpg?token=wrong").status_code == 403
    r = lan.get("/cctv/ha/still.jpg?token=t0ken-abc")
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8")
    pl = lan.get("/cctv/ha/live.m3u8?token=t0ken-abc")
    assert pl.status_code == 200
    assert "seg_00063.m4s?token=t0ken-abc" in pl.text and 'URI="init.mp4?token=t0ken-abc"' in pl.text
    assert lan.get("/cctv/ha/seg_00063.m4s?token=t0ken-abc").content == b"seg"
    assert lan.get("/cctv/ha/seg_00063.m4s").status_code == 403
    assert lan.get("/cctv/ha/smart_20261015_100000.mp4?token=t0ken-abc").status_code == 404
    # Not from the internet, and not through the tunnel.
    assert _client("8.8.8.8").get("/cctv/ha/still.jpg?token=t0ken-abc").status_code == 403
    assert lan.get("/cctv/ha/still.jpg?token=t0ken-abc", headers={"cf-ray": "x"}).status_code == 403
    # The HA token opens nothing else.
    assert lan.get("/cctv/api/days?token=t0ken-abc").status_code == 403
    assert lan.get("/cctv/live/latest.jpg?token=t0ken-abc").status_code == 403


def test_served_script_parses_and_has_no_dashes(jo):
    from mongoose.cctv_page import CCTV_JS, cctv_html
    for text in (CCTV_JS, cctv_html()):
        assert "\u2014" not in text and "\u2013" not in text
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = _client("127.0.0.1").get("/cctv/app.js").text
    p = subprocess.run([node, "--check", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr


def test_motion_events_listed_and_served(jo):
    ev = jo / "recordings_smart" / "events"
    (ev / "motion_20261015_103000.mp4").write_bytes(b"M" * 70)
    (ev / "motion_20261015_103000_still1.jpg").write_bytes(b"j")
    (ev / "motion_20261015_103000_still2.jpg").write_bytes(b"j")
    (ev / "motion_20261016_000100_still1.jpg").write_bytes(b"j")  # stills only, own day
    (ev / "motionx_20261015_103000.mp4").write_bytes(b"x")         # not a valid name
    (jo / "smart_recorder.log").write_text(
        "2026-10-15 10:12:40 event 20261015_101203: person seen for about 31 s; saved "
        "person_20261015_101203.mp4 (1 KB) and 2 still(s)\n"
        "2026-10-15 10:31:00 event 20261015_103000: movement without a confirmed person for about 12 s; "
        "saved motion_20261015_103000.mp4 (1 KB) and 2 still(s)\n")
    from mongoose.services import cctv
    evs = cctv.list_events("20261015")
    assert [(e["type"], e["time"]) for e in evs] == [("person", "10:12:03"), ("motion", "10:30:00"),
                                                    ("person", "12:00:00")]
    mo = evs[1]
    assert mo["clip"] == "motion_20261015_103000.mp4" and mo["seen_s"] == 12
    assert [s["full"] for s in mo["stills"]] == ["motion_20261015_103000_still1.jpg",
                                                "motion_20261015_103000_still2.jpg"]
    assert evs[0]["seen_s"] == 31 and evs[2]["seen_s"] is None
    info = {d["day"]: d for d in cctv.list_days_info()}
    assert info["20261015"]["events"] == 2 and info["20261015"]["motion"] == 1
    assert info["20261016"] == {"day": "20261016", "events": 0, "motion": 1, "continuous": 0, "older": 0}
    assert cctv.resolve("event", "motionx_20261015_103000.mp4") is None
    assert cctv.resolve("still", "motion_20261015_103000.mp4") is None
    loop = _client("127.0.0.1")
    assert loop.get("/cctv/file/event/motion_20261015_103000.mp4").content == b"M" * 70
    assert loop.get("/cctv/file/still/motion_20261015_103000_still1.jpg").status_code == 200
    j = loop.get("/cctv/api/days").json()
    assert j["days"][0] == "20261016" and j["day_info"][1]["motion"] == 1
    assert [e["type"] for e in loop.get("/cctv/api/day/20261015").json()["events"]] == ["person", "motion", "person"]


def test_segment_ranges_for_timeline(jo):
    import os
    from datetime import datetime
    cont = jo / "recordings_smart" / "continuous"
    mid = datetime(2026, 10, 15).timestamp()
    os.utime(cont / "smart_20261015_100000.mp4", (mid + 36900, mid + 36900))  # 10:15:00, past next start
    os.utime(cont / "smart_20261015_101500.mp4", (mid + 37200, mid + 37200))  # 10:20:00
    from mongoose.services import cctv
    a, b = cctv.list_continuous("20261015")
    assert a["start_s"] == 36000 and a["end_s"] == 36900  # capped at the next segment's start
    assert b["start_s"] == 36900 and b["end_s"] == 37200
    e = cctv.list_events("20261015")[0]
    assert e["start_s"] == 36723 and e["pre_roll_s"] == 10


def test_still_thumbnail(jo):
    PIL = pytest.importorskip("PIL.Image")
    p = jo / "recordings_smart" / "events" / "person_20261015_101203_still1.jpg"
    PIL.new("RGB", (1920, 1080), (200, 10, 10)).save(p, "JPEG")
    loop = _client("127.0.0.1")
    r = loop.get("/cctv/file/still/person_20261015_101203_still1.jpg?thumb=1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    import io
    assert PIL.open(io.BytesIO(r.content)).size == (480, 270)
    # A still that will not decode falls back to the original file, never an error page.
    bad = loop.get("/cctv/file/still/person_20261015_101203_still1_face.jpg?thumb=1")
    assert bad.status_code == 200 and bad.content == b"j"
    assert loop.get("/cctv/file/still/nope.jpg?thumb=1").status_code == 404
    guest = _device("trusted_guest")
    assert _client().get("/cctv/file/still/person_20261015_101203_still1.jpg?thumb=1",
                         headers={"X-Mongoose-Device-Token": guest}).status_code == 403


def test_served_script_identifiers_defined():
    """Every bare function called in the served script is defined in it (or is a known global),
    and the page has every element id the script looks up."""
    import re
    from mongoose.cctv_page import CCTV_JS, cctv_html
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", CCTV_JS))
    defined |= set(re.findall(r"var\s+([A-Za-z_$][\w$]*)\s*=", CCTV_JS))
    globals_ok = {"setTimeout", "clearTimeout", "setInterval", "clearInterval", "fetch", "Error",
                  "JSON", "Math", "Date", "String", "isFinite", "encodeURIComponent", "function",
                  "if", "for", "while", "return", "typeof", "catch", "switch", "AbortController"}
    stripped = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", CCTV_JS)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    called = set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", stripped))
    missing = sorted(c for c in called if c not in defined and c not in globals_ok)
    assert not missing, missing
    html = cctv_html()
    for i in set(re.findall(r"el\('([\w-]+)'\)", CCTV_JS)):
        assert 'id="' + i + '"' in html, i
    for text in (CCTV_JS, html):
        assert "\u2014" not in text and "\u2013" not in text
    assert "innerHTML" not in CCTV_JS and CCTV_JS.count("im.loading='lazy'") >= 2
