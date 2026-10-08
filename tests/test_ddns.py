"""Tests for the dynamic-DNS / remote-reach feature: provider catalogue, the auto-update engine's
change detection, per-provider routing, the conditional scheduler job, and honest trade-off framing
(Tailscale = no port-forward/no public IP; the DDNS providers need a port-forward)."""
from __future__ import annotations

import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    for k in [k for k in os.environ if k.startswith("DDNS_")]:
        os.environ.pop(k)
    from mongoose.db import init_db
    init_db()
    return d


def test_all_six_options_present_with_correct_categories():
    _fresh()
    import mongoose.ddns as D
    slugs = [p.slug for p in D.PROVIDERS]
    assert slugs == ["tailscale", "duckdns", "dynu", "noip", "cloudflare"]
    # Tailscale is a VPN that needs no port-forward; the rest are DDNS needing a port-forward
    assert D.provider_by_slug("tailscale").kind == "vpn"
    assert D.provider_by_slug("tailscale").needs_portforward is False
    for s in ("duckdns", "dynu", "noip", "cloudflare"):
        assert D.provider_by_slug(s).kind == "ddns"
        assert D.provider_by_slug(s).needs_portforward is True


def test_update_engine_only_calls_provider_when_ip_changes():
    _fresh()
    import mongoose.ddns as D
    D.detect_public_ip = lambda timeout=10.0: "203.0.113.7"
    calls = []
    D._UPDATERS["duckdns"] = lambda env, ip: (calls.append(ip) or (True, "ok"))
    env = {"DDNS_PROVIDER": "duckdns", "DDNS_DUCKDNS_DOMAIN": "x", "DDNS_DUCKDNS_TOKEN": "t"}
    assert D.update_now(env=env)["changed"] is True       # first time: changed
    assert D.update_now(env=env)["changed"] is False      # unchanged: skipped
    assert len(calls) == 1                                  # provider not called the 2nd time
    assert D.update_now(env=env, force=True)["changed"] is True
    assert len(calls) == 2                                  # force calls again


def test_tailscale_update_is_a_noop():
    _fresh()
    import mongoose.ddns as D
    res = D.update_now(env={"DDNS_PROVIDER": "tailscale"})
    assert res["ok"] is True
    assert res.get("noop") is True


def test_update_with_no_provider_is_actionable():
    _fresh()
    import mongoose.ddns as D
    res = D.update_now(env={})
    assert res["ok"] is False
    assert "ddns setup" in res["detail"]


def test_scheduler_job_registered_only_when_enabled():
    _fresh()
    import mongoose.services.scheduler as SCH
    os.environ.pop("DDNS_ENABLED", None)
    os.environ.pop("DDNS_PROVIDER", None)
    assert "ddns_update" not in SCH._build_registry()
    os.environ["DDNS_ENABLED"] = "true"
    os.environ["DDNS_PROVIDER"] = "duckdns"
    try:
        assert "ddns_update" in SCH._build_registry()
        assert "ddns_update" in SCH._JOB_DESCRIPTIONS
    finally:
        os.environ.pop("DDNS_ENABLED", None)
        os.environ.pop("DDNS_PROVIDER", None)


def test_ddns_job_runs_without_crashing_on_partial_config():
    _fresh()
    import asyncio
    import mongoose.services.scheduler as SCH
    res = asyncio.run(SCH._job_ddns_update())
    assert isinstance(res, dict)  # never raises


def test_ddns_integration_not_falsely_configured():
    d = _fresh()
    import mongoose.setup_wizard as SW
    from pathlib import Path
    envf = Path(d) / ".env"
    SW.ENV_PATH = envf
    envf.write_text("")
    st = {i["slug"]: i for i in SW.integration_status()}
    assert "ddns" in st
    assert st["ddns"]["configured"] is False


def test_cli_ddns_status_wired():
    _fresh()
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "mongoose.cli", "ddns", "status"],
                       env=dict(os.environ, PYTHONPATH="src"), capture_output=True, text=True)
    assert "invalid choice" not in r.stderr
    assert r.returncode in (0, 1)


# ---- browser/cockpit UI for DDNS ----

def test_ddns_options_payload_has_providers_and_fields():
    _fresh()
    import mongoose.ddns as D
    op = D.options_payload(env={})
    slugs = [p["slug"] for p in op["providers"]]
    assert slugs == ["tailscale", "duckdns", "dynu", "noip", "cloudflare"]
    ts = next(p for p in op["providers"] if p["slug"] == "tailscale")
    assert ts["recommended"] is True and ts["needs_portforward"] is False and ts["exposes_public_ip"] is False
    duck = next(p for p in op["providers"] if p["slug"] == "duckdns")
    assert duck["exposes_public_ip"] is True
    assert [f["key"] for f in duck["fields"]] == ["DDNS_DUCKDNS_DOMAIN", "DDNS_DUCKDNS_TOKEN"]
    # secret flag carried for tokens
    assert next(f for f in duck["fields"] if f["key"] == "DDNS_DUCKDNS_TOKEN")["secret"] is True


def test_save_ddns_validates_required_fields():
    _fresh()
    import mongoose.ddns as D
    r = D.save_ddns("duckdns", {}, do_update=False)
    assert r["ok"] is False and "fill in" in r["detail"].lower()
    r2 = D.save_ddns("duckdns", {"DDNS_DUCKDNS_DOMAIN": "h", "DDNS_DUCKDNS_TOKEN": "t"}, do_update=False)
    assert r2["ok"] is True


def test_save_ddns_tailscale_records_intent():
    _fresh()
    import mongoose.ddns as D
    r = D.save_ddns("tailscale", {}, do_update=False)
    assert r["ok"] is True and r.get("needs_install") is True


def test_cockpit_ddns_endpoints_exist():
    _fresh()
    from mongoose.app import app
    routes = {r.path for r in app.routes}
    for p in ("/cockpit/ddns/options", "/cockpit/ddns/save", "/cockpit/ddns/update",
              "/cockpit/ddns/disable", "/cockpit/ddns/tailscale-up"):
        assert p in routes


def test_cockpit_html_has_ddns_ui():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "function ddnsCardShell(" in h
    assert "async function loadDdns(" in h
    # DDNS is rendered as a setup tile whose form (the ddns card) expands on tap, via the grid's
    # per-stage form store, so its card shell is emitted into that store
    assert 'data-slug="ddns"' in h and "ddnsCardShell()" in h
    assert "function wizExpand(" in h
    # the honest trade-off framing is shown in the UI
    assert "publishes your home" in h
    assert "home IP is never exposed" in h


def test_ddns_appears_in_setup_wizard_stage():
    _fresh()
    from mongoose.wizard import wizard_schema
    w = wizard_schema()
    found = any("ddns" in [i["slug"] for i in s["integrations"]] for s in w["stages"])
    assert found, "ddns must appear in the staged setup wizard (at setup time)"
