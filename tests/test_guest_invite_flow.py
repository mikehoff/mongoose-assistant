"""Friend-invite flow: suggested name (guest-changeable), canonical share link, the
"You're invited" landing screen with Continue and Delete-this-invitation, and the
token-holder delete path."""
from __future__ import annotations

import warnings

import pytest

warnings.filterwarnings("ignore")


@pytest.fixture()
def ready_env(monkeypatch):
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "yourname.duckdns.org")


def _mint(**kw):
    from mongoose.guest_invites import create_guest_invite
    inv = create_guest_invite(owner_label=kw.pop("owner_label", "Test Friend"),
                              base_url="https://yourname.duckdns.org", **kw)
    assert inv["ok"], inv
    return inv, inv["invite_url"].rsplit("/", 1)[-1]


def test_suggested_name_stored_and_surfaced(ready_env):
    from mongoose.guest_invites import invite_status
    inv, token = _mint(owner_label="Lee from work", suggested_name="Lee")
    assert inv["suggested_name"] == "Lee"
    assert invite_status(token)["suggested_name"] == "Lee"


def test_suggested_name_defaults_to_owner_label(ready_env):
    inv, token = _mint(owner_label="Sam")
    assert inv["suggested_name"] == "Sam"


def test_landing_screen_copy_and_buttons(ready_env):
    from fastapi.testclient import TestClient
    import mongoose.app as A
    inv, token = _mint(suggested_name="Lee")
    c = TestClient(A.app, client=("203.0.113.50", 40000))
    r = c.get(f"/guest/invite/{token}")
    html = r.text
    assert "You\u2019re invited to join Mongoose" in html
    assert "Are you in?" in html
    assert ">Continue</button>" in html
    assert 'class="delete">Delete this invitation</button>' in html
    assert "INVITE_STATUS.suggested_name" in html      # prefill wiring
    assert r.headers.get("cache-control", "").startswith("no-store")


def test_delete_invalidates_and_is_idempotent(ready_env):
    from fastapi.testclient import TestClient
    import mongoose.app as A
    from mongoose.guest_invites import invite_status
    inv, token = _mint()
    c = TestClient(A.app, client=("203.0.113.50", 40000))
    assert c.post(f"/guest/invite/{token}/delete").json() == {"ok": True, "deleted": True}
    assert invite_status(token)["error"] == "revoked_invite"
    claim = c.post(f"/guest/invite/{token}/claim",
                   json={"mongoose_name": "x", "public_name": "x", "fun_fact": "x",
                         "client_version": "1.20.0"}).json()
    assert claim.get("ok") is False
    assert c.post(f"/guest/invite/{token}/delete").json()["ok"] is True
    assert c.post("/guest/invite/never-was-a-token/delete").json()["ok"] is False


def test_owner_route_passes_suggested_name(ready_env, monkeypatch):
    from fastapi.testclient import TestClient
    import mongoose.app as A
    captured = {}
    import mongoose.guest_invites as G
    real = G.create_guest_invite
    def spy(**kw):
        captured.update(kw)
        return real(**{**kw, "base_url": "https://yourname.duckdns.org"})
    monkeypatch.setattr(G, "create_guest_invite", spy)
    c = TestClient(A.app)
    r = c.post("/cockpit/guests/invite",
               headers={"x-cockpit-nonce": A._COCKPIT_NONCE},
               json={"owner_label": "Lee from work", "suggested_name": "Lee"})
    assert r.status_code == 200
    assert captured.get("suggested_name") == "Lee"
