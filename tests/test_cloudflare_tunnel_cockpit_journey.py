"""The no-terminal Cloudflare Tunnel journey. The CLI version shipped first and was
immediately rejected: 'i dont want terminal commands needed.' Correct call: the terminal
journey is a developer journey, not the product. Everything a terminal did is now done by
the server itself behind Cockpit buttons: download the official static binary into
Mongoose's own data/bin (no Homebrew, no sudo), spawn `cloudflared tunnel login` and hand
the UI the approval URL as a clickable link, then one Provision button (create + route DNS
+ write config + start + persist env). Mongoose supervises the tunnel process itself
instead of `sudo cloudflared service install`; that single substitution is what removes
sudo, and with it the terminal, from the whole journey. The only human step left is
Cloudflare's own browser approval, which is account auth and should not be automatable."""
from __future__ import annotations

import warnings
import unittest.mock as mock
from pathlib import Path

warnings.filterwarnings("ignore")

FAKE_CF = "/tmp/fakecf-test/cloudflared"


def _make_fake_cloudflared() -> str:
    d = Path("/tmp/fakecf-test")
    d.mkdir(exist_ok=True)
    p = d / "cloudflared"
    p.write_text(
        "#!/bin/bash\n"
        'case "$1 $2" in\n'
        '  "tunnel login")\n'
        '    echo "Please open the following URL:"\n'
        '    echo "https://dash.cloudflare.com/argotunnel?aud=test"\n'
        "    sleep 30 ;;\n"
        '  "tunnel create")\n'
        '    mkdir -p "$HOME/.cloudflared"\n'
        "    echo '{}' > \"$HOME/.cloudflared/12345678-aaaa-bbbb-cccc-1234567890ab.json\"\n"
        '    echo "Created tunnel mongoose with id 12345678-aaaa-bbbb-cccc-1234567890ab" ;;\n'
        '  "tunnel route") echo "Added CNAME" ;;\n'
        '  "tunnel run") sleep 300 ;;\n'
        "esac\n"
    )
    p.chmod(0o755)
    return str(p)


def test_start_login_captures_the_approval_url_from_a_real_process(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose import cloudflare_tunnel as ct
    ct._LOGIN_PROC.clear()
    fake = _make_fake_cloudflared()
    with mock.patch.object(ct, "cloudflared_path", return_value=fake):
        r = ct.start_login()
    assert r["ok"] and "dash.cloudflare.com" in r["login_url"]
    proc = ct._LOGIN_PROC.get("proc")
    if proc is not None:
        proc.kill()
    ct._LOGIN_PROC.clear()


def test_provision_runs_the_whole_chain_and_persists_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / ".env")
    from mongoose import cloudflare_tunnel as ct
    fake = _make_fake_cloudflared()
    cf = tmp_path / ".cloudflared"
    cf.mkdir()
    (cf / "cert.pem").write_text("cert")
    with mock.patch.object(ct, "cloudflared_path", return_value=fake), \
         mock.patch.object(ct, "_lookup", return_value=[]), \
         mock.patch.object(ct, "tunnel_end_to_end_proof",
                          return_value={"ok": True, "is_mongoose": True, "status": 200}):
        r = ct.provision("Mongoose.Example.COM")
    try:
        assert r["ok"], r
        assert r.get("status") == "live"
        assert r["public_url"] == "https://mongoose.example.com"
        cfg = (ct._managed_config_dir() / "config.yml").read_text()
        assert "hostname: mongoose.example.com" in cfg
        assert "127.0.0.1" in cfg
        env_text = (tmp_path / ".env").read_text()
        assert "MONGOOSE_PUBLIC_URL=https://mongoose.example.com" in env_text
    finally:
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "fakecf-test/cloudflared"], capture_output=True)


