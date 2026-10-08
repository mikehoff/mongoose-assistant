"""The add-to-home-screen entry must be the launchpad (the app), not the one-time /pair page, and it
must use the best reachable origin. This test pins that /pair/claim hands back a launchpad_url and a
remote flag so the pairing page can funnel the user to the launchpad before they add to home screen.
DB isolation is automatic via conftest."""
from __future__ import annotations


def test_pair_claim_returns_launchpad_url():
    from fastapi.testclient import TestClient
    import mongoose.app as A
    from mongoose.devices import create_pairing_code
    client = TestClient(A.app)
    code = create_pairing_code("primary_phone")["code"]
    r = client.post("/pair/claim", json={"code": code, "device_name": "Test phone", "device_type": "android"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert "launchpad_url" in body
    assert str(body["launchpad_url"]).endswith("/device/launchpad")
    assert "launchpad_is_remote" in body


def test_pair_page_funnels_through_launchpad_not_pair():
    """The pairing page HTML must send the home-screen step through the launchpad, and must not tell the
    user to add the /pair page itself."""
    from mongoose.cockpit import pair_html
    html = pair_html()
    assert "d.launchpad_url" in html                     # uses the server-provided launchpad URL
    assert "not this pairing page" in html               # explicitly warns against adding /pair
    assert "Open Mongoose" in html
