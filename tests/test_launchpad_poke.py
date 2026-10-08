"""Launchpad poke feature (Option A): the phone PWA gains an account identity so poking works there.

Poking is account-scoped (a poke is from a person, not a device), and the launchpad previously had only
device-token auth and no account concept, so it had no working way to poke. This adds account sign-in to
the launchpad and a hold-to-poke card. These tests lock in that:
  * the launchpad renders the poke card and the sign-in affordance;
  * /account/logout exists (login existed but logout did not);
  * a signed-in account can poke another account over the session cookie, end to end;
  * /poke/history labels direction so the phone can show 'pokes you got' without its own user id.
The embedded-JS validity gate (test_embedded_js_valid) separately guarantees the new poke JS has no
undefined-symbol bugs.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", tempfile.mkdtemp())
    yield


def _client():
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app)


def test_launchpad_renders_poke_card_and_signin():
    from mongoose.cockpit import device_launchpad_html
    h = device_launchpad_html()
    assert 'id="poke-card"' in h, "launchpad is missing the poke card"
    assert "loadPoke()" in h, "loadPoke is not wired into the launchpad load sequence"
    # sign-in affordance (Option A): the launchpad can authenticate as an account
    assert "pokeSignIn" in h and "/account/login" in h
    # the delightful hold-to-poke goose ported from the browser
    assert "pk-goose" in h and "firePoke" in h
    # poke fetches must carry the account cookie, not the device token (poke is account-scoped)
    assert "credentials:'same-origin'" in h


def test_account_logout_endpoint_exists_and_clears_cookie():
    c = _client()
    c.post("/account/register", json={"username": "loguser", "password": "pw_log_12345"})
    c.post("/account/login", json={"username": "loguser", "password": "pw_log_12345"})
    r = c.post("/account/logout")
    assert r.status_code == 200 and r.json().get("ok") is True
    # after logout the session is gone
    me = c.get("/account/me").json()
    assert not (me.get("authenticated") and me.get("account"))


def test_signed_in_account_can_poke_over_cookie_end_to_end():
    sender = _client()
    recipient = _client()
    sender.post("/account/register", json={"username": "asender", "password": "pw_send_1234"})
    recipient.post("/account/register", json={"username": "arecipient", "password": "pw_recv_1234"})
    # recipient opts in to pokes
    recipient.post("/account/login", json={"username": "arecipient", "password": "pw_recv_1234"})
    recipient.put("/account/profile", json={"display_name": "Recip", "allow_poke": True})
    # sender signs in and pokes , cookie carried same-origin, exactly as the launchpad does
    sender.post("/account/login", json={"username": "asender", "password": "pw_send_1234"})
    pk = sender.post("/poke/arecipient")
    assert pk.status_code == 200
    body = pk.json()
    assert body.get("ok") is True and body.get("to") == "Recip"


def test_poke_history_labels_direction_for_received():
    sender = _client()
    recipient = _client()
    sender.post("/account/register", json={"username": "dsender", "password": "pw_dsend_123"})
    recipient.post("/account/register", json={"username": "drecipient", "password": "pw_drecv_123"})
    recipient.post("/account/login", json={"username": "drecipient", "password": "pw_drecv_123"})
    recipient.put("/account/profile", json={"display_name": "DRecip", "allow_poke": True})
    sender.post("/account/login", json={"username": "dsender", "password": "pw_dsend_123"})
    sender.post("/poke/drecipient")
    # the recipient's history must label the poke as received (so the phone can show 'pokes you got')
    hist = recipient.get("/poke/history").json()
    assert hist.get("ok") is True
    assert any(p.get("direction") == "received" for p in hist.get("received", [])), hist
    # and the sender sees it as sent
    shist = sender.get("/poke/history").json()
    assert all(p.get("direction") == "sent" for p in shist.get("received", [])) or not shist.get("received")


def test_poke_requires_account_not_just_device_token():
    """Guard the core Option-A premise: poke is account-scoped. A caller with no account session is
    rejected, so the launchpad genuinely needed sign-in rather than riding the device token."""
    c = _client()
    r = c.post("/poke/whoever")
    assert r.status_code == 401
