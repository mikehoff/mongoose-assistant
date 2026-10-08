"""Six field reports, one investigation each with real evidence:

1. /install hung forever via https://localhost:8765 but https://127.0.0.1:8765/ui worked. Root
   cause: the server's default bind host was the LITERAL IP "127.0.0.1", but uvicorn passes the
   host string straight to asyncio's loop.create_server(), which resolves a HOSTNAME to every
   address it has and binds one socket per address -- a literal IP only ever binds that one
   family. On any real dual-stack Mac, a browser's own resolution of "localhost" tries IPv6
   (::1) first per standard OS behaviour; nothing was listening there, so the connection hung
   waiting on a socket that would never answer, while 127.0.0.1 (forcing IPv4 explicitly)
   always worked. Default changed to the hostname "localhost" so asyncio binds every address it
   resolves to, covering both families where they exist and doing no harm where they don't.

2/3. The QR pairing card could show "✓ scannable from anywhere" (DDNS checkmark) and a "not
   bound to the LAN" reachability warning SIMULTANEOUSLY -- but if the server isn't actually
   listening on the network interface, DDNS-routed traffic can't reach it either, for the exact
   same reason (same missing listener, whether traffic arrives via LAN or port-forwarded DDNS).
   The checkmark is now conditional on there being no reachability warning.

4. Clicking "Set up Dynamic DNS now" while already on the Setup tab collapsed the remote-access
   card and jumped the scroll position. Root cause: show() unconditionally calls load(t) even
   when already on that tab, fully rebuilding the pane (clearing remoteChase until its async
   re-fetch resolves, resetting scroll to the top). gotoDdnsSetup() now skips the reload when
   already on Setup.

5/6. "Set up remote access... unconfigured" and "Guest invites are not safe... remote_disabled"
   used a STATIC explanation always blaming Dynamic DNS + HTTPS, even when those were already
   correctly configured (proven by the QR pairing code already using a real DDNS URL) and the
   ONLY actual gap was the separate MONGOOSE_ALLOW_REMOTE toggle. The explanation now names the
   real gap: "Dynamic DNS and HTTPS are already set up correctly... the one thing left is
   turning on remote access itself" when that is genuinely the only remaining problem.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import warnings

import pytest

warnings.filterwarnings("ignore")


def test_default_host_is_a_resolvable_hostname_not_a_bare_ipv4_literal(monkeypatch, tmp_path):
    monkeypatch.delenv("MONGOOSE_HOST", raising=False)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "h.sqlite3"))
    from mongoose.settings import get_settings
    assert get_settings().host == "localhost"


def test_explicit_host_override_still_works(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "h2.sqlite3"))
    from mongoose.settings import get_settings
    assert get_settings().host == "0.0.0.0"


def test_self_signed_cert_covers_ipv6_loopback():
    src = pathlib.Path("src/mongoose/self_signed_tls.py").read_text()
    assert '"127.0.0.1", "::1", lan_ip' in src
    assert '"localhost", "127.0.0.1", "::1"' in src


def test_guest_readiness_reports_only_the_real_gap_when_ddns_is_configured(monkeypatch):
    """Reproduces the exact reported scenario: DDNS/public URL and network binding configured.
    Must be fully ready; the old remote_disabled gate (removed as architecturally incorrect,
    see test_dispatch_reconciliation.py) must never reappear."""
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "false")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://yourname.duckdns.org")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    from mongoose.guest_invites import guest_invite_prerequisites
    r = guest_invite_prerequisites()
    codes = [p["code"] for p in r.get("problems", [])]
    assert "no_dynamic_dns" not in codes
    assert "remote_disabled" not in codes
    assert r["ok"] is True


def _node_env():
    jsdom = pathlib.Path("/home/claude/node_modules/jsdom")
    return {"JSDOM_PATH": str(jsdom) if jsdom.exists() else "jsdom", "PATH": "/usr/bin:/bin"}


def _skip_if_no_node_jsdom():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    env = _node_env()
    if subprocess.run([node, "-e", "require(process.env.JSDOM_PATH || 'jsdom')"],
                      env=env, capture_output=True).returncode != 0:
        pytest.skip("jsdom not available")
    return node, env


def test_remote_access_card_names_the_real_gap_dom(tmp_path):
    node, env = _skip_if_no_node_jsdom() or (None, None)
    if node is None:
        return
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    start = src.index("async function chaseRemoteAccess")
    end = src.index("\nasync function j(url", start)
    block = tmp_path / "remote_chase.js"
    block.write_text(src[start:end])
    r = subprocess.run([node, "tests/assets/remote_chase_journeys.js", str(block)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL 3 REMOTE-ACCESS WORDING JOURNEYS PASS" in r.stdout


def test_qr_checkmark_honest_about_reachability_dom(tmp_path):
    node, env = _skip_if_no_node_jsdom() or (None, None)
    if node is None:
        return
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    start = src.index("async function newDevice")
    end = src.index("\nasync function", start + 10)
    block = tmp_path / "newdevice.js"
    block.write_text(src[start:end])
    r = subprocess.run([node, "tests/assets/qr_journeys.js", str(block)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL 2 QR-HONESTY JOURNEYS PASS" in r.stdout


def test_goto_ddns_setup_skips_destructive_reload_dom(tmp_path):
    node, env = _skip_if_no_node_jsdom() or (None, None)
    if node is None:
        return
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    start = src.index("function gotoDdnsSetup")
    end = src.index("\nasync function chaseRemoteAccess", start)
    block = tmp_path / "goto_ddns.js"
    block.write_text(src[start:end])
    r = subprocess.run([node, "tests/assets/goto_ddns_journeys.js", str(block)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL 2 GOTO-DDNS-SETUP JOURNEYS PASS" in r.stdout
