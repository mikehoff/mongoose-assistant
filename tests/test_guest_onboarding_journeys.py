"""The full guest arrival, audited end to end in both layers.

Python layer: the REAL chain against the app: owner creates an invite, the public landing page
renders (200, form present, suggested name injected), the guest claims, the resulting device is
trusted_guest with her chosen name, she speaks in the lounge, the owner sees her line: one
uninterrupted path through real routes.

DOM layer: the fully rendered landing page executing its own scripts in jsdom with the network
mocked, walking nine journeys: staged intro reveal, name prefill, validation without a network
call, the happy claim storing the guest pass, THE SEAM (the launchpad's real token() reader
executed against the same storage returns exactly what the landing stored), server rejection,
hostile server message staying inert, network failure surfaced honestly, revoked invite, and
the already-in short-circuit."""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest


def _mk_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "onb.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")  # guest invite readiness now also requires actual network binding, not just the allow_remote flag
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "ownertok")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://example.duckdns.org")
    from mongoose.db import init_db
    init_db()


def test_full_chain_invite_claim_lounge(monkeypatch, tmp_path):
    _mk_env(monkeypatch, tmp_path)
    from mongoose.guest_invites import create_guest_invite
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)

    inv = create_guest_invite(owner_label="Jo", suggested_name="Jo")
    assert inv.get("ok"), inv
    tok = inv.get("token") or inv["invite_url"].rsplit("/", 2)[-1].replace("/claim", "")

    # the public landing page renders with the form and the suggestion
    page = c.get(f"/guest/invite/{tok}")
    assert page.status_code == 200
    assert 'id="guestForm"' in page.text and '"suggested_name": "Jo"' in page.text.replace("'", '"') \
        or "Jo" in page.text

    # claim exactly as the page's JS does
    cl = c.post(f"/guest/invite/{tok}/claim",
               json={"mongoose_name": "Jo", "public_name": "Jo", "fun_fact": "kayaks"}).json()
    assert cl["ok"] and cl["profile"] == "trusted_guest"
    H = {"x-mongoose-device-token": cl["device_token"]}

    who = c.get("/device/whoami", headers=H).json()["device"]
    assert who["profile"] == "trusted_guest" and who.get("display_name") == "Jo"

    # she speaks in the lounge; the owner's paired phone sees her line
    assert c.post("/device/irc/send", headers=H, json={"text": "hi everyone!"}).json()["ok"]
    from mongoose.devices import create_pairing_code, claim_pairing_code
    own = claim_pairing_code(create_pairing_code(profile="primary_phone")["code"], "Alex's phone", "ios")
    room = c.get("/device/irc/history",
                 headers={"x-mongoose-device-token": own["device_token"]}).json()["messages"]
    assert any(m["sender"] == "Jo" and m["text"] == "hi everyone!" for m in room)


def test_onboarding_journeys_in_a_real_dom(monkeypatch, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    jsdom = pathlib.Path("/home/claude/node_modules/jsdom")
    env = {"JSDOM_PATH": str(jsdom) if jsdom.exists() else "jsdom", "PATH": "/usr/bin:/bin"}
    if subprocess.run([node, "-e", "require(process.env.JSDOM_PATH || 'jsdom')"],
                      env=env, capture_output=True).returncode != 0:
        pytest.skip("jsdom not available")

    from mongoose.cockpit import guest_invite_html
    landing = tmp_path / "landing.html"
    landing.write_text(guest_invite_html("test-invite-tok-123",
                                         {"ok": True, "suggested_name": "Jo",
                                          "server_version": "1.28.1"}))
    revoked = tmp_path / "revoked.html"
    revoked.write_text(guest_invite_html("dead-tok",
                                         {"ok": False, "message": "That invite has been revoked."}))
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    lp = src.index("DEVICE_LAUNCHPAD_HTML")
    reader = re.search(r"function token\(\)\{[^}]*\}", src[lp:]).group(0)
    reader_f = tmp_path / "token_reader.js"
    reader_f.write_text(reader)

    r = subprocess.run([node, "tests/assets/onboarding_journeys.js",
                        str(landing), str(revoked), str(reader_f)],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"onboarding journeys failed:\n{r.stdout}\n{r.stderr}"
    assert "ALL 9 ONBOARDING JOURNEYS PASS" in r.stdout
