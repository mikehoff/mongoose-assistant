"""Cloudflare Tunnel: the guest-friendly public route for a Mongoose behind CGNAT.

Built after a real seven-hour incident proved the DDNS + port-forward route structurally
impossible on carrier-grade NAT (the public IP is the ISP's shared address; the router's
forward receives traffic that never arrives). The requirement that decided the design:
"works on my guests' phones" -- a normal https link on an unmodified phone, no app, no
enrollment. Tunnel proof from this machine is genuinely end-to-end (out to Cloudflare's
edge, back down the tunnel: the same path a guest's phone uses), which retires the
long-standing "a self-test can't prove external reachability" limitation for this mode."""
from __future__ import annotations

import warnings
import unittest.mock as mock

warnings.filterwarnings("ignore")


def test_set_hostname_normalizes_and_sets_public_url(monkeypatch, tmp_path):
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / ".env")
    from mongoose.cloudflare_tunnel import set_tunnel_hostname
    r = set_tunnel_hostname("https://Mongoose.Example.COM/")
    assert r["ok"] and r["hostname"] == "mongoose.example.com"
    text = (tmp_path / ".env").read_text()
    assert "MONGOOSE_TUNNEL_PROVIDER=cloudflare" in text
    assert "MONGOOSE_PUBLIC_URL=https://mongoose.example.com" in text


def test_set_hostname_rejects_garbage(monkeypatch, tmp_path):
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / ".env")
    from mongoose.cloudflare_tunnel import set_tunnel_hostname
    assert set_tunnel_hostname("")["ok"] is False
    assert set_tunnel_hostname("nodots")["ok"] is False


def test_tunnel_public_url_has_no_port_suffix(monkeypatch, tmp_path):
    """The entire point for guests: a boring 443 https URL, no :8765 anywhere."""
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / ".env")
    from mongoose.cloudflare_tunnel import set_tunnel_hostname
    r = set_tunnel_hostname("mongoose.example.com")
    assert ":8765" not in r["public_url"]
    from mongoose.services.remote_access import public_base_url
    assert public_base_url({"MONGOOSE_PUBLIC_URL": r["public_url"]}) == "https://mongoose.example.com"


def test_end_to_end_proof_requires_this_mongoose_not_just_any_200():
    """A parked page or someone else's service answering 200 on the hostname must never
    count as proof; the body must be THIS product's health JSON."""
    from mongoose import cloudflare_tunnel as ct
    fake_resp = mock.MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = b"<html>parked domain</html>"
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = lambda s, *a: False
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        r = ct.tunnel_end_to_end_proof("mongoose.example.com")
    assert r["status"] == 200 and r["ok"] is False and r["is_mongoose"] is False


def test_end_to_end_proof_accepts_real_mongoose_health():
    from mongoose import cloudflare_tunnel as ct
    fake_resp = mock.MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = b'{"ok":"true","name":"mongoose","version":"1.49.0","port":8765}'
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = lambda s, *a: False
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        r = ct.tunnel_end_to_end_proof("mongoose.example.com")
    assert r["ok"] is True and r["is_mongoose"] is True and r["version"] == "1.49.0"


def test_remote_away_ready_via_tunnel_when_proven(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MONGOOSE_TUNNEL_PROVIDER", "cloudflare")
    monkeypatch.setenv("MONGOOSE_TUNNEL_HOSTNAME", "mongoose.example.com")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://mongoose.example.com")
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    import mongoose.cloudflare_tunnel as ct
    with mock.patch.object(ct, "tunnel_end_to_end_proof",
                          return_value={"ok": True, "hostname": "mongoose.example.com",
                                       "status": 200, "is_mongoose": True}):
        from mongoose.setup_state import setup_state
        s = setup_state(only=["remote_away"])["surfaces"]["remote_away"]
    assert s["ok"] is True and s["status"] == "ready_via_tunnel"


def test_remote_away_tunnel_failure_never_misdiagnosed_as_dns_stale(monkeypatch, tmp_path):
    """The bug this branch specifically prevents: a Cloudflare hostname resolves to CF edge
    IPs, never this network's public IP, so falling through to the DDNS diagnosis would
    call a working-or-broken tunnel 'dns_stale' either way."""
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MONGOOSE_TUNNEL_PROVIDER", "cloudflare")
    monkeypatch.setenv("MONGOOSE_TUNNEL_HOSTNAME", "mongoose.example.com")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://mongoose.example.com")
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    import mongoose.cloudflare_tunnel as ct
    with mock.patch.object(ct, "tunnel_end_to_end_proof",
                          return_value={"ok": False, "error": "timed out"}), \
         mock.patch.object(ct, "_process_running", return_value=False):
        from mongoose.setup_state import setup_state
        s = setup_state(only=["remote_away"])["surfaces"]["remote_away"]
    assert s["ok"] is False and s["status"] == "tunnel_configured_not_proven"
    assert "dns_stale" not in str(s)


def test_no_tunnel_configured_leaves_ddns_path_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "nonexistent.env")
    from mongoose.setup_state import setup_state
    s = setup_state(only=["remote_away"])["surfaces"]["remote_away"]
    assert s["status"] == "not_configured"


