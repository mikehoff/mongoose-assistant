"""Real-device reproduction (two screenshots): pairing succeeded over the LAN IP
(192.168.1.9:8765), then tapping "Open Mongoose" redirected to yourname.duckdns.org:8765 and
hit ERR_CONNECTION_REFUSED.

An earlier fix made /pair/claim's launchpad_url prefer LAN over DDNS, reasoning "the owner
just proved LAN reachability, hand back a route that's guaranteed to work." That reasoning
was wrong: whatever gets Added to Home Screen becomes a PERMANENT bookmark, and a LAN address
is meaningless the instant the phone leaves this network. The actual requirement is that the
bookmark is always the durable, away-from-home-capable address (DDNS, when configured); a LAN
address must never be what gets saved as the persistent icon.

The real fix is a genuine reachability diagnosis (mongoose.ddns.reachability_diagnosis),
distinguishing DNS staleness from a router that can't loop back to its own public address
(hairpin, or a missing port-forward), so the pairing page can tell the owner the truth and
offer an explicit, clearly-labelled temporary LAN link for right now, without EVER making
that LAN link the thing that gets bookmarked."""
from __future__ import annotations

import warnings
import unittest.mock as mock

warnings.filterwarnings("ignore")


def _paired_client(monkeypatch, tmp_path, db_name):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / db_name), "MONGOOSE_DATA_DIR": str(tmp_path / "d"),
                 "DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname",
                 "DDNS_DUCKDNS_TOKEN": "t"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A


def test_post_pair_launchpad_url_is_ddns_not_lan_when_ddns_is_configured(monkeypatch, tmp_path):
    """The bookmark target must be the durable address, even moments after a LAN-based
    pairing succeeded: LAN reachability at pairing time says nothing about whether the
    address survives the phone leaving the house."""
    c, A = _paired_client(monkeypatch, tmp_path, "p.sqlite3")
    from mongoose import ddns
    with mock.patch.object(ddns, "reachability_diagnosis", return_value={"ok": True, "reason": "reachable"}):
        code = c.post("/install/pair-code").json()["code"]
        r = c.post("/pair/claim", json={"code": code, "device_name": "Alex's phone", "device_type": "android"})
        j = r.json()
    assert "duckdns" in j.get("launchpad_url", "")
    assert j.get("launchpad_is_remote") is True


def test_lan_fallback_is_offered_but_never_the_bookmark_target(monkeypatch, tmp_path):
    c, A = _paired_client(monkeypatch, tmp_path, "p2.sqlite3")
    from mongoose import ddns
    with mock.patch.object(ddns, "reachability_diagnosis", return_value={"ok": True, "reason": "reachable"}):
        code = c.post("/install/pair-code").json()["code"]
        r = c.post("/pair/claim", json={"code": code, "device_name": "Alex's phone", "device_type": "android"})
        j = r.json()
    assert j.get("launchpad_lan_fallback_url") and "192.168" not in j.get("launchpad_url", "")


def test_no_ddns_configured_falls_back_to_lan_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "p3.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    code = c.post("/install/pair-code").json()["code"]
    r = c.post("/pair/claim", json={"code": code, "device_name": "Alex's phone", "device_type": "android"})
    j = r.json()
    assert r.status_code == 200 and j.get("ok")
    assert j.get("launchpad_is_remote") is False
    assert "ddns_reachability" not in j


def test_hairpin_diagnosis_is_attached_and_honest(monkeypatch, tmp_path):
    """This is the actual screenshot scenario: DDNS configured, phone on the same LAN that
    just paired. The response must carry an honest diagnosis, not silently succeed or fail."""
    c, A = _paired_client(monkeypatch, tmp_path, "p4.sqlite3")
    from mongoose import ddns
    with mock.patch.object(ddns, "reachability_diagnosis",
                           return_value={"ok": False, "reason": "hairpin_or_no_portforward",
                                        "dns_matches": True, "self_reach_ok": False}):
        code = c.post("/install/pair-code").json()["code"]
        r = c.post("/pair/claim", json={"code": code, "device_name": "Alex's phone", "device_type": "android"})
        j = r.json()
    assert "duckdns" in j["launchpad_url"], "bookmark stays DDNS even when currently unreachable from here"
    assert j["ddns_reachability"]["reason"] == "hairpin_or_no_portforward"
    assert j.get("launchpad_lan_fallback_url")


def test_pair_html_explains_the_hairpin_case_honestly():
    from mongoose.cockpit import pair_html
    h = pair_html()
    assert "ddns_reachability" in h
    assert "hairpin_or_no_portforward" in h
    assert "launchpad_lan_fallback_url" in h
    # Must still funnel through the launchpad, not the one-time /pair page.
    assert "not this pairing page" in h