def test_provision_refuses_before_login_and_names_the_step(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose import cloudflare_tunnel as ct
    fake = _make_fake_cloudflared()
    with mock.patch.object(ct, "cloudflared_path", return_value=fake):
        r = ct.provision("mongoose.example.com")
    assert r["ok"] is False and r["step"] == "login"


def test_install_returns_existing_binary_without_downloading(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose import cloudflare_tunnel as ct
    with mock.patch.object(ct, "cloudflared_path", return_value="/x/cloudflared"):
        r = ct.install_cloudflared()
    assert r["ok"] and r["already"] is True


def test_cockpit_tunnel_routes_are_nonce_gated(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "d.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    for method, path in (("GET", "/cockpit/tunnel/state"), ("POST", "/cockpit/tunnel/install"),
                         ("POST", "/cockpit/tunnel/login"), ("POST", "/cockpit/tunnel/provision")):
        r = c.request(method, path)
        assert r.status_code in (401, 403), path
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.get("/cockpit/tunnel/state", headers=H)
    assert r.status_code == 200 and "state" in r.json()


def test_cockpit_card_renders_the_journey():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "Guest access (Cloudflare Tunnel)" in h
    for fn in ("tunnelInstall", "tunnelLogin", "tunnelProvision"):
        assert fn in h
    # The login poll must use this document's real async nonce pattern.
    assert "await ensureNonce()" in h


def test_serve_startup_restarts_a_configured_tunnel():
    import inspect
    import mongoose.cli as cli_mod
    src = inspect.getsource(cli_mod)
    assert "ensure_tunnel_running" in src


def test_supervised_run_writes_pidfile_and_is_detected(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose import cloudflare_tunnel as ct
    fake = _make_fake_cloudflared()
    (ct._managed_config_dir() / "config.yml").write_text("tunnel: mongoose\n")
    with mock.patch.object(ct, "cloudflared_path", return_value=fake), \
         mock.patch.object(ct, "_process_running", side_effect=[False, True]):
        r = ct.ensure_tunnel_running()
        assert r["ok"] and r.get("pid")
        assert ct._process_running() is True
    import subprocess
    subprocess.run(["pkill", "-9", "-f", "fakecf-test/cloudflared"], capture_output=True)


def test_reboot_persistence_is_a_distinct_honest_fact(monkeypatch, tmp_path):
    """External review, verbatim: without real user-level persistence, 'survives reboots'
    means only 'survives tunnel crashes during a Mongoose session', which is not the same
    thing. tunnel_state now carries reboot_persistent as its own independently checked
    fact, and plist-on-disk alone must not count: the real machine's forensics showed
    exactly that split (plist present, launchctl list empty)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.cloudflare_tunnel import tunnel_state, _reboot_persistent
    assert _reboot_persistent() is False
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.mongoose.server-watchdog.plist").write_text("<plist/>")
    assert _reboot_persistent() is False
    assert tunnel_state({}).get("reboot_persistent") is False


def test_service_install_route_targets_the_watchdog_not_the_scheduler():
    """Caught before shipping: the codebase has TWO LaunchAgents, and `mongoose service`'s
    plist runs `mongoose scheduler`, which never starts the server. A button labelled
    'start Mongoose after reboot' backed by it would be live-looking-but-wrong wiring. The
    route must use the watchdog agent (the thing that actually restarts the server) and
    must never call service.install."""
    import inspect
    import mongoose.app as A
    src = inspect.getsource(A.cockpit_service_install)
    assert "_wd_install_agent" in src
    assert "from .service import install" not in src


def test_cockpit_card_warns_when_live_but_not_reboot_persistent():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "Won't survive a restart yet" in h
    assert "serviceInstall" in h


def test_route_step_zone_not_found_explains_the_real_fix(monkeypatch, tmp_path):
    """The single most likely stall for a real domain, asked about directly ('will it tell
    me exactly how to hook up example.org'): routing a DNS name to the tunnel only works
    if the domain's zone is already on Cloudflare DNS. cloudflared's raw zone-not-found
    error does not say that, and for a domain hosting a live site the message must also say
    what is safe: records get imported, and Mongoose only uses a subdomain."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import unittest.mock as mock
    from mongoose import cloudflare_tunnel as ct
    cf = tmp_path / ".cloudflared"
    cf.mkdir()
    (cf / "cert.pem").write_text("c")
    (cf / "u.json").write_text("{}")

    def fake_run(binary, *args, timeout=60.0):
        if args[:2] == ("tunnel", "create"):
            (cf / "abcd0000-1111-2222-3333-444455556666.json").write_text("{}")
            return 0, "Created tunnel mongoose with id abcd0000-1111-2222-3333-444455556666"
        if args[:3] == ("tunnel", "route", "dns"):
            return 1, "failed to add route: zone could not be found"
        return 0, "ok"

    with mock.patch.object(ct, "cloudflared_path", return_value="/x/cf"), \
         mock.patch.object(ct, "_lookup", return_value=[]), \
         mock.patch.object(ct, "_run", side_effect=fake_run):
        r = ct.provision("mongoose.example.org")
    assert r["step"] == "route"
    assert "not on Cloudflare DNS yet" in r["error"]
    assert "nameservers" in r["error"]
    assert "subdomain" in r["error"]


def _fake_ovh_lookup(rrtype, name, timeout=5.0):
    if rrtype == "NS":
        return ["dns200.anycast.me", "dns.ovh.net"]
    if rrtype == "MX":
        return ["1 mx1.mail.ovh.net"]
    return []


def test_dns_preflight_names_ovh_and_protects_the_email_records():
    """Asked directly: 'will it tell me to change anything in my ovh control panel'. The
    NS records themselves identify where the change happens, so the guidance names OVH and
    the exact panel path instead of 'your registrar'. And because the same domain runs
    email on OVH, the one warning that genuinely matters is included: verify the MX
    records survived Cloudflare's import BEFORE switching nameservers, or mail to that
    domain stops."""
    import unittest.mock as mock
    from mongoose import cloudflare_tunnel as ct
    with mock.patch.object(ct, "_lookup", side_effect=_fake_ovh_lookup):
        pre = ct.domain_dns_preflight("mongoose.example.org")
    assert pre["domain"] == "example.org"
    assert pre["provider"] == "OVH"
    assert "OVH control panel" in pre["detail"]
    assert pre["has_mail"] and "MX records BEFORE" in pre["detail"]


def test_provision_fails_fast_at_dns_prereq_before_any_cloudflared_call(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import unittest.mock as mock
    from mongoose import cloudflare_tunnel as ct
    cf = tmp_path / ".cloudflared"
    cf.mkdir()
    (cf / "cert.pem").write_text("c")
    ran = []
    with mock.patch.object(ct, "cloudflared_path", return_value="/x/cf"), \
         mock.patch.object(ct, "_lookup", side_effect=_fake_ovh_lookup), \
         mock.patch.object(ct, "_run", side_effect=lambda *a, **k: ran.append(a) or (0, "ok")):
        r = ct.provision("mongoose.example.org")
    assert r["step"] == "dns_prereq"
    assert ran == [], "no cloudflared command should run when the DNS prerequisite fails"


def test_unknown_ns_lookup_never_blocks_provisioning():
    """An empty lookup means 'could not determine', not 'not on Cloudflare'; blocking on it
    would strand people whose machines can't resolve NS records, and the route step's own
    error mapping still covers the genuine zone-missing case."""
    import unittest.mock as mock
    from mongoose import cloudflare_tunnel as ct
    with mock.patch.object(ct, "_lookup", return_value=[]):
        pre = ct.domain_dns_preflight("mongoose.example.org")
    assert pre["known"] is False and pre["on_cloudflare"] is False


def test_mixed_setup_route_already_existed_is_diagnosed_not_blamed_on_propagation(monkeypatch, tmp_path):
    """Found via a parallel assistant's terminal instructions for the OLD version (Zero
    Trust dashboard tunnel, sudo service install): if the hostname was ever routed to a
    tunnel made another way, Cloudflare reports the DNS record 'already exists', the route
    step cannot tell whose record it is, and the subsequent proof failure must name the
    likely mixed-setup cause with the dashboard fix, NOT the 'DNS can take a few minutes'
    hint, which would send the owner into an infinite wait on a name pointed at the wrong
    tunnel forever."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import unittest.mock as mock
    from mongoose import cloudflare_tunnel as ct
    cf = tmp_path / ".cloudflared"
    cf.mkdir()
    (cf / "cert.pem").write_text("c")

    def fake_run(binary, *args, timeout=60.0):
        if args[:2] == ("tunnel", "create"):
            (cf / "abcd0000-1111-2222-3333-444455556666.json").write_text("{}")
            return 0, "Created tunnel mongoose with id abcd0000-1111-2222-3333-444455556666"
        if args[:3] == ("tunnel", "route", "dns"):
            return 1, "Failed to add route: An A, AAAA, or CNAME record with that host already exists"
        return 0, "ok"

    import mongoose.setup_wizard as sw
    with mock.patch.object(ct, "cloudflared_path", return_value="/x/cf"), \
         mock.patch.object(ct, "_lookup", return_value=[]), \
         mock.patch.object(ct, "_run", side_effect=fake_run), \
         mock.patch.object(ct, "ensure_tunnel_running", return_value={"ok": True, "pid": 1}), \
         mock.patch.object(ct, "tunnel_end_to_end_proof", return_value={"ok": False, "error": "timed out"}), \
         mock.patch("time.sleep"), \
         mock.patch.object(sw, "ENV_PATH", tmp_path / ".env"):
        r = ct.provision("mongoose.example.com")
    assert r["status"] == "configured_not_proven"
    assert "DIFFERENT tunnel" in r["error"]
    assert "few minutes" not in r["error"]