def test_setup_plan_next_step_tracking(monkeypatch, tmp_path):
    from mongoose import cloudflare_tunnel as ct
    with mock.patch.object(ct, "cloudflared_path", return_value="/opt/homebrew/bin/cloudflared"), \
         mock.patch.object(ct, "_logged_in", return_value=False):
        plan = ct.setup_plan({})
    assert plan[0]["step"] == "install" and plan[0]["done"] is True
    assert plan[1]["step"] == "login" and plan[1]["done"] is False


def test_cli_tunnel_commands_are_registered():
    import inspect
    import mongoose.cli as cli_mod
    src = inspect.getsource(cli_mod)
    assert 'sub.add_parser("tunnel"' in src
    for c in ('"status"', '"setup"', '"diagnose"', '"set-hostname"'):
        assert f'tunnel_sub.add_parser({c}' in src


def test_guest_invites_accept_loopback_only_bind_in_tunnel_mode(monkeypatch, tmp_path):
    """External review of a parallel implementation caught this: the guest-invite readiness
    collapsed every route into the DDNS/router-forward model, demanding MONGOOSE_HOST=0.0.0.0
    even in tunnel mode, where cloudflared connects OUTBOUND to the loopback origin and a
    this-Mac-only bind is fully correct (and the safer default the tunnel exists to enable)."""
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.guest_invites import guest_invite_prerequisites
    env = {"MONGOOSE_TUNNEL_PROVIDER": "cloudflare", "MONGOOSE_TUNNEL_HOSTNAME": "m.example.com",
          "MONGOOSE_PUBLIC_URL": "https://m.example.com", "MONGOOSE_HOST": "localhost"}
    # Post-audit: tunnel mode also requires the LIVE end-to-end proof, not config alone; a
    # configured-but-dead tunnel producing a normal-looking invite was the audit's top find.
    import mongoose.cloudflare_tunnel as ct
    with mock.patch.object(ct, "tunnel_end_to_end_proof",
                          return_value={"ok": True, "is_mongoose": True, "status": 200}):
        r = guest_invite_prerequisites(env)
    assert r["ok"], r["problems"]


