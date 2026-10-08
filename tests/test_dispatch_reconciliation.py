"""Reconciling a second external review (Dispatch's patched 1.28.2) against this lineage: not a
merge, a re-derivation. Each finding independently verified against THIS codebase before being
adopted, several found to be genuinely more correct than what shipped here, one found to
identify a real additional instance of an already-fixed bug class in a third file.

1. Owner-local URLs (install/welcome/ui, the CLI's own startup message, the bash bootstrap
   script that opens /install) now consistently emit 127.0.0.1, complementing (not replacing)
   the server-side dual-stack bind fix: browsers track certificate trust and service-worker
   state per origin, so avoiding "localhost" in anything the app itself opens removes that as a
   variable independent of whether the bind mechanism is also correct on a given machine.

2. guest_invite_prerequisites() no longer gates invite CREATION on MONGOOSE_ALLOW_REMOTE at
   all -- verified against this codebase's own documented design (_guard_sensitive_routes:
   "does NOT depend on allow_remote") that guest/device routes are authorized by their own
   scoped device token, independent of that flag. Replaced with the real prerequisite (is the
   server actually network-bound), read from saved .env alongside the live process so a
   just-saved setting is not reported as missing purely because the process has not restarted;
   still correctly blocks when genuinely unconfigured or not yet restarted, with an honest
   reason distinguishing the two.

3. remote_readiness()'s diagnostic display (feeds doctor's status only, gates no action) shows
   both live and saved MONGOOSE_HOST/ALLOW_REMOTE/API_TOKEN, correctly reporting "saved
   correctly, restart needed" instead of "unconfigured" when that's the true state.

4. wizExpand gained a genuinely separate bug fix: it toggles CLOSED if the same slug is already
   open (correct, desired behaviour for a manual tile click) and always scrolls even if already
   in view. gotoDdnsSetup (a programmatic deep link, not a manual click) now force-opens and
   only scrolls if not already in view -- a second, independent mechanism behind "collapses and
   jumps" that the earlier fix (avoiding an unnecessary tab reload) did not address.

5. The raw "not_lan_reachable" sentinel was shown to the person verbatim on two render sites;
   now translated to human text on both.

6. liveness.py had its OWN independent, even weaker (bare os.path.exists + getsize > 0, no
   plausibility floor at all) token-presence reimplementation, missed when scheduler.py's
   identical bug class was fixed earlier. Routed through the same central oauth.py helpers.

7. The stable secrets directory is now created unconditionally, not only inside the
   legacy-migration branch -- a completely fresh install with no prior secrets never had it
   created at this call site at all.
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_owner_local_urls_use_127_not_localhost():
    from mongoose.access_urls import local_browser_base
    assert local_browser_base(8765).startswith("https://127.0.0.1:8765")
    assert "localhost" not in local_browser_base(8765)


def test_no_hardcoded_localhost_browser_urls_remain():
    import pathlib
    for rel in ("src/mongoose/cockpit.py", "src/mongoose/mcp_stub.py"):
        assert "https://localhost:" not in pathlib.Path(rel).read_text(), rel


def test_bootstrap_script_opens_install_via_127_not_localhost():
    import pathlib
    src = pathlib.Path("scripts/mongoose_local_probe.sh").read_text()
    idx = src.index("mongoose_local_base()")
    body = src[idx:idx + 200]
    assert "127.0.0.1" in body
    assert '"localhost"' not in body.split("printf")[1] if "printf" in body else True


def test_pairing_preflight_uses_127():
    import pathlib
    src = pathlib.Path("src/mongoose/services/pairing_preflight.py").read_text()
    assert "://127.0.0.1:{port}/install" in src
    assert "listening only on 127.0.0.1" in src


def test_guest_invite_no_longer_gates_on_allow_remote(monkeypatch):
    """The removed, architecturally-wrong gate must never reappear."""
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "false")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://yourname.duckdns.org")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    from mongoose.guest_invites import guest_invite_prerequisites
    r = guest_invite_prerequisites()
    assert r["ok"] is True
    codes = [p["code"] for p in r["problems"]]
    assert "remote_disabled" not in codes


def test_guest_invite_distinguishes_saved_from_live(monkeypatch):
    monkeypatch.delenv("MONGOOSE_HOST", raising=False)
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://yourname.duckdns.org")
    import mongoose.guest_invites as GI
    monkeypatch.setattr(GI, "_saved_env_for_readiness",
                        lambda: {"MONGOOSE_HOST": "0.0.0.0",
                                "MONGOOSE_PUBLIC_URL": "https://yourname.duckdns.org"})
    r = GI.guest_invite_prerequisites()
    codes = [p["code"] for p in r["problems"]]
    assert r["ok"] is False
    assert "remote_restart_needed" in codes
    assert "remote_disabled" not in codes


def test_guest_invite_still_blocks_when_genuinely_unconfigured(monkeypatch):
    monkeypatch.delenv("MONGOOSE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("MONGOOSE_HOST", raising=False)
    import mongoose.guest_invites as GI
    monkeypatch.setattr(GI, "_saved_env_for_readiness", lambda: {})
    r = GI.guest_invite_prerequisites()
    assert r["ok"] is False
    assert "no_dynamic_dns" in [p["code"] for p in r["problems"]]


def test_remote_readiness_shows_saved_vs_live(monkeypatch):
    monkeypatch.delenv("MONGOOSE_HOST", raising=False)
    import mongoose.services.remote_access as RA
    monkeypatch.setattr(RA, "_saved_env", lambda: {"MONGOOSE_HOST": "0.0.0.0"})
    r = RA.remote_readiness(probe_outside=False)
    lan = next(c for c in r["checks"] if c["label"] == "Server binds the LAN")
    assert lan["ok"] is False
    assert "restart needed" in lan["detail"]


def test_wizexpand_force_open_never_toggles_closed():
    from mongoose.cockpit import COCKPIT_HTML
    assert "function wizExpand(stageId, slug, opts)" in COCKPIT_HTML
    assert "opts.forceOpen" in COCKPIT_HTML
    assert "wizExpand(sid, \"ddns\", {forceOpen:true" in COCKPIT_HTML


def test_reachability_sentinel_translated_to_human_text():
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    assert 'esc(d.reachability_warning)' not in html
    assert "not reachable from the network yet" in html


def test_liveness_routes_through_central_oauth_helpers():
    import pathlib
    src = pathlib.Path("src/mongoose/liveness.py").read_text()
    assert "getsize(cache) > 0" not in src
    assert "from .oauth import microsoft_token_present" in src
    assert "from .oauth import google_token_present" in src


def test_liveness_rejects_placeholder_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_TOKEN_CACHE", str(tmp_path / "ms.json"))
    (tmp_path / "ms.json").write_text("{}")
    from mongoose.liveness import _microsoft_token_present
    assert _microsoft_token_present() is False
    (tmp_path / "ms.json").write_text('{"token":"' + "x" * 200 + '"}')
    assert _microsoft_token_present() is True


def test_stable_secrets_dir_created_on_fresh_install(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from mongoose.settings import _default_secret_path
    _default_secret_path("google-token.json")
    assert (tmp_path / ".mongoose" / "secrets").is_dir()
