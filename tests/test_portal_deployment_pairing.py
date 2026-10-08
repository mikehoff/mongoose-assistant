"""Deployment-pairing gates: the WordPress plugin scaffold must expose every sync endpoint
Mongoose's rtm_portal calls; PyNaCl must be a known capability so the updater offers to
install it; and the scheduler's portal jobs must skip cleanly until the portal is ready."""
from __future__ import annotations

import asyncio
import inspect
import re
import warnings

warnings.filterwarnings("ignore")


def test_scaffold_exposes_every_endpoint_mongoose_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_PLUGIN_DIR", str(tmp_path / "plug"))
    from mongoose.services import rtm_portal, wp_plugin
    r = wp_plugin.scaffold_example_court_plugin("0.0.0-test")
    assert r["ok"]
    sync_php = (tmp_path / "plug" / "includes" / "portal-sync.php").read_text()
    src = inspect.getsource(rtm_portal)
    called = set(re.findall(r'_wp_call\(\s*"[A-Z]+",\s*f?"(/wp-json/example-court/v1/[^"]+)"', src))
    assert called, "no sync calls found in rtm_portal; pairing test is stale"
    for path in called:
        stem = path.replace("/wp-json/example-court/v1/", "").split("{")[0].rstrip("/")
        assert stem in sync_php, f"plugin scaffold missing endpoint for {path}"
    # crypto + auth primitives on the PHP side
    for needle in ("sodium_crypto_box_seal", "hash_equals", "X-Mongoose-Signature",
                   "pc_rtm_claim_public_key", "pc_rtm_webhook_secret"):
        assert needle in sync_php


def test_pynacl_is_a_known_capability():
    from mongoose.services.capabilities import _PIP_TO_IMPORT, capabilities_report
    assert _PIP_TO_IMPORT.get("pynacl") == "nacl"
    rows = [d for d in capabilities_report()["all"] if d["capability"] == "rtm_portal"]
    assert rows, "rtm_portal capability entry missing from capabilities.yaml"
    from mongoose.services.capabilities import CAPABILITIES_PATH
    assert "pynacl" in CAPABILITIES_PATH.read_text()


def test_portal_jobs_registered_and_skip_until_ready(monkeypatch, tmp_path):
    for k in ("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PC_PORTAL_CLAIM_KEYPAIR_PATH", str(tmp_path / "kp.json"))
    from mongoose.services import scheduler as S
    reg = S._build_registry()
    assert "portal_process_claims" in reg and "portal_pull_issues" in reg
    r = asyncio.run(S._job_portal_process_claims())
    assert "skipped" in r and "WordPress" in r["skipped"]
    monkeypatch.setenv("WP_URL", "https://x.example")
    monkeypatch.setenv("WP_USERNAME", "m")
    monkeypatch.setenv("WP_APP_PASSWORD", "p")
    r2 = asyncio.run(S._job_portal_pull_issues())
    assert "skipped" in r2 and "keypair" in r2["skipped"]