def test_guest_invites_still_flag_loopback_bind_without_a_tunnel(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.guest_invites import guest_invite_prerequisites
    env = {"MONGOOSE_PUBLIC_URL": "https://x.duckdns.org:8765", "MONGOOSE_HOST": "localhost"}
    r = guest_invite_prerequisites(env)
    assert "server_not_network_bound" in [p["code"] for p in r["problems"]]


def test_dead_tunnel_blocks_invite_creation_with_tunnel_not_proven(monkeypatch, tmp_path):
    """The audit's highest-risk find, verbatim scenario: tunnel provider + hostname +
    MONGOOSE_PUBLIC_URL set, host localhost, end-to-end proof failing. Before the fix this
    produced a normal-looking invite whose first contact with the guest was a browser error
    page, the exact experience the tunnel route exists to eliminate."""
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import mongoose.cloudflare_tunnel as ct
    from mongoose.guest_invites import guest_invite_prerequisites
    env = {"MONGOOSE_TUNNEL_PROVIDER": "cloudflare", "MONGOOSE_TUNNEL_HOSTNAME": "down.example.com",
          "MONGOOSE_PUBLIC_URL": "https://down.example.com", "MONGOOSE_HOST": "localhost"}
    with mock.patch.object(ct, "tunnel_end_to_end_proof", return_value={"ok": False, "error": "timed out"}):
        r = guest_invite_prerequisites(env)
    assert r["ok"] is False
    assert "tunnel_not_proven" in [p["code"] for p in r["problems"]]


def test_ddns_public_url_honors_the_explicit_canonical_url():
    """Fix for the audit's second find: four DDNS-era callers (guest QR fallback,
    /pair/claim handoff, /device/remote/proof, webhook URLs) call ddns.public_url directly,
    which knew nothing of MONGOOSE_PUBLIC_URL and so reported 'no public route' beside a
    proven live tunnel. Healed at the source so every caller tells the same truth."""
    from mongoose.ddns import public_url
    assert public_url({"MONGOOSE_PUBLIC_URL": "https://mongoose.example.com"}) == "https://mongoose.example.com"
    assert public_url({"MONGOOSE_PUBLIC_URL": "https://mongoose.example.com/"}) == "https://mongoose.example.com"


def test_guest_consent_backend_defaults_match_the_opt_in_ui():
    """Audit privacy note: the UI presents both sharing choices as unchecked opt-ins, but
    the backend defaulted absent fields to True, making a direct API call more permissive
    than the visible form. Defaults now match the UI."""
    import inspect
    import mongoose.guest_invites as gi
    src = inspect.getsource(gi)
    assert 'payload.get("announce_join", False)' in src
    assert 'payload.get("share_fun_fact", False)' in src


def test_cloudflare_wins_when_ddns_is_also_configured():
    """Reviewer's insisted precedence case: Cloudflare public URL configured AND DuckDNS
    also fully set; every public link must still use the Cloudflare hostname."""
    from mongoose.ddns import public_url
    env = {"MONGOOSE_PUBLIC_URL": "https://mongoose.example.org",
          "DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "yourname",
          "DDNS_DUCKDNS_TOKEN": "t"}
    assert public_url(env) == "https://mongoose.example.org"


def test_guest_qr_and_pair_claim_use_the_tunnel_hostname(monkeypatch, tmp_path):
    """Reviewer's insisted QR/claim cases, verified against the live routes rather than
    helpers: the trusted-guest QR fallback and the post-pair launchpad handoff both use the
    tunnel hostname, with ddns_used reported true and no guest blocker. This is the 'swap
    the host manually, keep the path' caveat from the old version, proven dead."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "d.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://mongoose.example.org")
    monkeypatch.setenv("MONGOOSE_TUNNEL_PROVIDER", "cloudflare")
    monkeypatch.setenv("MONGOOSE_TUNNEL_HOSTNAME", "mongoose.example.org")
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/devices/pair-code", headers=H, json={"profile": "trusted_guest"})
    j = r.json()
    assert r.status_code == 200
    assert j["pair_url"].startswith("https://mongoose.example.org/pair?code=")
    assert j.get("ddns_used") is True and not j.get("guest_blocker")
    from mongoose.devices import create_pairing_code
    code = create_pairing_code("primary_phone")["code"]
    r2 = c.post("/pair/claim", json={"code": code, "device_name": "p", "platform": "android"})
    assert r2.json()["launchpad_url"] == "https://mongoose.example.org/device/launchpad"


def test_proof_maps_502_to_the_local_origin_hop():
    """Reviewer suggestion, adopted in scoped form: a 502 from Cloudflare's edge means the
    tunnel is up but the local hop failed; the message names the usual cause (Mongoose not
    answering locally) and the hand-edit TLS case, without claiming TLS specifically."""
    import unittest.mock as mock
    from mongoose import cloudflare_tunnel as ct
    import urllib.error
    err = urllib.error.HTTPError("https://x/health", 502, "Bad Gateway", {}, None)
    with mock.patch("urllib.request.urlopen", side_effect=err):
        r = ct.tunnel_end_to_end_proof("mongoose.example.com")
    assert r["ok"] is False and r["status"] == 502
    assert "hop from" in r["error"] and "noTLSVerify" in r["error"]
